"""API 级自测：租约、重叠、过期、长度校验、原子提交、回读、下载。"""

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
    assert client.get(f"/api/image/blocks/10").content == data[:BLOCK_SIZE]
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
    """落盘失败：部分字节已污染时也必须恢复该段原字节。"""
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
    assert list(store.journal_dir.glob("*.orig")) == []  # 回滚完成，journal 已清理


def test_journal_recovery_after_crash(env, monkeypatch, tmp_path):
    """落盘失败且回滚也失败：journal 留存，重启时自动恢复原字节。"""
    client, _, store = env
    lease = claim(client, 80, 1).json()
    original = block_pattern(80)

    def dead_disk(data, offset):
        os.pwrite(store._fd, b"\xff" * 1024, offset)  # 每次都先污染再失败
        raise OSError("disk still dead")

    monkeypatch.setattr(store, "_write_image", dead_disk)
    r = client.put(f"/api/leases/{lease['id']}/data", content=b"\xab" * BLOCK_SIZE)
    assert r.status_code == 500
    assert len(list(store.journal_dir.glob("*.orig"))) == 1  # journal 留存

    # 模拟进程重启：重新打开同一镜像，启动时恢复 journal
    store2 = ImageStore(store.path, store.journal_dir)
    assert store2.recovered_journals == 1
    assert store2.read_block(80) == original
    assert list(store.journal_dir.glob("*.orig")) == []
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
