# 首次加载基准：2026-11-10

## 结论和范围

以一次全新浏览器上下文打开首页，事件数据下载、SHA256 校验和页面首次渲染完成时的 DOM 状态作为「事件就绪」；它不是地图底图加载完成，也不是屏幕绘制完成。浏览器和 CPU 使用本机 Chromium 156.0.8078.73、AMD Ryzen 7 H 255，不模拟手机 CPU；视口 1280×720。每次新建上下文且阻止 Service Worker，浏览器进程复用，因此不能视为完全冷启动的 DNS、TLS 和操作系统缓存。

为避免第三方站点波动，所有跨源请求（包括交大和 OSM 瓦片）被阻止；页面的校区几何可以显示，但地图影像、完整网络竞争和后续 GitHub API 状态不在计时内。真实用户的完整体验可能更慢或因第三方不可达而不完整。

测试数据来自线上 2026-11-10 04:12:30 UTC 生成的快照，共 977 条事件，`events.json` SHA256 为 `45a0637bc6468d8509a0b890eb09f2ad2cb3f9b17eb7a14efaa40d28457d1638`。线上部署和本地工作区代码不同；下列两组不能视为同一代码的严格 A/B 测试。工作区基于 `1b78451` 且含未提交修改，其中 `deploy/app.mjs` 的 SHA256 为 `fc15477d6cdff3ee7154e6aa838b4d5d616143901d10fb8c25757af997fb3742`、`deploy/data.mjs` 为 `51bc05afcc92fc8a5062ca9220a26203f2aa3fc7f614f5f132af488983ae585f`。

## GitHub Pages 路径测量

当前环境通过 `127.0.0.1:10808` 本地代理访问线上，实际出口位置和用户接入网络未知。对线上 `events.json` 顺序发起五次新 curl 请求，接受 gzip；返回 HTTP 200、`Content-Encoding: gzip`，每次传输 626,912 字节（解压后 5,359,695 字节）。下面有效传输速率按 `压缩字节 × 8 / (总时间 − 首字节时间)` 计算，不含连接、TLS 和首字节等待；不是 Pages 的带宽上限。缓存状态和路由可能改变结果。

| 次数 | 首字节 s | 总时间 s | 有效传输 Mbps |
| --- | ---: | ---: | ---: |
| 1 | 1.232 | 3.060 | 2.74 |
| 2 | 1.444 | 2.757 | 3.82 |
| 3 | 1.354 | 2.883 | 3.28 |
| 4 | 1.316 | 4.209 | 1.73 |
| 5 | 1.302 | 2.356 | 4.76 |

样本中位数为 **3.28 Mbps**，首字节时间中位数 **1.316 s**。此处的首字节时间包含本地代理、建连和 TLS，不能当作 RTT。样本量少、时间和地点单一，无法估计上海用户的分位数或 Pages 的普遍速度。

线上已部署版本的浏览器事件就绪三次为 **6.87、6.72、4.40 s**，中位数 **6.72 s**；对应 FCP 为 2.67、1.66、1.65 s。第一次与一次下载测试有重叠。实际网络波动、代理、线上资源和旧代码均包含在内；地图瓦片仍然被阻止。

## 当前工作区的受控模拟

