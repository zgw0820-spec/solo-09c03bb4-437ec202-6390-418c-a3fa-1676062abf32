"""核心逻辑：租约管理 + 带日志（journal）的 Raw 镜像分段原子提交。

设计要点
--------
* 每块 4 KiB，工作器按 (起始块号, 块数) 领取租约。
* 有效（未过期、未提交、未释放）租约的区间互不重叠；过期租约不阻挡新领取。
* 上传先落到独立的暂存文件，收满且长度恰好等于区间字节数后才进入提交。
* 提交是整段原子生效的：先把该段原始字节写入 journal 并 fsync，
  再把新数据写进镜像并 fsync，最后删除 journal。
  - 传输中断：暂存文件不完整，根本不会进入提交，原字节不变。
  - 落盘失败：用 journal 回滚原字节；回滚也失败则保留 journal，
    下次启动时自动恢复。
* 慢上传只占用自己的暂存文件，不持有任何全局锁；提交锁只包住
  “校验租约 + 写镜像”这一小段，因此慢上传期间其他不相交区间仍可提交。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from starlette.requests import ClientDisconnect

BLOCK_SIZE = 4096
_CHUNK = 1024 * 1024  # 镜像/日志顺序读写时的块大小


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


class ImageStore:
    """Raw 镜像文件，支持按块读、整盘流式下载、带 journal 的分段原子提交。"""

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
        # 启动时恢复上次崩溃可能留下的未完成提交
        self.recovered_journals = self._recover_journals()

    def close(self) -> None:
        os.close(self._fd)

    # ---------- 读 ----------

    def read_block(self, index: int) -> bytes:
        if not 0 <= index < self.total_blocks:
            raise ApiError(404, f"block {index} out of range (0..{self.total_blocks - 1})")
        return os.pread(self._fd, BLOCK_SIZE, index * BLOCK_SIZE)

    # ---------- 原子提交 ----------

    def commit_segment(self, lease: Lease, staging_path: Path) -> None:
        """把暂存文件整段写入镜像；失败时保留/恢复该段原字节。

        协议：journal 原字节 -> fsync -> 写新数据 -> fsync -> 删 journal。
        任何 OSError 都触发回滚；回滚也失败则保留 journal 供重启恢复。
        """
        offset, length = lease.byte_offset, lease.byte_length
        staged_size = staging_path.stat().st_size
        if staged_size != length:
            raise ApiError(400, f"staged data is {staged_size} bytes, expected {length}")
        journal = self._journal_path(lease.id, offset, length)
        self._write_journal(journal, offset, length)
        try:
            self._stream_file_to_image(staging_path, offset)
            os.fsync(self._fd)
        except OSError:
            try:
                self._restore_journal(journal, offset)
                os.fsync(self._fd)
                journal.unlink()
            except OSError:
                # 回滚也失败：保留 journal，重启时 _recover_journals 会恢复
                pass
            raise
        journal.unlink()

    # ---------- journal 内部实现 ----------

    def _journal_path(self, lease_id: str, offset: int, length: int) -> Path:
        return self.journal_dir / f"{lease_id}_{offset}_{length}.orig"

    def _write_journal(self, journal: Path, offset: int, length: int) -> None:
        with open(journal, "wb") as jf:
            pos, remaining = offset, length
            while remaining > 0:
                chunk = os.pread(self._fd, min(_CHUNK, remaining), pos)
                if not chunk:
                    raise OSError("short read from image while journaling")
                jf.write(chunk)
                pos += len(chunk)
                remaining -= len(chunk)
            jf.flush()
            os.fsync(jf.fileno())
        self._fsync_dir(self.journal_dir)

    def _restore_journal(self, journal: Path, offset: int) -> None:
        with open(journal, "rb") as jf:
            pos = offset
            while True:
                chunk = jf.read(_CHUNK)
                if not chunk:
                    break
                self._write_image(chunk, pos)
                pos += len(chunk)

    def _recover_journals(self) -> int:
        recovered = 0
        for jp in sorted(self.journal_dir.glob("*.orig")):
            try:
                _lease_id, offset_s, length_s = jp.stem.rsplit("_", 2)
                offset, length = int(offset_s), int(length_s)
            except ValueError:
                continue  # 文件名无法解析，留人工检查
            if jp.stat().st_size != length:
                continue  # journal 本身不完整，留人工检查
            self._restore_journal(jp, offset)
            os.fsync(self._fd)
            jp.unlink()
            recovered += 1
        return recovered

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

    def _stream_file_to_image(self, src: Path, offset: int) -> None:
        with open(src, "rb") as sf:
            pos = offset
            while True:
                chunk = sf.read(_CHUNK)
                if not chunk:
                    break
                self._write_image(chunk, pos)
                pos += len(chunk)

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

    async def upload_and_commit(self, lease_id: str, request) -> Lease:
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
                await self._commit(lease, staging)
            finally:
                staging.unlink(missing_ok=True)
                await self._end_upload(lease)
        return lease

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

    async def _commit(self, lease: Lease, staging: Path) -> None:
        # 提交前再次校验：上传先开始、提交时已过期 -> 拒绝
        now = time.time()
        async with self._table_lock:
            if lease.state != "active":
                raise ApiError(409, f"lease is {lease.state}")
            if lease.is_expired(now):
                raise ApiError(410, "lease expired before commit; upload rejected")
        try:
            await asyncio.to_thread(self.store.commit_segment, lease, staging)
        except ApiError:
            raise
        except OSError as exc:
            raise ApiError(500, f"commit failed ({exc}); original bytes restored")
        async with self._table_lock:
            lease.state = "committed"

    def _staging_path(self, lease_id: str) -> Path:
        return self.staging_dir / f"{lease_id}.part"
