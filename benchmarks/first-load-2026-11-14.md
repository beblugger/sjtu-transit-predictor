# 首次加载基准：2026-11-14

## 首屏只加载首批事件（最终）

本轮把发布页面改为一个内联首屏包：HTML 内含样式、应用脚本、首屏索引和生成时刻的 10 条事件。正常打开只请求文档，不请求 `events.json`、`events.columns.json.gz` 或 `weather.json`；列表、地图选中区域和详情在首屏完成。只有筛选、翻页、定位、通知或外部事件链接才读取完整目录。快照过期时，HTML 解析阶段只预取一个经 SHA256 校验的小批次。

同一份 1166 条事件快照、每场景 5 次，结果如下：

| 场景 | 首屏中位数（两帧后） | DOM 标记中位数 | FCP 中位数 | 严格 0.5 s |
| --- | ---: | ---: | ---: | --- |
| 不限速本地 | 106 ms | 70 ms | — | 4 / 5 |
| 3.3 Mbps / 150 ms | 371 ms | 340 ms | 208 ms | 5 / 5 |
| 1.6 Mbps / 150 ms | 525 ms | 488 ms | 260 ms | 0 / 5 |

- 每次首屏都显示 10 条事件、地图选中区域和选中项详情；`result-count` 为 `174 / 1166`，没有完整快照请求。
- 3.3 Mbps 下逐次首屏为 373.9 / 369.6 / 373.7 / 371.2 / 365.8 ms。1.6 Mbps 下为 518.6 / 525.7 / 522.5 / 524.7 / 527.2 ms；DOM 标记已低于 0.5 s，但“两帧后”目标还差约 20–27 ms，主要受 59 KB 文档在 1.6 Mbps 下的传输时间限制。
- 不限速组第 1 次冷启动时，两帧回调被推迟到 523.7 ms，而 DOM 标记仅 111.2 ms；其余 4 次为 103.9–108.1 ms。这里保留原始样本，不删掉慢样本。
- 首屏后点击非边缘行仍停留在小批次；翻页、筛选、定位、通知和外部事件链接会读取一次完整快照。过期访问只读取一个 `first-screen-*.json`，不读取完整事件目录。
- 首屏文档 gzip 为 59,023 字节，其中内联首屏索引约 11 KB、当前批次约 9 KB、应用脚本约 30 KB、样式约 4 KB（gzip 后）。没有把校区或天气拆成额外首屏请求。

```bash
NODE_PATH=/path/to/playwright/node_modules CHROME_PATH=/usr/bin/chromium-browser \
  BENCH_STAGE=firstscreen BENCH_RUNS=5 BENCH_PROFILES=unthrottled,measured,slow4g \
  BENCH_MAX_FIRSTSCREEN_MS=500 node scripts/benchmark_first_load.cjs
```

`BENCH_STAGE=firstscreen` 会构建内联首屏页面（`BENCH_BUILD=1` 自动开启），并禁止首屏期间请求完整事件文件。`BENCH_MAX_FIRSTSCREEN_MS` 只影响退出码和 `deadline_passes`，超过目标的样本仍计入中位数。

## 上一轮：整批事件就绪优化（已归档）

以下结果针对上一轮“仍读取整批事件、但压缩并提前请求”的实现，保留用于对照。其源码哈希、绝对时间和结论不代表当前内联首屏版本。

## 结果

用同一份 1166 条事件快照，比较已提交版本 `1b7845110483e886c1437c2179ba4851987f0444` 与本次最终工作区。事件就绪指标在事件数据校验、天气读取与默认筛选、列表 DOM 渲染完成后记录；新版提前显示的校区几何另行计时。两组每次都显示相同的 174 条晴天事件，避免拿空列表与真实事件渲染比较。

| 场景 | 修改前事件就绪中位数 | 修改后事件就绪中位数 | 缩短 |
| --- | ---: | ---: | ---: |
| 3.3 Mbps / 150 ms | 3.048 s | 2.010 s | 34.1% |
| 1.6 Mbps / 150 ms | 5.112 s | 3.694 s | 27.7% |

