#!/usr/bin/env python3
"""交错回填实战演练：对运行中的服务驱动与网页完全相同的 REST API。

场景：
  A. 交错回填：租约 A 慢速上传期间，不相交的租约 B 照常提交成功
  B. 重叠拒绝：与有效租约重叠的领取返回 409
  C. 过期拒绝：上传先开传、提交时已过期 -> 410，随后可重领并成功
  D. 传输中断：上传中途断连，该段原字节保持不变
  E. 整盘下载：修复后下载 Raw 镜像，抽查回填结果

用法：python3 tests/interleaved_demo.py [BASE_URL]   （默认 http://127.0.0.1:8000）
仅用标准库，退出码非 0 表示有失败项。
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from urllib.parse import urlparse

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
_u = urlparse(BASE)
HOST, PORT = _u.hostname, _u.port or 80

FAILURES: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({extra})" if extra else ""))
    if not cond:
        FAILURES.append(name)


def req(method: str, path: str, body: bytes | None = None,
        headers: dict | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def req_json(method: str, path: str, payload: dict) -> tuple[int, dict]:
    status, data = req(method, path, json.dumps(payload).encode(),
                       {"Content-Type": "application/json"})
    return status, json.loads(data)


def slow_put(path: str, payload: bytes, chunk: int, pause: float,
             result: dict, skip_first: int = 0) -> None:
    """后台线程：分块慢速 PUT。skip_first>0 时只发前 N 字节就断连（中断演练）。"""
    try:
        conn = http.client.HTTPConnection(HOST, PORT, timeout=30)
        total = skip_first if skip_first else len(payload)
        conn.putrequest("PUT", path)
        # Content-Length 始终声明完整长度；中断时少发字节，服务器应察觉断连
        conn.putheader("Content-Length", str(len(payload)))
        conn.endheaders()
        sent = 0
        while sent < total:
            piece = payload[sent:sent + chunk]
            conn.send(piece)
            sent += len(piece)
            time.sleep(pause)
        if skip_first:
            # 模拟传输中断：直接断开，不发剩余字节
            conn.close()
            result["status"] = -1
            return
        resp = conn.getresponse()
        result["status"] = resp.status
        result["body"] = resp.read()
        conn.close()
    except Exception as exc:  # 服务器提前关闭连接（如过期拒绝）时读响应
        result["error"] = exc
        try:
            resp = conn.getresponse()
            result["status"] = resp.status
            result["body"] = resp.read()
        except Exception:
            pass


def main() -> int:
    print(f"== 目标 {BASE} ==")
    status, info_raw = req("GET", "/api/info")
    info = json.loads(info_raw)
    print(f"镜像 {info['image_size_bytes']} 字节, {info['total_blocks']} 块, "
          f"块大小 {info['block_size']}")

    _, backups_raw = req("GET", "/api/backups")
    backups = json.loads(backups_raw)["backups"]
    b1, b2 = backups[0], backups[1]
    _, data1 = req("GET", f"/api/backups/{b1['file']}")
    _, data2 = req("GET", f"/api/backups/{b2['file']}")

    print("\n== A. 交错回填：慢上传不阻塞不相交区间 ==")
    s, lease_a = req_json("POST", "/api/leases",
                          {"start_block": b1["start_block"],
                           "block_count": b1["block_count"], "ttl_seconds": 30})
    check("领取租约 A", s == 201, f"[{lease_a['start_block']}, {lease_a['end_block']})")
    res_a: dict = {}
    t = threading.Thread(target=slow_put,
                         args=(f"/api/leases/{lease_a['id']}/data", data1, 4096, 0.25, res_a),
                         daemon=True)
    t.start()
    time.sleep(0.6)  # 确保 A 正在慢速上传中

    s, lease_b = req_json("POST", "/api/leases",
                          {"start_block": b2["start_block"],
                           "block_count": b2["block_count"], "ttl_seconds": 30})
    check("A 上传期间领取不相交租约 B", s == 201)
    s, body = req("PUT", f"/api/leases/{lease_b['id']}/data", data2)
    check("A 上传期间 B 提交成功", s == 200, f"HTTP {s}")
    t.join(timeout=30)
    check("A 慢速上传最终提交成功", res_a.get("status") == 200,
          f"HTTP {res_a.get('status')}")

    _, blk = req("GET", f"/api/image/blocks/{b1['start_block']}")
    check("A 区间首块 == 备份数据", blk == data1[:4096])
    _, blk = req("GET", f"/api/image/blocks/{b2['start_block'] + b2['block_count'] - 1}")
    check("B 区间末块 == 备份数据", blk == data2[-4096:])

    print("\n== B. 重叠拒绝 ==")
    s, lease_c = req_json("POST", "/api/leases",
                          {"start_block": 1800, "block_count": 4, "ttl_seconds": 20})
    check("领取租约 C [1800,1804)", s == 201)
    s, body = req_json("POST", "/api/leases",
                       {"start_block": 1802, "block_count": 4, "ttl_seconds": 20})
    check("重叠领取被拒绝", s == 409, f"HTTP {s}: {body.get('detail', '')[:60]}")
    s, _ = req("DELETE", f"/api/leases/{lease_c['id']}")
    check("释放租约 C", s == 200)
    s, lease_d = req_json("POST", "/api/leases",
                          {"start_block": 1802, "block_count": 4, "ttl_seconds": 20})
    check("释放后可领取原重叠区间", s == 201)
    req("DELETE", f"/api/leases/{lease_d['id']}")

    print("\n== C. 过期拒绝 + 重领 ==")
    s, lease_e = req_json("POST", "/api/leases",
                          {"start_block": 1810, "block_count": 2, "ttl_seconds": 2})
    check("领取短 TTL 租约 E", s == 201)
    res_e: dict = {}
    # 先开传，拖到过期后才发完（第 2 块 ~2.2s 时到达，TTL 只有 2s）-> 提交时必须拒绝
    t = threading.Thread(target=slow_put,
                         args=(f"/api/leases/{lease_e['id']}/data", b"\x55" * 8192,
                               4096, 2.2, res_e),
                         daemon=True)
    t.start()
    t.join(timeout=30)
    check("过期租约的提交被拒绝", res_e.get("status") == 410,
          f"HTTP {res_e.get('status')}")
    _, blk_before = req("GET", "/api/image/blocks/1810")
    s, lease_e2 = req_json("POST", "/api/leases",
                           {"start_block": 1810, "block_count": 2, "ttl_seconds": 20})
    check("过期后同一区间可重领", s == 201)
    s, _ = req("PUT", f"/api/leases/{lease_e2['id']}/data", b"\x55" * 8192)
    check("重领后提交成功", s == 200)
    _, blk_after = req("GET", "/api/image/blocks/1810")
    check("重领提交后数据生效", blk_after == b"\x55" * 4096 and blk_after != blk_before)

    print("\n== D. 传输中断保留原字节 ==")
    _, blk_orig = req("GET", "/api/image/blocks/1820")
    s, lease_f = req_json("POST", "/api/leases",
                          {"start_block": 1820, "block_count": 2, "ttl_seconds": 20})
    check("领取租约 F", s == 201)
    res_f: dict = {}
    t = threading.Thread(target=slow_put,
                         args=(f"/api/leases/{lease_f['id']}/data", b"\x99" * 8192,
                               4096, 0.1, res_f, 4096),  # 只发一半就断
                         daemon=True)
    t.start()
    t.join(timeout=30)
    time.sleep(0.8)  # 等服务器察觉断连并清理
    _, blk_now = req("GET", "/api/image/blocks/1820")
    check("中断后该段原字节不变", blk_now == blk_orig)
    # 租约 F 还在（未提交），释放后重领
    for _ in range(20):
        s, _ = req("DELETE", f"/api/leases/{lease_f['id']}")
        if s == 200:
            break
        time.sleep(0.3)
    check("释放中断的租约 F", s == 200, f"HTTP {s}")
    s, lease_f2 = req_json("POST", "/api/leases",
                           {"start_block": 1820, "block_count": 2, "ttl_seconds": 20})
    check("重领该区间", s == 201)
    s, _ = req("PUT", f"/api/leases/{lease_f2['id']}/data", b"\x99" * 8192)
    check("重新上传提交成功", s == 200)
    _, blk = req("GET", "/api/image/blocks/1821")
    check("重新提交后数据生效", blk == b"\x99" * 4096)

    print("\n== E. 整盘下载抽查 ==")
    s, image = req("GET", "/api/image/download")
    check("下载状态 200", s == 200)
    check("下载大小 == 镜像大小", len(image) == info["image_size_bytes"],
          f"{len(image)} 字节")
    off1 = b1["start_block"] * 4096
    check("下载内容中 A 区间 == 备份数据",
          image[off1:off1 + len(data1)] == data1)

    print()
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项: {FAILURES}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
