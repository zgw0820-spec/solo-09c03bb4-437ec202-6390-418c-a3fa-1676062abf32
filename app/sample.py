"""本地 Raw 样例盘生成。

镜像内容确定性生成：每个 4 KiB 块以 ``VMDISK-BLOCK-<序号>`` 头开始，
其余字节填充块序号（小端 4 字节重复），便于按块回读时肉眼核对。

生成后按 ``damage`` 列表把若干区间覆盖为 0xEE（模拟损坏），
同时把这些区间的原始字节导出到 ``backups/segment_<起始块>_<块数>.bin``，
作为网页上"从备份补回"的演示数据源。
"""

from __future__ import annotations

import json
from pathlib import Path

from .core import BLOCK_SIZE

DEFAULT_DAMAGE = [(64, 8), (512, 16), (1500, 4)]
DAMAGE_BYTE = b"\xee"


def block_pattern(index: int) -> bytes:
    header = f"VMDISK-BLOCK-{index:06d}".encode("ascii").ljust(32, b"\x00")
    word = index.to_bytes(4, "little")
    return header + word * ((BLOCK_SIZE - len(header)) // 4)


def parse_damage(spec: str) -> list[tuple[int, int]]:
    """解析 "起始块:块数,起始块:块数" 形式的环境变量。"""
    ranges = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        start_s, count_s = part.split(":")
        ranges.append((int(start_s), int(count_s)))
    return ranges


def ensure_sample_image(
    data_dir: Path,
    size_mib: int,
    damage: list[tuple[int, int]] | None = None,
) -> tuple[Path, Path]:
    """确保样例盘存在；不存在则生成（含损坏区间与对应备份文件）。

    已存在时不做任何改动——修复成果在重启后保留。
    返回 (镜像路径, 备份目录)。
    """
    data_dir = Path(data_dir)
    image_path = data_dir / "disk.raw"
    backups_dir = data_dir / "backups"
    if image_path.exists():
        return image_path, backups_dir

    data_dir.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)
    damage = DEFAULT_DAMAGE if damage is None else damage
    total_blocks = (size_mib * 1024 * 1024) // BLOCK_SIZE

    # 1. 生成完好的样例盘
    with open(image_path, "wb") as f:
        for i in range(total_blocks):
            f.write(block_pattern(i))

    # 2. 导出备份段后把对应区间"损坏"为 0xEE
    manifest = []
    with open(image_path, "r+b") as f:
        for start, count in damage:
            if start < 0 or count < 1 or start + count > total_blocks:
                continue
            name = f"segment_{start}_{count}.bin"
            payload = b"".join(block_pattern(i) for i in range(start, start + count))
            (backups_dir / name).write_bytes(payload)
            f.seek(start * BLOCK_SIZE)
            f.write(DAMAGE_BYTE * (count * BLOCK_SIZE))
            manifest.append(
                {
                    "file": name,
                    "start_block": start,
                    "block_count": count,
                    "bytes": count * BLOCK_SIZE,
                }
            )
    (backups_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return image_path, backups_dir