本机 HTTP 服务器提供当前 `deploy/` 和上述快照；依据当前导出逻辑为快照生成 gzip 事件文件，生成的 `events.json.gz` 为 606,655 字节。服务器对文本模拟 Pages 的 HTTP gzip 压缩；HTTP/1.1、本机连接，不模拟真实 Pages 的 DNS、TLS、CDN 和代理延时。网络限制使用 Chromium DevTools 请求级模拟，非逐包网络仿真。两个限速场景均假定额外延迟 150 ms、上行 0.75 Mbps；其中 3.3 Mbps 下行由上面的单路径测量近似，150 ms 并非实测 RTT。1.6 Mbps / 150 ms 采用 [Lighthouse 的 Slow 4G 参考参数](https://github.com/GoogleChrome/lighthouse/blob/main/docs/throttling.md)，仅作较慢连接对照。

| 场景 | 校区地图 DOM 可用，三次 s | 事件就绪，三次 s | 事件就绪中位数 |
| --- | --- | --- | ---: |
| 不限速 | 0.534 / 0.222 / 0.223 | 0.742 / 0.324 / 0.322 | 0.324 s |
| 3.3 Mbps / 150 ms | 1.200 / 1.015 / 0.990 | 2.827 / 2.652 / 2.630 | 2.652 s |
| 1.6 Mbps / 150 ms | 1.152 / 1.108 / 1.115 | 4.428 / 4.427 / 4.420 | 4.427 s |

3.3 Mbps 场景 FCP 三次为 0.488 / 0.400 / 0.404 s；1.6 Mbps 场景为 0.392 / 0.388 / 0.404 s。FCP 主要是页眉，并不意味着事件已可用。三次均没有同源请求失败或页面异常。因 HTTP 压缩，当前线上旧版的事件传输已经约 627 KB，新版预压缩文件约 607 KB；仅凭原始 JSON 的 5.36 MB 估算下载时间会显著高估差异。样本内容及服务器压缩参数变化会改变体积。

## 提前请求的对照复测

使用上述同一快照和本机服务器，在修改前后各运行三次；每次新建浏览器上下文，阻止 Service Worker 和跨源请求，其余限制与上面的受控模拟相同。修改后在解析 HTML 时并行发起 manifest 和 gzip 请求，收到 manifest 后仍验证两个 SHA256；传输大小不变。以下时间为页面导航起算的 DOM 状态时间，不能代表真实网络或移动设备的耗时。

| 场景 | 修改前事件就绪 s | 修改后事件就绪 s | 中位数变化 |
| --- | --- | --- | ---: |
| 3.3 Mbps / 150 ms | 2.729 / 2.561 / 2.622 | 2.264 / 2.219 / 2.224 | 2.622 → 2.224 s（-15%） |
| 1.6 Mbps / 150 ms | 4.391 / 4.407 / 4.437 | 4.099 / 4.094 / 4.103 | 4.407 → 4.099 s（-7%） |

3.3 Mbps 下地图 DOM 可用中位数由 0.995 s 变为 0.870 s，较慢场景由 1.161 s 变为 1.120 s；FCP 中位数分别由 0.372 s 变为 0.372 s、由 0.376 s 变为 0.416 s。每组仅三次，波动和 CPU、网络模拟限制不支持推断真实用户分位数。所有样本均无同源请求失败或页面异常。

旧版 manifest 没有 gzip 文件时会取消提前请求并回退到 JSON；若提前请求失败会重新读取，若提前数据与 manifest 不符则按发布切换重试。旧版站点仍可能收到一次无用的 gzip 请求。

## 关键路径拆解

在上述同一快照上，用临时浏览器探针记录一次 3.3 Mbps / 150 ms 运行：事件 gzip 请求从导航后 0.194 s 到 2.103 s，耗时 1.909 s；事件就绪为 2.267 s。下载后浏览器解压约 26 ms、压缩前后 SHA256 合计约 12 ms、JSON 解析约 30 ms；剩余时间包括文本解码、数据验证和界面工作。这是桌面 CPU 的单次观察，处理步骤的计时可能受调度和探针影响，不能推断手机上的成本。

压缩文件 606,655 字节占受控测试中主要同源传输量。用同一快照做离线体积实验，移除每个事件的 `visibility` 后重新压缩得到 289,471 字节；这是不同文件整体压缩后的对比，并不表示可以直接移除该字段，它供地图、位置筛选和提醒使用。将原数据编码成 MessagePack 再 gzip 得到约 501,974 字节（约减少 17%），只是格式实验，尚未实现客户端解码或进行端到端计时。

同一原始 JSON 经 Brotli quality 11 压缩为 411,164 字节，但当前部署的 Pages 对 `events.json` 的 `Accept-Encoding: br, gzip` 请求返回 gzip；本次 Chromium 的 `DecompressionStream` 也不支持 Brotli。因而这个离线体积不能直接当作当前静态站可实现的下载速度。

## 复现

安装 Node.js、Chromium 和 Playwright（如不使用系统 Chromium，先执行 `npx playwright install chromium`）：

```bash
npm install --no-save --no-package-lock playwright
```

准备同时包含 `manifest.json`、`events.json`、`campus.geojson` 和 `weather.json` 的同一批快照，例如构建后的 `dist/data`；也可从线上下载一批，注意发布恰好切换时哈希会不匹配，应重新下载整批：

```bash
mkdir -p /tmp/transit-bench-data
for name in manifest.json events.json campus.geojson weather.json; do
  curl -fsSL "https://beblugger.github.io/sjtu-transit-predictor/data/$name" -o "/tmp/transit-bench-data/$name"
done
BENCH_DATA=/tmp/transit-bench-data CHROME_PATH=/usr/bin/chromium-browser \
  node scripts/benchmark_first_load.cjs > /tmp/transit-bench.jsonl
```

省略 `BENCH_DATA` 默认使用 `dist/data`；设置 `BENCH_RUNS=5` 和 `BENCH_PROFILES=measured,slow4g` 可调整重复次数及场景。`CHROME_PATH` 可省略以使用 Playwright 安装的 Chromium。输出是逐次 JSON，包括 DOM 时间、FCP、请求传输量和失败信息；快照哈希错误或请求失败会返回非零退出码。脚本会在内存中合成 gzip 与 manifest，不修改快照。

测试实际线上部署（保留跨源阻止，网络条件由本机决定）：

```bash
BENCH_URL=https://beblugger.github.io/sjtu-transit-predictor/ \
  BENCH_PROFILES=unthrottled BENCH_RUNS=3 CHROME_PATH=/usr/bin/chromium-browser \
  node scripts/benchmark_first_load.cjs
```

复测传输速率时，请在没有并发大流量请求的情况下顺序运行下面命令，`size_download` 为传输字节，单位为字节而非比特：

```bash
for i in 1 2 3 4 5; do
  curl -sS -L --compressed -o /dev/null \
    -w '%{http_code} %{size_download} %{time_starttransfer} %{time_total}\n' \
    https://beblugger.github.io/sjtu-transit-predictor/data/events.json
done
```
