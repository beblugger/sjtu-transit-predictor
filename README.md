# 交大日月凌星预报

为上海交通大学闵行校区静态网站生成未来 96 小时的太阳、月球凌星事件、轨道目录和天气缓存。GitHub Actions 定时计算，GitHub Pages 发布数据。

- [数据入口](https://beblugger.github.io/sjtu-transit-predictor/)
- [数据版本与更新时间](https://beblugger.github.io/sjtu-transit-predictor/data/manifest.json)
- [构建记录与手动运行](https://github.com/beblugger/sjtu-transit-predictor/actions/workflows/backend.yml)

## 本地构建

需要 Python 3.10 或更新版本；线上使用 Python 3.14。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
mkdir -p ~/.cache/sjtu-transit
curl --fail --location https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp \
  --output ~/.cache/sjtu-transit/de421.bsp
python -m app.export --database .state/transit.db --output dist/data --hours 96
python scripts/validate_export.py --data dist/data
python -m pytest -q
```

配置使用 `TRANSIT_` 环境变量前缀，见 `.env.example`。轨道、天气无需 API 密钥。
保留 `.state/transit.db` 可复用数据和计算缓存；加 `--offline` 可完全使用已保存的数据。
生产构建校验 DE421 的 SHA256；本地缺少星历时使用精度较低的解析模型，并在导出中警告。

## 静态数据

所有文件位于网站的 `data/` 下，时间使用 UTC。基础前端提供 WGS84 校区凌星可见区域图、事件表、详情、时区切换、筛选及 CSV / JSON 下载。可见区域按角几何反解，并裁剪到校区；不是优选点缓冲圆。地图不使用第三方瓦片或坐标偏移，不需要地图 API 密钥。

| 文件 | 内容 |
| --- | --- |
| `manifest.json` | 生成时间、96 小时覆盖范围、上游时间、来源、数量、警告和文件 SHA256 |
| `events.json` | 凌星事件、观测位置、入凌/出凌时间、角距、卫星尺寸、轨道历元和天气评分 |
| `orbits.json` | 完整 OMM 轨道目录，支持六位及以上卫星编号 |
| `orbits.tle` | 编号不超过 99999 的兼容子集；遗漏数量记录在 manifest 中 |
| `weather.json` | 含原始获取时间和位置的确定性、ECMWF/GFS 集合天气缓存 |
| `campus.geojson` | 闵行校区观测范围 |

客户端可选择时间范围、天气条件，并使用轨道和天气数据进一步计算。导出保留所有几何事件，不按天气、地影或轨道历元超过六小时排除。超过时效会给出警告；天气缺失、过期或窗口不足时标为不可用。

## 更新与缓存

优先使用 CelesTrak active 目录，失败时回落到 `satvisorcom/satvisor-data` 社区 OMM 镜像，再使用最后有效快照。落后的镜像不能覆盖更新的缓存轨道。

主站请求至少间隔两小时。先读取 GP 列表页的 `Current as of`，与上次成功下载对应的发布时间比较；相同则跳过目录下载。发布时间仅在有效目录保存后持久记录。无法解析时间或 HTTP 出错时停止本轮主站请求并回落；未更新响应不会刷新原始下载时间。

几何预报按 UTC 六小时分块缓存。轨道内容、算法、依赖版本、星历、校区或计算参数变化会使缓存失效；窗口滚动时只补算新增时间段。每次导出重新计算天气评分，清理七天前的几何缓存。

## 自动部署

`.github/workflows/backend.yml` 在 main 推送、手动运行，以及北京时间每个偶数小时的 23 分触发。流程依次安装依赖、恢复状态、校验星历、运行测试、计算、独立验证几何结果，然后发布整批数据。失败时保留上次成功的 Pages 版本。

仅使用仓库自带的 `GITHUB_TOKEN`，无需自定义凭证。构建任务具有保存状态所需的 contents 写权限；部署任务具有 Pages 和 OIDC 写权限。工作流串行运行，顶层 Action 固定到提交版本。

Actions cache 保存几何计算和星历；独立 `state` 分支持久保存压缩后的轨道、天气、更新时间和请求记录。每次运行都会合并更新的持久记录，避免旧计算缓存丢失失败请求的两小时冷却。即使计算失败，也会尝试保存这些记录。

`state` 分支只保留最新快照对应的根提交，避免二进制数据库历史持续增长；main 历史不重写。旧版本状态中的废弃数据表会在打包时移除。缓存被清理后仍可恢复输入并重新计算。

GitHub 定时任务可能延迟或丢弃，公共仓库无活动达 60 天可能被停用。实际数据更新会写入 state 分支，但平台不保证永久运行；可查看构建记录和 `manifest.generated_at`，必要时手动运行工作流。

## 计算与验证

SGP4 传播轨道，DE421 计算日月位置。搜索采用 60 秒有限轨迹弧、100 米校区网格和 25 米局部细化。筛选使用日月位置插值，最终最小角距及接触时间使用完整星历重新求解。出入凌是在选定观测位置上求时间根，接触搜索允许向两侧各扩展一小时。

独立验证脚本使用 Skyfield 的卫星地平坐标计算，检查文件哈希、校区范围、时间覆盖及一角秒内的几何一致性。这不代表真实轨道预报误差，也不证明候选搜索没有遗漏。轨道外推和机动可能造成实际预报偏差。

## 数据来源

- [CelesTrak GP 数据与使用说明](https://celestrak.org/NORAD/documentation/gp-data-formats.php)
- [CelesTrak 使用政策](https://celestrak.org/usage-policy.php)
- [社区备用镜像](https://github.com/satvisorcom/satvisor-data)
- [Open-Meteo](https://open-meteo.com/)：免费接口用于非商业用途，天气数据按 CC BY 4.0 署名
- [JPL 星历](https://ssd.jpl.nasa.gov/planets/eph_export.html)

## 基础前端

`deploy/` 为无框架、无外部运行依赖的 HTML/CSS/ES modules。界面读取并核对 manifest 中的事件和校区文件 SHA256；发布切换导致不一致时重试一次，仍失败则明确报错。数据缺失、历史数据、天气不可用及未知尺寸均单独标注。

本地预览（先生成或下载 `dist/data`）：

```bash
cp deploy/*.html deploy/*.css deploy/*.mjs dist/
python -m http.server 8765 --directory dist --bind 127.0.0.1
node --test tests/frontend.test.mjs
```

访问 `http://localhost:8765`。无需 npm 安装；前端数据处理测试使用 Node.js 自带测试工具，Actions runner 已提供 Node。视觉规则和科学字段定义见网站 `reading.html`。不提供后台提醒或未量化的轨道误差带。参考位置处的区域判断、到区域距离和最近接时刻由导出的分片反解模型计算；列表中的原始精确时刻仍对应优选点。

## 可见区域反解

`app/footprints.py` 将二维角偏差对地面位置和时间一阶展开，消去时间，反解出凌星带及中心线，并使用 Shapely 与校区多边形相交。边界采样用完整 SGP4 / 日月星历回算；超过 5 m 数值目标时分片重新展开，无法收敛则构建失败。区域与局部时刻模型随每个事件的 `visibility` 导出并进入几何缓存。该区域是经过期间的可见位置集合，每个位置的事件时刻不同。

这个 5 m 目标只约束检查点的一阶近似偏差，不是全域严格上界，也不是轨道误差。独立审计对区域有效性、校区裁剪和导出的接触采样点使用 Skyfield 回算，允许原有一角秒实现差异加上局部展开的空间容差。无轨道协方差，因此不绘制 1σ / 3σ 误差带。