每种场景、每个版本各 5 次，下面保留逐次事件就绪时间：

| 场景 | 修改前，s | 修改后，s |
| --- | --- | --- |
| 3.3 Mbps / 150 ms | 3.069 / 3.051 / 3.029 / 3.048 / 3.038 | 2.033 / 2.013 / 2.010 / 2.010 / 2.006 |
| 1.6 Mbps / 150 ms | 5.129 / 5.119 / 5.103 / 5.112 / 5.099 | 3.689 / 3.694 / 3.691 / 3.696 / 3.704 |

| 场景 | 校区几何 DOM 中位数，修改前 → 后 | 加载及缓存保存完成中位数，修改前 → 后 | FCP 中位数，修改前 → 后 |
| --- | ---: | ---: | ---: |
| 3.3 Mbps / 150 ms | 3.048 → 0.955 s | 3.048 → 2.107 s | 0.380 → 0.376 s |
| 1.6 Mbps / 150 ms | 5.112 → 1.118 s | 5.112 → 3.792 s | 0.384 → 0.412 s |

补充不限速本地对照，每组 3 次：事件就绪中位数为 0.272 → 0.156 s；包含缓存保存的完成时间为 0.272 → 0.269 s。该组主要体现桌面本机处理及调度，不能代表网络用户首屏。

事件文件的实际响应体由修改前 HTTP gzip 的 **707,025 字节**减少到 columns gzip 的 **580,896 字节**，减少 17.8%。完整 `events.json` 仍为 6,334,701 字节，继续供科学审计和旧客户端使用。这里比较压缩传输体积，未用原始 JSON 体积估算网络收益。

全部 20 次受控样本均无页面异常和同源请求失败。提前请求、并行读取天气与校区、列编码及可重建的可见区表示、延后建筑轮廓与 Service Worker 安装一起构成本轮变化，因此该结果衡量整组修改的效果，不能单独归因到一种编码。

## 测量边界

- Chromium 156.0.8078.73，AMD Ryzen 7 H 255，1280×720；不模拟移动设备 CPU。每次创建新浏览器上下文，关闭 HTTP 缓存并阻止 Service Worker；浏览器进程复用。
- 使用本地 HTTP/1.1 服务器与 CDP 请求级网络模拟；上行为 0.75 Mbps，两种场景均额外延迟 150 ms。它们是固定对照参数，不代表实测上海 RTT 或 GitHub Pages 带宽。
- 文本文件使用 HTTP gzip，显式 `.gz` 文件直接传输。数据文件在服务器启动时预压缩，避免把每次临时压缩大 JSON 的服务端 CPU 时间算入旧版首屏。压缩参数可能与真实 Pages 不同。
- 阻止全部跨源请求，包括交大与 OSM 瓦片。校区几何 DOM 时间要求实际 SVG 路径出现；它不包含底图影像完成、屏幕最终绘制或后续 GitHub API 状态。FCP 主要对应页眉，不能当作事件就绪。
- `ready` 在“数据校验通过”对应的 DOM 状态记录；`settled` 再等待加载按钮恢复，包含本地快照保存。新版缓存保存移出事件显示前的关键路径，所以同时报告这两个时间。
- 快照已过期，浏览器墙上时间默认从其 `generated_at` 开始正常前进，以实际渲染未来事件；`performance.now()` 不变。两组使用相同墙上时间起点和完整事件。
- 未模拟冷 DNS、TLS、真实 CDN、第三方网络竞争；5 个样本不能推断真实用户分位数。此处是同数据、本机受控 A/B，不是线上部署后的用户速度。

## 快照与源码

