# 虚拟机系统盘分段修复台

虚拟机系统盘（Raw 镜像）需要按段从备份补回数据。本服务提供一个网页控制台 +
REST API：工作器按 **(起始块号, 块数)** 领取租约（每块 **4 KiB**），在租约有效期内
上传与该区间**等长的完整备份段**；服务端提交时按 4 KiB 逐块与当前盘面比对——
**相同块跳过**（不写回镜像、不记回滚日志），只把差异块**稀疏原子提交**进镜像，
响应与网页显示**改写/跳过块数**；修复完成后可下载整个 Raw 镜像。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

首次启动自动生成 8 MiB 样例盘（含 3 段 0xEE "损坏"区间）和对应的演示备份段。
数据保存在命名卷 `repair-data` 中，重启后修复成果保留。

### 本地运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --port 8000
```

### 重新生成样例盘

```bash
python3 scripts/make_sample_image.py --data-dir data --size-mib 8 \
    --damage "64:8,512:16,1500:4"
```

## 使用方式（网页）

1. **① 服务器备份段**：样例盘出厂自带 3 段待回填区间，点"领取并回填"即自动
   领取租约 → 拉取备份 → 上传提交。把"慢速上传"调到 200~500 ms/64 KiB，
   上传期间对另一段重复操作，即可看到**交错回填**（互不相交，互不阻塞）。
2. **② 手动领取租约**：输入起始块/块数/TTL，领取后选择本地备份文件上传。
3. **④ 按块回读**：输入块号查看 4 KiB 十六进制转储，核对修复结果。
4. 页首可随时**下载 Raw 镜像**（流式读取，修复某段无需复制整盘）。

## 规则与语义

| 规则 | 实现 |
| --- | --- |
| 有效租约区间不重叠 | 领取时检查所有"有效"（未过期/未提交/未释放）租约，重叠返回 `409` |
| 慢上传不阻塞他人 | 上传只写自己的暂存文件，不持全局锁；提交锁仅包住"校验+写镜像"一小段 |
| 到期可重领 | 过期租约不阻挡新领取 |
| 先开传、提交时已过期 → 拒绝 | 接收每个分块时检查过期；收满后提交前在提交锁内**再次**检查，过期返回 `410` |
| 一次提交整段生效 | 数据收满且长度恰好等于 `块数×4096` 才进入提交，否则拒绝（`400`）；即使差异块互不相邻，也在同一次提交里一起生效 |
| 相同块不写回 | 提交时按 4 KiB 逐块与当前盘面比对：相同块既不写镜像也不进 journal；响应返回 `blocks_compared / blocks_written / blocks_skipped` |
| 内存不随段长增长 | 上传与提交全程流式，驻留内存的只有少量 4 KiB 缓冲；journal 只存差异块的写前字节，体积随差异块数而非段长增长 |
| 传输中断保留原字节 | 中断的传输不会进入提交，暂存文件被清理，镜像原字节不变 |
| 落盘失败撤销全部改写 | 块级 journal 协议：扫描遍武装 journal（差异块写前字节 + crc32 校验）→ 改写遍只 pwrite 差异块 → fsync → 删 journal；任何写失败即按 journal 回滚本次全部块；回滚也失败则保留 journal，**重启时自动恢复** |
| 提交未完成就退出 | 扫描遍崩溃只留 `*.jrn.tmp`（镜像未动，启动直接清理）；改写遍崩溃留下武装好的 `*.jrn`，启动时把其中写前字节还原；**已提交（journal 已删除）的改动不回退** |
| 兼容旧版整段日志 | 旧版 `*.orig`（整段原始字节）journal 仍可在启动时按文件名偏移原样还原 |
| 修一段无需复制整盘 | `pwrite` 原地写差异块；下载为流式读取 |

## 块级 journal 文件格式（`data/journal/`）

每次提交最多产生两个文件，文件名携带租约 id 与区间偏移/长度：

| 文件 | 含义 |
| --- | --- |
| `{lease}_{offset}_{length}.jrn.tmp` | 扫描遍临时文件，rename 前的任何崩溃都只留下它，启动即清理 |
| `{lease}_{offset}_{length}.jrn` | 武装完成的块级 journal：提交点之前一直存在，改写遍崩溃后靠它恢复 |

`.jrn` 为二进制格式：16 字节 header（魔数 `BJN1`、块大小、起始块号）+
N 个条目（u32 块号 + 该块 4 KiB **写前字节**，N = 差异块数）+ 12 字节 trailer
（魔数 `JEND`、条目数、覆盖 header 与全部条目的 crc32）。启动恢复时先校验
魔数、长度与 crc，任一不符则保留文件等人工处理；校验通过才逐块还原。
旧版 `{lease}_{offset}_{length}.orig`（整段原始字节，无格式头）仍被识别并还原。

## REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/info` | 镜像大小/块数/块大小/默认 TTL/启动恢复数 |
| POST | `/api/leases` | 领取租约 `{start_block, block_count, ttl_seconds?}` → `201`；重叠 `409`，越界 `400` |
| GET | `/api/leases` | 租约列表（含状态与剩余秒） |
| DELETE | `/api/leases/{id}` | 释放租约 |
| PUT | `/api/leases/{id}/data` | 上传等长完整备份段并按块稀疏提交；返回 `blocks_compared/blocks_written/blocks_skipped`；过期 `410`，长度不符 `400`，未知租约 `404`，落盘失败 `500`（已自动回滚） |
| GET | `/api/image/blocks/{i}` | 按块回读 4 KiB；越界 `404` |
| GET | `/api/image/download` | 流式下载整个 Raw 镜像 |
| GET | `/api/backups` · `/api/backups/{name}` | 演示备份段清单与下载 |

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `data`（容器内 `/data`） | 镜像、暂存、journal、备份段所在目录 |
| `IMAGE_SIZE_MIB` | `8` | 样例盘大小（仅首次生成时生效） |
| `LEASE_TTL_SECONDS` | `60` | 默认租约 TTL（领取时可覆盖，0.1~3600） |
| `DEMO_DAMAGE` | `64:8,512:16,1500:4` | 出厂"损坏"区间（仅首次生成时生效） |

