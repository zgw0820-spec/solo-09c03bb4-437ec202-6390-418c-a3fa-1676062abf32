#!/usr/bin/env python3
"""重新生成样例 Raw 盘 + 演示备份段（会删除已有 data 目录内容）。

用法（在仓库根目录）：
    python3 scripts/make_sample_image.py [--data-dir data] [--size-mib 8] \
        [--damage "64:8,512:16,1500:4"]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.sample import DEFAULT_DAMAGE, ensure_sample_image, parse_damage  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data/）")
    parser.add_argument("--size-mib", type=int, default=8, help="镜像大小 MiB（默认 8）")
    parser.add_argument(
        "--damage",
        default=",".join(f"{s}:{c}" for s, c in DEFAULT_DAMAGE),
        help='损坏区间，形如 "64:8,512:16,1500:4"',
    )
    parser.add_argument("--keep", action="store_true", help="不清空已有数据目录")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if data_dir.exists() and not args.keep:
        shutil.rmtree(data_dir)
    image, backups = ensure_sample_image(data_dir, args.size_mib, parse_damage(args.damage))
    size = image.stat().st_size
    print(f"镜像:   {image}  ({size} 字节, {size // 4096} 块 x 4 KiB)")
    for f in sorted(backups.glob("segment_*.bin")):
        print(f"备份段: {f}  ({f.stat().st_size} 字节)")


if __name__ == "__main__":
    main()