- 快照生成时间：`2026-11-04T14:58:27+00:00`；1166 条事件。
- 完整 `events.json` SHA256：`6f6cef38cd664132edf14bae347d11e8809493f0ce970abe3d55c8af84f00dab`。
- 最终 `app.mjs` SHA256：`14ef8131afb9c8b7052bdd1eb2103249823a1064f92e4ca6b5576af7e9662b6d`。
- 最终 `data.mjs` SHA256：`f404cf19258cbe2f30dafcd221702f4fa06c272c1ebada9cd7c37bead26c1792`。
- 最终 `bands.mjs` SHA256：`031742eaa1216f6630a942f9eafd59c3a36025d8def456f32be6bba6772a5e04`。
- 最终 `core.mjs` SHA256：`40fd795a302e69a3d587fcb510a464451b98596cd85d2d72e1105f7e0bf1b47a`。
- 最终 `index.html` SHA256：`6c05f524984dd6360c4f7100dc055b3f4b091d0b2b58703ed1fbd8459168845c`。

## 复现

需要 Python 项目依赖、Node.js、Playwright 和 Chromium。可将 Playwright 装到临时目录，避免给源码目录增加依赖文件：

```bash
npm install --prefix /tmp/transit-bench-tools --no-package-lock --no-save playwright
export NODE_PATH=/tmp/transit-bench-tools/node_modules
export CHROME_PATH=/usr/bin/chromium-browser
```

如没有系统 Chromium，使用 Playwright 安装浏览器并省略 `CHROME_PATH`。准备包含同一批 `manifest.json`、`events.json`、`campus.geojson`、`weather.json` 的快照；当前默认目录是 `dist/data`。在线数据会滚动更新，未来快照的数值不必与本报告相同。脚本先校验原文件 SHA256，再在内存中生成所选浏览器格式，不修改快照。

```bash
mkdir -p /tmp/transit-bench-head
git archive 1b7845110483e886c1437c2179ba4851987f0444 deploy | tar -x -C /tmp/transit-bench-head
BENCH_SOURCE=/tmp/transit-bench-head/deploy BENCH_FORMAT=json BENCH_RUNS=5 \
  BENCH_PROFILES=measured,slow4g node scripts/benchmark_first_load.cjs > /tmp/transit-head.jsonl
BENCH_FORMAT=columns BENCH_RUNS=5 BENCH_PROFILES=measured,slow4g \
  node scripts/benchmark_first_load.cjs > /tmp/transit-final.jsonl
```

两组顺序运行，不要并发基准任务。输出包括源文件 SHA256、快照版本与大小、逐次 DOM 时间、实际事件行数、请求体积和各场景中位数；页面异常、同源请求失败或主数据校验错误导致非零退出码。

可用环境变量：

| 变量 | 含义 |
| --- | --- |
| `BENCH_DATA` | 快照目录，默认 `dist/data` |
| `BENCH_SOURCE` | 固定前端目录，默认 `deploy` |
| `BENCH_FORMAT` | `json`、`gzip` 或 `columns`，默认 `columns` |
| `BENCH_NOW` | 浏览器墙上时间起点，本地默认快照生成时刻 |
| `BENCH_RUNS` | 每种场景的重复次数，默认 3 |
| `BENCH_PROFILES` | `unthrottled`、`measured`、`slow4g`，逗号分隔 |
| `BENCH_LABEL` | 写入输出的版本标签 |
| `BENCH_STAGE` | `firstscreen` 或 `full`，默认 `full`；`firstscreen` 自动构建内联首屏并禁止完整事件请求 |
| `BENCH_MAX_FIRSTSCREEN_MS` | 首屏截止时间；超时只标记失败，样本仍计入中位数 |
| `BENCH_MIN_FIRSTSCREEN_ROWS` | 首屏最少事件行数，默认 1 |
| `BENCH_FORBID_FULL=1` | 即使 `BENCH_STAGE=full` 也禁止首屏期间读取完整事件文件 |
| `BENCH_SERVE_ONLY=1` | 仅启动 fixture 服务，打印随机本地 URL 后保持运行；Ctrl+C 停止 |
| `BENCH_URL` | 测线上部署；使用真实墙上时间，除非另设 `BENCH_NOW` |

`BENCH_SERVE_ONLY` 会缓存 gzip 资源；修改前端后需要重启服务。其 `clock` 输出只供调用者参考，不会自动修改另开浏览器的日期。

早期实验及其测量条件保留于 [2026-11-10 基准](first-load-2026-11-10.md)，不能将其中不同线上代码、不同快照的结果混入本次 A/B。
