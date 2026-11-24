"""API 级自测：租约、重叠、过期、长度校验、按块稀疏提交、回滚/恢复、回读、下载。"""

import os
import time

import pytest
from fastapi.testclient import TestClient

from app.core import BLOCK_SIZE, ImageStore
from app.main import create_app
from app.sample import block_pattern

SIZE_MIB = 1  # 256 块，跑得快
DAMAGE = [(4, 2)]


@pytest.fixture()
def env(tmp_path):
    app = create_app(data_dir=tmp_path, image_size_mib=SIZE_MIB, default_ttl=5.0, damage=DAMAGE)
    with TestClient(app) as client:
        yield client, app.state.manager, app.state.store


def claim(client, start, count, ttl=None):
    body = {"start_block": start, "block_count": count}
    if ttl is not None:
        body["ttl_seconds"] = ttl
    return client.post("/api/leases", json=body)


def test_info_and_sample_image(env):
    client, _, store = env
    info = client.get("/api/info").json()
    assert info["block_size"] == BLOCK_SIZE
    assert info["total_blocks"] == SIZE_MIB * 256
    assert info["image_size_bytes"] == SIZE_MIB * 1024 * 1024
    # 出厂损坏区间是 0xEE，备份段内容等于原始 pattern
    assert client.get("/api/image/blocks/4").content == b"\xee" * BLOCK_SIZE
    backups = client.get("/api/backups").json()["backups"]
    assert backups == [
        {"file": "segment_4_2.bin", "start_block": 4, "block_count": 2, "bytes": 8192}
    ]
    payload = client.get("/api/backups/segment_4_2.bin").content
    assert payload == block_pattern(4) + block_pattern(5)
    assert store.total_blocks == SIZE_MIB * 256


def test_claim_upload_commit_readback(env):
    client, _, _ = env
    lease = claim(client, 10, 2).json()
    data = b"\xab" * (2 * BLOCK_SIZE)
    r = client.put(f"/api/leases/{lease['id']}/data", content=data)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "committed"
    assert client.get("/api/image/blocks/10").content == data[:BLOCK_SIZE]
    assert client.get("/api/image/blocks/11").content == data[BLOCK_SIZE:]
    # 相邻块未被波及
    assert client.get("/api/image/blocks/12").content == block_pattern(12)


def test_overlap_rejected_and_adjacent_ok(env):
    client, _, _ = env
    assert claim(client, 20, 4).status_code == 201
    r = claim(client, 22, 4)  # 重叠
    assert r.status_code == 409
    assert "overlaps" in r.json()["detail"]
    assert claim(client, 24, 4).status_code == 201  # 相邻不算重叠
    # 已提交的区间可以再次领取
    lease = claim(client, 30, 1).json()
    client.put(f"/api/leases/{lease['id']}/data", content=b"\x01" * BLOCK_SIZE)
    assert claim(client, 30, 1).status_code == 201


def test_out_of_range_rejected(env):
    client, _, store = env
    assert claim(client, -1, 1).status_code == 400
    assert claim(client, 0, 0).status_code == 400
    assert claim(client, store.total_blocks - 1, 2).status_code == 400
    assert client.get(f"/api/image/blocks/{store.total_blocks}").status_code == 404


def test_expired_lease_rejected_at_commit_and_reclaimable(env):
    client, _, _ = env
    lease = claim(client, 40, 1, ttl=0.3).json()
    time.sleep(0.5)  # 等到过期后再提交：即使数据合法也必须拒绝
    r = client.put(f"/api/leases/{lease['id']}/data", content=b"\x02" * BLOCK_SIZE)
    assert r.status_code == 410
    assert client.get("/api/image/blocks/40").content == block_pattern(40)
    # 过期后同一区间可重新领取
    lease2 = claim(client, 40, 1, ttl=5).json()
    assert lease2["id"] != lease["id"]
    r = client.put(f"/api/leases/{lease2['id']}/data", content=b"\x02" * BLOCK_SIZE)
    assert r.status_code == 200


def test_wrong_length_rejected(env):
    client, _, _ = env
    lease = claim(client, 50, 2).json()
    r = client.put(
        f"/api/leases/{lease['id']}/data",
        content=b"\x03" * 100,
        headers={"Content-Length": "100"},
    )
    assert r.status_code == 400
    # 短数据 + 虚报长度也不行
    r = client.put(f"/api/leases/{lease['id']}/data", content=b"\x03" * 100)
    assert r.status_code == 400
    assert client.get("/api/image/blocks/50").content == block_pattern(50)


