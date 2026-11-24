"""核心逻辑：租约管理 + 按块比对的稀疏 journal 原子提交。

设计要点
--------
* 每块 4 KiB，工作器按 (起始块号, 块数) 领取租约。
* 有效（未过期、未提交、未释放）租约的区间互不重叠；过期租约不阻挡新领取。
* 上传仍然是与租约等长的**完整备份段**，先落到独立暂存文件；收满且长度
  恰好等于区间字节数后才进入提交。
* 提交时按 4 KiB 逐块把暂存数据与当前镜像比对（两遍流式扫描，不把整段
  读进内存）：
  - 相同块：不写回镜像，也不记入回滚日志（跳过）；
  - 不同块：先把该块**写前字节**记进块级 journal，再在第二遍统一改写。
    即使改动块互不相邻，也在同一次提交里一起生效。
* 内存占用只与块大小有关、不随段长增长：任何时刻驻留内存的只有
  少量 4 KiB 缓冲，journal 与暂存都走文件顺序 I/O。
* journal 协议（块级格式，文件名 ``*.jrn``）：
  1. 扫描遍：journal 先写到 ``*.jrn.tmp``，每个差异块追加
     (块号 u32 + 原块 4 KiB)，结尾写带 crc32 的 trailer，fsync 后
     rename 成正式 journal 并 fsync 目录 —— 此刻 journal 武装完成；
  2. 改写遍：按 journal 把暂存中的新块 pwrite 进镜像，fsync；
  3. 删除 journal 并 fsync 目录 —— 提交点。
  - 扫描遍崩溃：只剩 ``.tmp``，镜像一个字节都没动，启动时直接清理；
  - 改写遍失败/退出：journal 完好，启动时把其中记录的写前字节逐个
    还原，本次提交的全部改写撤销；其他已提交（journal 已删除）的
    改动不受影响；
  - 落盘失败且当场回滚也失败：保留 journal，重启恢复。
* 旧版整段 journal（``*.orig``，内容是整段原始字节）仍可在启动时
  原样还原，见 ``_recover_journals``。
* 慢上传只占用自己的暂存文件，不持有任何全局锁；提交锁只包住
  “校验租约 + 写镜像”这一小段，因此慢上传期间其他不相交区间仍可提交。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from starlette.requests import ClientDisconnect

BLOCK_SIZE = 4096
_CHUNK = 1024 * 1024  # 镜像/旧版日志顺序读写时的块大小

# 块级 journal 二进制格式
_JRN_MAGIC = b"BJN1"          # header 魔数（block journal v1）
_JRN_END_MAGIC = b"JEND"      # trailer 魔数
_JRN_HEADER_SIZE = 16         # magic(4) + bsize u16 + reserved u16 + start_block u32 + reserved u32
_JRN_TRAILER_SIZE = 12        # end magic(4) + count u32 + crc32 u32
_JRN_ENTRY_INDEX_SIZE = 4


class ApiError(Exception):
    """可直接映射为 HTTP 响应的业务错误。"""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class Lease:
    id: str
    start_block: int
    block_count: int
    created_at: float
    expires_at: float
    state: str = "active"  # active | committed | released（expired 由时间推算）
    upload_in_progress: bool = False

    @property
    def end_block(self) -> int:
        return self.start_block + self.block_count

    @property
    def byte_offset(self) -> int:
        return self.start_block * BLOCK_SIZE

    @property
    def byte_length(self) -> int:
        return self.block_count * BLOCK_SIZE

    def is_expired(self, now: float) -> bool:
        return self.state == "active" and now >= self.expires_at

    def public(self, now: float) -> dict:
        state = self.state
        if state == "active" and self.is_expired(now):
            state = "expired"
        return {
            "id": self.id,
            "start_block": self.start_block,
            "block_count": self.block_count,
            "end_block": self.end_block,
            "byte_offset": self.byte_offset,
            "byte_length": self.byte_length,
            "state": state,
            "upload_in_progress": self.upload_in_progress,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "seconds_remaining": max(0.0, self.expires_at - now) if state == "active" else 0.0,
        }


@dataclass
class CommitStats:
    """一次提交的按块统计：compared = written + skipped。"""

    compared: int
    written: int
    skipped: int

    def public(self) -> dict:
        return {
            "blocks_compared": self.compared,
            "blocks_written": self.written,
            "blocks_skipped": self.skipped,
        }


class ImageStore:
    """Raw 镜像文件，支持按块读、整盘流式下载、带稀疏 journal 的按块原子提交。"""

    def __init__(self, path: Path, journal_dir: Path):
        self.path = Path(path)
        self.journal_dir = Path(journal_dir)
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        size = self.path.stat().st_size
        if size == 0 or size % BLOCK_SIZE != 0:
            raise ValueError(f"image size {size} is not a positive multiple of {BLOCK_SIZE}")
        self.size_bytes = size
        self.total_blocks = size // BLOCK_SIZE
        self._fd = os.open(self.path, os.O_RDWR)
        # 启动时恢复上次崩溃可能留下的未完成提交（同时兼容旧版整段 journal）
        self.recovered_journals = self._recover_journals()

    def close(self) -> None:
        os.close(self._fd)

    # ---------- 读 ----------

    def read_block(self, index: int) -> bytes:
        if not 0 <= index < self.total_blocks:
            raise ApiError(404, f"block {index} out of range (0..{self.total_blocks - 1})")
        return os.pread(self._fd, BLOCK_SIZE, index * BLOCK_SIZE)

    # ---------- 按块原子提交 ----------

    def commit_segment(self, lease: Lease, staging_path: Path) -> CommitStats:
        """把暂存段按 4 KiB 与当前镜像比对后稀疏提交；失败时撤销本次全部改写。

        流程：
          扫描遍（只读镜像 + 写 journal 临时文件）→ fsync + rename 武装 journal
          → 改写遍（只写差异块）→ fsync 镜像 → 删 journal。
        任何 OSError 都触发回滚；回滚也失败则保留 journal 供重启恢复。
        全程只持有少量 4 KiB 缓冲，内存占用与段长无关。
        """
        offset, length = lease.byte_offset, lease.byte_length
        staged_size = staging_path.stat().st_size
        if staged_size != length:
            raise ApiError(400, f"staged data is {staged_size} bytes, expected {length}")
        journal = self._journal_path(lease.id, offset, length)
        tmp_journal = self._tmp_journal_path(journal)
        try:
            stats = self._build_journal(lease, staging_path, journal, tmp_journal)
            try:
                self._apply_journal(journal, staging_path, lease.start_block)
                os.fsync(self._fd)
            except OSError:
                try:
                    self._restore_block_journal(journal)
                    os.fsync(self._fd)
                    journal.unlink()
                    self._fsync_dir(self.journal_dir)
                except OSError:
                    # 回滚也失败：保留 journal，重启时 _recover_journals 会恢复
                    pass
                raise
        except BaseException:
            # 扫描遍失败：journal 尚未武装，镜像未被触碰，清掉临时文件
            tmp_journal.unlink(missing_ok=True)
            raise
        journal.unlink()
        self._fsync_dir(self.journal_dir)
        return stats

    # ---------- 块级 journal 构建 / 应用 / 还原 ----------

    def _build_journal(
        self, lease: Lease, staging_path: Path, journal: Path, tmp_journal: Path
    ) -> CommitStats:
        """扫描遍：逐块比对，仅把差异块的写前字节追加进 journal 临时文件。

        任意时刻驻留内存的只有两个 4 KiB 块缓冲；差异块数量不累积在内存里，
        只维护计数与增量 crc32。
        """
        staging_fd = os.open(staging_path, os.O_RDONLY)
        written = 0
        try:
            with open(tmp_journal, "wb") as jf:
                header = (
                    _JRN_MAGIC
                    + BLOCK_SIZE.to_bytes(2, "little")
                    + (0).to_bytes(2, "little")
                    + lease.start_block.to_bytes(4, "little")
                    + (0).to_bytes(4, "little")
                )
                jf.write(header)
                jf.flush()
                crc = zlib.crc32(header)
                for i in range(lease.block_count):
                    block_index = lease.start_block + i
                    image_block = os.pread(self._fd, BLOCK_SIZE, block_index * BLOCK_SIZE)
                    staged_block = os.pread(staging_fd, BLOCK_SIZE, i * BLOCK_SIZE)
                    if len(image_block) != BLOCK_SIZE or len(staged_block) != BLOCK_SIZE:
                        raise OSError("short read while comparing blocks")
                    if image_block == staged_block:
                        continue  # 相同块：不写镜像，也不进 journal
                    entry = block_index.to_bytes(_JRN_ENTRY_INDEX_SIZE, "little") + image_block
                    jf.write(entry)
                    crc = zlib.crc32(entry, crc)
                    written += 1
                trailer = (
                    _JRN_END_MAGIC
                    + written.to_bytes(4, "little")
                    + (crc & 0xFFFFFFFF).to_bytes(4, "little")
                )
                jf.write(trailer)
                jf.flush()
                os.fsync(jf.fileno())
            os.rename(tmp_journal, journal)  # 武装：rename 原子可见
            self._fsync_dir(self.journal_dir)
        finally:
            os.close(staging_fd)
        return CommitStats(
            compared=lease.block_count, written=written,
            skipped=lease.block_count - written,
        )

    def _verify_journal(self, journal: Path) -> int:
        """校验块级 journal（魔数/尺寸/crc32），全程流式、常量内存；返回条目数。"""
        file_size = journal.stat().st_size
        if file_size < _JRN_HEADER_SIZE + _JRN_TRAILER_SIZE:
            raise ValueError("block journal too short")
        with open(journal, "rb") as jf:
            header = jf.read(_JRN_HEADER_SIZE)
            if len(header) != _JRN_HEADER_SIZE or header[:4] != _JRN_MAGIC:
                raise ValueError("bad block journal header")
            bsize = int.from_bytes(header[4:6], "little")
            if bsize != BLOCK_SIZE:
                raise ValueError("block journal block size mismatch")
            jf.seek(file_size - _JRN_TRAILER_SIZE)
            trailer = jf.read(_JRN_TRAILER_SIZE)
            if len(trailer) != _JRN_TRAILER_SIZE or trailer[:4] != _JRN_END_MAGIC:
                raise ValueError("bad block journal trailer")
            count = int.from_bytes(trailer[4:8], "little")
            stored_crc = int.from_bytes(trailer[8:12], "little")
            entries_size = count * (_JRN_ENTRY_INDEX_SIZE + bsize)
            if file_size != _JRN_HEADER_SIZE + entries_size + _JRN_TRAILER_SIZE:
                raise ValueError("block journal size does not match entry count")
            # 增量 crc：每次最多 64 KiB，内存占用与条目数（段长）无关
            jf.seek(0)
            remaining = _JRN_HEADER_SIZE + entries_size
            crc = 0
            while remaining > 0:
                chunk = jf.read(min(65536, remaining))
                if not chunk:
                    raise ValueError("truncated block journal while checksumming")
                crc = zlib.crc32(chunk, crc)
                remaining -= len(chunk)
            if (crc & 0xFFFFFFFF) != stored_crc:
                raise ValueError("block journal crc mismatch")
        return count

    def _read_journal_entries(self, journal: Path):
        """校验通过后流式 yielding (block_index, 原块字节)，每次只驻留一个块。"""
        count = self._verify_journal(journal)
        with open(journal, "rb") as jf:
            jf.seek(_JRN_HEADER_SIZE)
            for _ in range(count):
                index_bytes = jf.read(_JRN_ENTRY_INDEX_SIZE)
                orig = jf.read(BLOCK_SIZE)
                if len(index_bytes) != _JRN_ENTRY_INDEX_SIZE or len(orig) != BLOCK_SIZE:
                    raise ValueError("truncated block journal entry")
                block_index = int.from_bytes(index_bytes, "little")
                if not 0 <= block_index < self.total_blocks:
                    raise ValueError("block journal entry out of range")
                yield block_index, orig

    def _apply_journal(self, journal: Path, staging_path: Path, start_block: int) -> None:
        """改写遍：按 journal 记录的差异块，把暂存中的新块写进镜像。"""
        staging_fd = os.open(staging_path, os.O_RDONLY)
        try:
            for block_index, _orig in self._read_journal_entries(journal):
                new_block = os.pread(
                    staging_fd, BLOCK_SIZE, (block_index - start_block) * BLOCK_SIZE
                )
                if len(new_block) != BLOCK_SIZE:
                    raise OSError("short read from staging while applying journal")
                self._write_image(new_block, block_index * BLOCK_SIZE)
        finally:
            os.close(staging_fd)

    def _restore_block_journal(self, journal: Path) -> None:
        """把块级 journal 中记录的写前字节逐个还原（回滚 / 启动恢复共用）。"""
        for block_index, orig in self._read_journal_entries(journal):
            self._write_image(orig, block_index * BLOCK_SIZE)

    # ---------- 启动恢复（新块级 journal + 旧整段 journal） ----------

    def _recover_journals(self) -> int:
        recovered = 0
        # 1. 扫描遍崩溃的残留：journal 未武装，镜像从未被改，直接清理
        for tmp in sorted(self.journal_dir.glob("*.jrn.tmp")):
            tmp.unlink(missing_ok=True)
        # 2. 新版块级 journal：校验通过才还原，损坏的留人工检查
        for jp in sorted(self.journal_dir.glob("*.jrn")):
            try:
                self._restore_block_journal(jp)
            except (OSError, ValueError):
                continue
            os.fsync(self._fd)
            jp.unlink()
            self._fsync_dir(self.journal_dir)
            recovered += 1
        # 3. 旧版整段 journal（*.orig）：内容即整段原始字节，按文件名偏移还原
        for jp in sorted(self.journal_dir.glob("*.orig")):
            try:
                _lease_id, offset_s, length_s = jp.stem.rsplit("_", 2)
                offset, length = int(offset_s), int(length_s)
            except ValueError:
                continue  # 文件名无法解析，留人工检查
            if jp.stat().st_size != length:
                continue  # journal 本身不完整，留人工检查
            try:
                self._restore_legacy_journal(jp, offset)
            except OSError:
                continue
            os.fsync(self._fd)
            jp.unlink()
            self._fsync_dir(self.journal_dir)
            recovered += 1
        return recovered

    def _restore_legacy_journal(self, journal: Path, offset: int) -> None:
        with open(journal, "rb") as jf:
            pos = offset
            while True:
                chunk = jf.read(_CHUNK)
                if not chunk:
                    break
                self._write_image(chunk, pos)
                pos += len(chunk)

    # ---------- journal 路径 ----------

    def _journal_path(self, lease_id: str, offset: int, length: int) -> Path:
        return self.journal_dir / f"{lease_id}_{offset}_{length}.jrn"

    def _tmp_journal_path(self, journal: Path) -> Path:
        return journal.with_name(journal.name + ".tmp")

    # ---------- 底层写 ----------

    def _write_image(self, data: bytes, offset: int) -> None:
        """把 data 完整写进镜像 offset 处（测试可注入故障）。"""
        self._pwrite_all(data, offset)

    def _pwrite_all(self, data: bytes, offset: int) -> None:
        view = memoryview(data)
        while view:
            written = os.pwrite(self._fd, view, offset)
            if written == 0:
                raise OSError("pwrite returned 0")
            offset += written
            view = view[written:]

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class LeaseManager:
    """租约表 + 上传接收 + 提交编排。"""

    def __init__(self, store: ImageStore, staging_dir: Path, default_ttl: float = 60.0):
        self.store = store
        self.staging_dir = Path(staging_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = float(default_ttl)
        self._leases: dict[str, Lease] = {}
        self._table_lock = asyncio.Lock()   # 只保护租约表与租约字段，持锁时间极短
        self._commit_lock = asyncio.Lock()  # 串行化镜像提交，上传不经过它
        # 重启后旧租约已不存在，遗留暂存文件直接清理
        for leftover in self.staging_dir.glob("*.part"):
            leftover.unlink(missing_ok=True)

    # ---------- 领取 / 释放 / 查询 ----------

    async def claim(self, start_block: int, block_count: int,
                    ttl_seconds: Optional[float] = None) -> Lease:
        if start_block < 0:
            raise ApiError(400, "start_block must be >= 0")
        if block_count < 1:
            raise ApiError(400, "block_count must be >= 1")
        if start_block + block_count > self.store.total_blocks:
            raise ApiError(
                400,
                f"range [{start_block}, {start_block + block_count}) exceeds "
                f"total blocks {self.store.total_blocks}",
            )
        ttl = self.default_ttl if ttl_seconds is None else float(ttl_seconds)
        if not 0.1 <= ttl <= 3600:
            raise ApiError(400, "ttl_seconds must be within [0.1, 3600]")
        now = time.time()
        async with self._table_lock:
            for other in self._leases.values():
                if other.state != "active" or other.is_expired(now):
                    continue  # 已提交/已释放/已过期的不阻挡新领取
                if start_block < other.end_block and other.start_block < start_block + block_count:
                    raise ApiError(
                        409,
                        f"range overlaps active lease {other.id} "
                        f"[{other.start_block}, {other.end_block})",
                    )
            lease = Lease(
                id=uuid.uuid4().hex,
                start_block=start_block,
                block_count=block_count,
                created_at=now,
                expires_at=now + ttl,
            )
            self._leases[lease.id] = lease
        return lease

    async def release(self, lease_id: str) -> Lease:
        async with self._table_lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise ApiError(404, "unknown lease")
            if lease.state != "active":
                raise ApiError(409, f"cannot release a {lease.state} lease")
            if lease.upload_in_progress:
                raise ApiError(409, "upload in progress for this lease")
            lease.state = "released"
        self._staging_path(lease_id).unlink(missing_ok=True)
        return lease

    async def list_public(self) -> list[dict]:
        now = time.time()
        async with self._table_lock:
            leases = sorted(self._leases.values(), key=lambda l: l.created_at)
            return [l.public(now) for l in leases]

    # ---------- 上传 + 提交 ----------

    async def upload_and_commit(self, lease_id: str, request) -> tuple[Lease, CommitStats]:
        lease = await self._begin_upload(lease_id)
        staging = self._staging_path(lease.id)
        try:
            await self._receive_body(request, staging, lease)
        except BaseException:
            # 传输中断 / 长度不符 / 上传中途过期：不进入提交，原字节不变
            staging.unlink(missing_ok=True)
            await self._end_upload(lease)
            raise
        async with self._commit_lock:
            try:
                stats = await self._commit(lease, staging)
            finally:
                staging.unlink(missing_ok=True)
                await self._end_upload(lease)
        return lease, stats

    async def _begin_upload(self, lease_id: str) -> Lease:
        now = time.time()
        async with self._table_lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise ApiError(404, "unknown lease")
            if lease.state != "active":
                raise ApiError(409, f"lease is {lease.state}")
            if lease.is_expired(now):
                raise ApiError(410, "lease expired; claim the range again")
            if lease.upload_in_progress:
                raise ApiError(409, "an upload is already in progress for this lease")
            lease.upload_in_progress = True
            return lease

    async def _end_upload(self, lease: Lease) -> None:
        async with self._table_lock:
            lease.upload_in_progress = False

    async def _receive_body(self, request, staging: Path, lease: Lease) -> None:
        """把请求体完整落到暂存文件；长度必须恰好等于区间字节数。"""
        expected = lease.byte_length
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                raise ApiError(400, "invalid content-length header")
            if declared != expected:
                raise ApiError(
                    400,
                    f"backup data must be exactly {expected} bytes "
                    f"(declared content-length {declared})",
                )
        received = 0
        try:
            with open(staging, "wb") as sf:
                async for chunk in request.stream():
                    if lease.is_expired(time.time()):
                        raise ApiError(410, "lease expired during upload")
                    if chunk:
                        await asyncio.to_thread(sf.write, chunk)
                        received += len(chunk)
                        if received > expected:
                            raise ApiError(400, f"uploaded more than {expected} bytes")
                await asyncio.to_thread(sf.flush)
                await asyncio.to_thread(os.fsync, sf.fileno())
        except ClientDisconnect:
            raise ApiError(400, "transfer interrupted; segment left unchanged")
        if received != expected:
            raise ApiError(400, f"received {received} bytes, expected exactly {expected}")

    async def _commit(self, lease: Lease, staging: Path) -> CommitStats:
        # 提交前再次校验：上传先开始、提交时已过期 -> 拒绝
        now = time.time()
        async with self._table_lock:
            if lease.state != "active":
                raise ApiError(409, f"lease is {lease.state}")
            if lease.is_expired(now):
                raise ApiError(410, "lease expired before commit; upload rejected")
        try:
            stats = await asyncio.to_thread(self.store.commit_segment, lease, staging)
        except ApiError:
            raise
        except OSError as exc:
            raise ApiError(500, f"commit failed ({exc}); original bytes restored")
        async with self._table_lock:
            lease.state = "committed"
        return stats

    def _staging_path(self, lease_id: str) -> Path:
        return self.staging_dir / f"{lease_id}.part"