## 自测

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -q          # 18 项单元/集成测试
python3 tests/interleaved_demo.py             # 对运行中的服务做交错回填实战演练
```

`tests/test_api.py` 覆盖：领取/提交/回读、重叠 409、过期 410 与重领、长度不符 400、
越界 400/404、释放重领、**相同块全部跳过**、**非相邻差异块一起提交**、
**journal 体积只随差异块增长**、**峰值内存不随段长增长（tracemalloc）**、
**落盘失败回滚**、**改写途中崩溃的重启恢复（已提交改动不回退）**、
**扫描残留 `*.jrn.tmp` 启动清理**、**旧版 `*.orig` journal 兼容恢复**、整盘下载。

`tests/interleaved_demo.py`（仅标准库）对运行中的服务演练：A 慢速上传期间 B 提交、
重叠拒绝、过期拒绝+重领、传输中断保留原字节、整盘下载抽查，以及 F 重复提交相同段
（0 改写 / 全部跳过）和单块差异的稀疏提交统计。

## 目录结构

```
app/
  core.py            租约管理 + 按块比对的稀疏 journal 原子提交（核心逻辑）
  sample.py          样例盘生成（pattern + 损坏区间 + 备份段导出）
  main.py            FastAPI 路由与配置
  static/index.html  网页控制台（无外部依赖）
scripts/make_sample_image.py   重新生成样例盘
tests/test_api.py              pytest 单元/集成测试
tests/interleaved_demo.py      交错回填实战演练
Dockerfile / compose.yaml      一体化部署
```

## 已知限制 / 待办

- **单进程**：租约表在内存中，多 worker/多实例部署需要把租约表移到 Redis
  等共享存储（镜像文件本身的 journal 协议不受影响）。
- **重启即租约清空**：进程重启后未提交的租约丢失（镜像数据与 journal 恢复
  不受影响）；如需续传可持久化租约。
- **无认证**：假定运行在内网/受信环境，生产使用需加认证与鉴权。
- 网页慢速上传依赖浏览器 `fetch` 流式请求体（`duplex: 'half'`），
  不支持时自动退回普通上传（无节流）。