def test_release_frees_range(env):
    client, _, _ = env
    lease = claim(client, 60, 2).json()
    assert claim(client, 61, 1).status_code == 409
    r = client.delete(f"/api/leases/{lease['id']}")
    assert r.status_code == 200
    assert claim(client, 61, 1).status_code == 201


def test_unknown_lease(env):
    client, _, _ = env
    assert client.put("/api/leases/nope/data", content=b"x").status_code == 404
    assert client.delete("/api/leases/nope").status_code == 404


def test_commit_failure_rolls_back(env, monkeypatch):
    """落盘失败：部分字节已污染时也必须恢复本次全部改写块的原字节。"""
    client, _, store = env
    lease = claim(client, 70, 2).json()
    original = block_pattern(70) + block_pattern(71)
    real_write = store._write_image
    calls = {"n": 0}

    def flaky(data, offset):
        calls["n"] += 1
        if calls["n"] == 1:
            os.pwrite(store._fd, b"\xff" * 2048, offset)  # 模拟写了一半才坏
            raise OSError("simulated disk failure")
        return real_write(data, offset)

    monkeypatch.setattr(store, "_write_image", flaky)
    r = client.put(f"/api/leases/{lease['id']}/data", content=b"\xab" * (2 * BLOCK_SIZE))
    assert r.status_code == 500
    assert "original bytes restored" in r.json()["detail"]
    assert client.get("/api/image/blocks/70").content == original[:BLOCK_SIZE]
    assert client.get("/api/image/blocks/71").content == original[BLOCK_SIZE:]
    assert list(store.journal_dir.glob("*.jrn")) == []  # 回滚完成，journal 已清理
    assert list(store.journal_dir.glob("*.jrn.tmp")) == []


def test_journal_recovery_after_crash(env, monkeypatch, tmp_path):
    """落盘失败且回滚也失败：块级 journal 留存，重启时自动恢复写前字节。"""
    client, _, store = env
    lease = claim(client, 80, 1).json()
    original = block_pattern(80)

    def dead_disk(data, offset):
        os.pwrite(store._fd, b"\xff" * 1024, offset)  # 每次都先污染再失败
        raise OSError("disk still dead")

    monkeypatch.setattr(store, "_write_image", dead_disk)
    r = client.put(f"/api/leases/{lease['id']}/data", content=b"\xab" * BLOCK_SIZE)
    assert r.status_code == 500
    journals = list(store.journal_dir.glob("*.jrn"))
    assert len(journals) == 1  # 块级 journal 留存
    assert list(store.journal_dir.glob("*.jrn.tmp")) == []

    # 模拟进程重启：重新打开同一镜像，启动时恢复 journal
    store2 = ImageStore(store.path, store.journal_dir)
    assert store2.recovered_journals == 1
    assert store2.read_block(80) == original
    assert list(store.journal_dir.glob("*.jrn")) == []
    store2.close()


def test_legacy_orig_journal_still_recoverable(env, tmp_path):
    """旧版整段 journal（*.orig）必须能在新版启动时还原镜像。"""
    client, _, store = env
    start, count = 84, 3
    original = b"".join(block_pattern(i) for i in range(start, start + count))
    # 手工造一个旧版 journal：文件名 {lease}_{offset}_{length}.orig，内容为整段原字节
    offset, length = start * BLOCK_SIZE, count * BLOCK_SIZE
    legacy = store.journal_dir / f"oldlease_{offset}_{length}.orig"
    legacy.write_bytes(original)
    # 先把镜像对应区间污染，模拟旧版本在改写途中崩溃
    with open(store.path, "r+b") as f:
        f.seek(offset)
        f.write(b"\xcc" * length)
    assert store.read_block(start) != original[:BLOCK_SIZE]

    store2 = ImageStore(store.path, store.journal_dir)
    assert store2.recovered_journals == 1
    restored = b"".join(store2.read_block(i) for i in range(start, start + count))
    assert restored == original
    assert list(store.journal_dir.glob("*.orig")) == []
    store2.close()


