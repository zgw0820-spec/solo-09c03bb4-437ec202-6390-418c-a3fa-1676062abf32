"""FastAPI 应用：虚拟机系统盘分段修复台。

路由
----
GET    /                              网页控制台
GET    /api/info                      镜像信息（大小/块数/块大小/默认 TTL）
GET    /api/leases                    租约列表
POST   /api/leases                    领取租约 {start_block, block_count, ttl_seconds?}
DELETE /api/leases/{id}               释放租约
PUT    /api/leases/{id}/data          上传与区间等长的备份数据并整段提交
GET    /api/image/blocks/{index}      按块回读镜像（4 KiB）
GET    /api/image/download            下载整个 Raw 镜像
GET    /api/backups                   服务器上的演示备份段清单
GET    /api/backups/{name}            下载某个备份段
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from .core import BLOCK_SIZE, ApiError, ImageStore, LeaseManager
from .sample import DEFAULT_DAMAGE, ensure_sample_image, parse_damage

STATIC_DIR = Path(__file__).parent / "static"
_BACKUP_NAME_RE = re.compile(r"segment_\d+_\d+\.bin")


class ClaimRequest(BaseModel):
    start_block: int
    block_count: int
    ttl_seconds: Optional[float] = None


def create_app(
    data_dir: str | Path = "data",
    image_size_mib: int = 8,
    default_ttl: float = 60.0,
    damage: list[tuple[int, int]] | None = None,
) -> FastAPI:
    data_dir = Path(data_dir)
    image_path, backups_dir = ensure_sample_image(data_dir, image_size_mib, damage)
    store = ImageStore(image_path, data_dir / "journal")
    manager = LeaseManager(store, data_dir / "staging", default_ttl=default_ttl)

    app = FastAPI(title="虚拟机系统盘分段修复台", version="1.0.0")
    app.state.store = store
    app.state.manager = manager

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    # ---------- 网页 ----------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    # ---------- 信息 ----------

    @app.get("/api/info")
    def info() -> dict:
        return {
            "image_path": str(store.path),
            "image_size_bytes": store.size_bytes,
            "total_blocks": store.total_blocks,
            "block_size": BLOCK_SIZE,
            "default_lease_ttl_seconds": manager.default_ttl,
            "recovered_journals": store.recovered_journals,
        }

    # ---------- 租约 ----------

    @app.get("/api/leases")
    async def list_leases() -> dict:
        return {"leases": await manager.list_public()}

    @app.post("/api/leases", status_code=201)
    async def claim_lease(body: ClaimRequest) -> dict:
        lease = await manager.claim(body.start_block, body.block_count, body.ttl_seconds)
        return lease.public(time.time())

    @app.delete("/api/leases/{lease_id}")
    async def release_lease(lease_id: str) -> dict:
        lease = await manager.release(lease_id)
        return lease.public(time.time())

    @app.put("/api/leases/{lease_id}/data")
    async def upload_data(lease_id: str, request: Request) -> dict:
        lease = await manager.upload_and_commit(lease_id, request)
        return {"status": "committed", **lease.public(time.time())}

    # ---------- 镜像读取 ----------

    @app.get("/api/image/blocks/{index}")
    def read_block(index: int) -> Response:
        return Response(content=store.read_block(index), media_type="application/octet-stream")

    @app.get("/api/image/download")
    def download_image() -> FileResponse:
        # FileResponse 流式发送，修复某段不需要复制整盘
        return FileResponse(
            store.path,
            media_type="application/octet-stream",
            filename="disk-repaired.raw",
        )

    # ---------- 演示备份段 ----------

    @app.get("/api/backups")
    def list_backups() -> dict:
        manifest = backups_dir / "manifest.json"
        if not manifest.exists():
            return {"backups": []}
        return {"backups": json.loads(manifest.read_text(encoding="utf-8"))}

    @app.get("/api/backups/{name}")
    def get_backup(name: str) -> FileResponse:
        if not _BACKUP_NAME_RE.fullmatch(name):
            raise ApiError(404, "no such backup segment")
        path = backups_dir / name
        if not path.is_file():
            raise ApiError(404, "no such backup segment")
        return FileResponse(path, media_type="application/octet-stream", filename=name)

    return app


def _create_app_from_env() -> FastAPI:
    damage_env = os.environ.get("DEMO_DAMAGE")
    damage = parse_damage(damage_env) if damage_env else DEFAULT_DAMAGE
    return create_app(
        data_dir=os.environ.get("DATA_DIR", "data"),
        image_size_mib=int(os.environ.get("IMAGE_SIZE_MIB", "8")),
        default_ttl=float(os.environ.get("LEASE_TTL_SECONDS", "60")),
        damage=damage,
    )


app = _create_app_from_env()