def test_identical_blocks_are_skipped(env):
    """与当前盘面相同的块：不写回、不进 journal，响应里报告跳过数。"""
    client, _, store = env
    # 直接回传当前镜像内容（10、11 两块的 pattern），应当全部跳过
    payload = block_pattern(10) + block_pattern(11)
    lease = claim(client, 10, 2).json()
    r = client.put(f"/api/leases/{lease['id']}/data", content=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["blocks_compared"] == 2
    assert body["blocks_written"] == 0
    assert body["blocks_skipped"] == 2
    # 没有任何 journal 残留，镜像不变
    assert list(store.journal_dir.glob("*.jrn")) == []
    assert client.get("/api/image/blocks/10").content == block_pattern(10)


def test_mixed_changed_and_unchanged_non_adjacent(env):
    """4 块里只改第 1、3 块（不相邻）：一起提交，journal 只含 2 个差异块。"""
    client, _, store = env
    start = 100
    payload = b"".join(
        (b"\x7c" * BLOCK_SIZE) if i in (start, start + 2) else block_pattern(i)
        for i in range(start, start + 4)
    )
    lease = claim(client, start, 4).json()
    r = client.put(f"/api/leases/{lease['id']}/data", content=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["blocks_compared"] == 4
    assert body["blocks_written"] == 2
    assert body["blocks_skipped"] == 2
    assert client.get(f"/api/image/blocks/{start}").content == b"\x7c" * BLOCK_SIZE
    assert client.get(f"/api/image/blocks/{start + 1}").content == block_pattern(start + 1)
    assert client.get(f"/api/image/blocks/{start + 2}").content == b"\x7c" * BLOCK_SIZE
    assert client.get(f"/api/image/blocks/{start + 3}").content == block_pattern(start + 3)
    # 相邻区间未被波及
    assert client.get(f"/api/image/blocks/{start + 4}").content == block_pattern(start + 4)


def test_journal_size_scales_with_changed_blocks_only(env):
    """journal 体积只随差异块增长：0 差异无 journal，2 差异只存 2 块写前字节。"""
    client, _, store = env
    # 全相同 -> 不产生 journal 文件
    same = b"".join(block_pattern(i) for i in range(120, 124))
    lease = claim(client, 120, 4).json()
    assert client.put(f"/api/leases/{lease['id']}/data", content=same).status_code == 200
    assert list(store.journal_dir.glob("*.jrn")) == []

    # 拦截 commit 完成前的瞬间：直接驱动底层流程，检查武装后的 journal 大小
    lease2 = claim(client, 130, 4).json()
    staging = store.path.parent / "staging" / f"{lease2['id']}.part"
    payload = b"".join(
        (b"\x5a" * BLOCK_SIZE) if i in (130, 132) else block_pattern(i)
        for i in range(130, 134)
    )
    staging.write_bytes(payload)
    from app.core import Lease

    lease_obj = Lease(
        id=lease2["id"], start_block=130, block_count=4,
        created_at=time.time(), expires_at=time.time() + 60,
    )
    journal = store._journal_path(lease_obj.id, lease_obj.byte_offset, lease_obj.byte_length)
    tmp = store._tmp_journal_path(journal)
    stats = store._build_journal(lease_obj, staging, journal, tmp)
    try:
        assert (stats.written, stats.skipped) == (2, 2)
        # header + 2 * (u32 块号 + 4 KiB 原块) + trailer
        assert journal.stat().st_size == 16 + 2 * (4 + BLOCK_SIZE) + 12
    finally:
        journal.unlink(missing_ok=True)
        staging.unlink(missing_ok=True)


def test_memory_does_not_grow_with_segment_length(env, tmp_path):
    """最坏情况（全部块都不同）下，峰值内存也不随段长增长（全程流式）。"""
    import tracemalloc

    _, _, store = env

    def run_commit(start: int, count: int):
        lease_id = f"mem-{start}-{count}"
        staging = store.path.parent / "staging" / f"{lease_id}.part"
        staging.write_bytes(b"\x42" * (count * BLOCK_SIZE))  # 与 pattern 全不同
        from app.core import Lease

        lease = Lease(
            id=lease_id, start_block=start, block_count=count,
            created_at=time.time(), expires_at=time.time() + 60,
        )
        tracemalloc.start()
        try:
            stats = store.commit_segment(lease, staging)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        staging.unlink(missing_ok=True)
        return peak, stats

    small_peak, small_stats = run_commit(32, 32)     # 128 KiB 段，32 个差异块
    mid_peak, mid_stats = run_commit(96, 64)         # 256 KiB 段，64 个差异块
    big_peak, big_stats = run_commit(160, 96)        # 384 KiB 段，96 个差异块
    assert (small_stats.written, mid_stats.written, big_stats.written) == (32, 64, 96)
    # 差异块数 x3，峰值增量必须远小于 journal 数据量差（256 KiB）：取 64 KiB 上限
    assert big_peak - small_peak < 64 * 1024, (small_peak, mid_peak, big_peak)
    # 后两段的峰值基本持平 -> 平台期，而不是随差异块线性增长
    assert abs(big_peak - mid_peak) < 32 * 1024, (mid_peak, big_peak)


def test_crash_during_apply_restores_on_restart(env, monkeypatch, tmp_path):
    """改写遍中途退出：重启后恢复写前字节，已提交（journal 已删）的改动不回退。"""
    _, _, store = env
    from app.core import Lease

    # 先正常提交一次 [140,142)，作为“已提交改动”
    done_staging = store.path.parent / "staging" / "done.part"
    done_staging.write_bytes(b"\x11" * (2 * BLOCK_SIZE))
    done_lease = Lease(id="done", start_block=140, block_count=2,
                       created_at=time.time(), expires_at=time.time() + 60)
    store.commit_segment(done_lease, done_staging)
    done_staging.unlink(missing_ok=True)
    assert list(store.journal_dir.glob("*.jrn")) == []

    # 第二次提交 [144,148)，在改写第 2 个块时“进程被杀”（不做回滚直接退出）
    staging = store.path.parent / "staging" / "killed.part"
    staging.write_bytes(b"\x22" * (4 * BLOCK_SIZE))
    killed_lease = Lease(id="killed", start_block=144, block_count=4,
                         created_at=time.time(), expires_at=time.time() + 60)
    originals = {i: block_pattern(i) for i in range(144, 148)}
    real_write = store._write_image
    state = {"n": 0}

    def die_second_write(data, offset):
        state["n"] += 1
        if state["n"] == 2:
            # 第一个块已落盘，第二个块写到一半后进程退出
            os.pwrite(store._fd, b"\xee" * 512, offset)
            raise SystemExit("simulated kill mid-commit")
        return real_write(data, offset)

    monkeypatch.setattr(store, "_write_image", die_second_write)
    with pytest.raises(SystemExit):
        store.commit_segment(killed_lease, staging)
    monkeypatch.undo()
    journals = list(store.journal_dir.glob("*.jrn"))
    assert len(journals) == 1  # journal 武装完成且留存
    assert list(store.journal_dir.glob("*.jrn.tmp")) == []

    # 重启恢复：未完成提交的 4 个块全部回到写前字节
    store2 = ImageStore(store.path, store.journal_dir)
    assert store2.recovered_journals == 1
    for i in range(144, 148):
        assert store2.read_block(i) == originals[i]
    # 已完成的提交不受恢复影响
    assert store2.read_block(140) == b"\x11" * BLOCK_SIZE
    assert store2.read_block(141) == b"\x11" * BLOCK_SIZE
    assert list(store.journal_dir.glob("*.jrn")) == []
    store2.close()
    staging.unlink(missing_ok=True)


def test_tmp_journal_from_scan_crash_is_discarded(env, tmp_path):
    """扫描遍崩溃只留下 *.jrn.tmp：重启直接清理，镜像不动。"""
    _, _, store = env
    before = store.read_block(150)
    tmp = store.journal_dir / "ghost_0_4096.jrn.tmp"
    tmp.write_bytes(b"partial journal garbage")
    store2 = ImageStore(store.path, store.journal_dir)
    assert store2.recovered_journals == 0
    assert store2.read_block(150) == before
    assert list(store.journal_dir.glob("*.jrn.tmp")) == []
    store2.close()


def test_download_whole_image(env):
    client, _, store = env
    lease = claim(client, 90, 1).json()
    client.put(f"/api/leases/{lease['id']}/data", content=b"\x7a" * BLOCK_SIZE)
    r = client.get("/api/image/download")
    assert r.status_code == 200
    assert len(r.content) == store.size_bytes
    assert r.content[90 * BLOCK_SIZE : 91 * BLOCK_SIZE] == b"\x7a" * BLOCK_SIZE
    assert r.content[:32] == block_pattern(0)[:32]
