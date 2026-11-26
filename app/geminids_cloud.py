"""Geminids observation-night cloud-cover chart.

Fetches deterministic and ensemble cloud cover for a few candidate sites from
Open-Meteo (ECMWF, NOAA, DWD, UKMO, Meteo-France, JMA, CMA, ECCC), builds a
time-decayed weighted consensus plus ensemble probabilities, and renders one
poster-sized PNG.

Sources that do not reach the target night yet are drawn greyed out with their
last valid timestamp instead of being dropped: an empty cell means "no forecast
yet", never "no cloud".

Usage:
    python -m app.geminids_cloud --date 2026-12-12 --out charts
    python -m app.geminids_cloud --date 2026-12-02 --label "design sample" --font-dir ~/.fonts
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
USER_AGENT = "sjtu-transit-predictor/cloud-chart"

NIGHT_START_HOUR = 18
NIGHT_HOURS = 12
DECAY_DAYS = 6.0
CLEAR_THRESHOLD_PCT = 30.0
GOOD_WINDOW_SCORE = 70.0
# 少于这个小时数就不给"整夜"结论：3 小时的共识拿去排三地优劣是误导
MIN_CONSENSUS_HOURS = 6


@dataclass(frozen=True)
class Site:
    key: str
    name: str
    alias: str
    latitude: float
    longitude: float
    metar: str


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    org: str
    weight: float = 0.0
    members: int = 0


SITES: tuple[Site, ...] = (
    Site("dongtan", "东滩", "崇明东滩 · 长江口", 31.49, 121.05, "ZSPD"),
    Site("qdh", "千岛湖", "淳安 · 湖谷", 29.5139, 118.7708, "ZSHC"),
    Site("yld", "野鹿荡", "盐城 · 黄海滩涂", 32.875, 120.875, "ZSYN"),
)

DET_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("ecmwf_ifs025", "ECMWF IFS", "ECMWF", 1.00),
    ModelSpec("gfs_seamless", "GFS", "NOAA", 0.90),
    ModelSpec("icon_seamless", "ICON", "DWD", 0.90),
    ModelSpec("ukmo_seamless", "UM", "UKMO", 0.85),
    ModelSpec("meteofrance_seamless", "ARPEGE", "Météo-FR", 0.85),
    ModelSpec("jma_seamless", "GSM", "JMA", 0.80),
    ModelSpec("cma_grapes_global", "GRAPES", "CMA", 0.85),
    ModelSpec("gem_seamless", "GEM", "ECCC", 0.75),
)

ENS_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("ecmwf_ifs025", "EC-ENS", "ECMWF", members=51),
    ModelSpec("ncep_gefs025", "GEFS", "NOAA", members=31),
    ModelSpec("icon_global", "ICON-EPS", "DWD", members=40),
    ModelSpec("gem_global", "GEM-ENS", "ECCC", members=21),
)

# palette
C_BG_TOP = "#0d1524"
C_BG_BOT = "#070a12"
C_PANEL = "#101827"
C_PANEL_EDGE = "#1e2942"
C_RULE = "#1b2437"
C_TXT = "#eaeef7"
C_TXT_DIM = "#909ab1"
C_TXT_FAINT = "#59637d"
C_ACCENT = "#7fb2ff"
C_ACCENT_SOFT = "#4a7ec0"
C_WARN = "#e8b04b"
C_GOOD = "#5fd6a8"
C_BAD = "#e88a6b"
C_MISS = "#141c2e"
C_MISS_HATCH = "#25304a"
CLOUD_STOPS = ((0.00, "#0c1e3c"), (0.30, "#1c4a78"), (0.55, "#3f6f9f"),
               (0.78, "#7d9dc4"), (1.00, "#dde6f3"))


@dataclass
class Series:
    """Hourly cloud cover for one model at one site (ensemble mean when members exist)."""

    spec: ModelSpec
    kind: str
    total: dict[str, float] = field(default_factory=dict)
    low: dict[str, float] = field(default_factory=dict)
    mid: dict[str, float] = field(default_factory=dict)
    high: dict[str, float] = field(default_factory=dict)
    members: dict[str, list[float]] = field(default_factory=dict)
    last_valid: str | None = None
    error: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.total)

    @property
    def reason(self) -> str:
        if self.last_valid:
            return f"时效仅到 {self.last_valid[5:10].replace('-', '/')} {self.last_valid[11:16]}"
        return self.error or "无数据"


def night_slots(date: str) -> list[str]:
    """ISO local hours from 18:00 of `date` to 05:00 next morning."""
    day = dt.date.fromisoformat(date)
    slots = []
    for index in range(NIGHT_HOURS):
        hour = NIGHT_START_HOUR + index
        slots.append(f"{(day + dt.timedelta(days=hour // 24)).isoformat()}T{hour % 24:02d}:00")
    return slots


def _belongs(column: str, key: str) -> bool:
    """Open-Meteo suffixes ensemble columns with _ensemble / _eps when several are requested."""
    return any(column.endswith(suffix) for suffix in (f"_{key}", f"_{key}_ensemble", f"_{key}_eps"))


def fetch_site(client: httpx.Client, site: Site, slots: list[str]) -> tuple[list[Series], dict]:
    det = [Series(spec, "det") for spec in DET_MODELS]
    ens = [Series(spec, "ens") for spec in ENS_MODELS]
    meta: dict = {"endpoints": {}, "errors": []}

    params = {
        "latitude": site.latitude,
        "longitude": site.longitude,
        "hourly": "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high",
        "models": ",".join(spec.key for spec in DET_MODELS),
        "forecast_days": 16,
        "timezone": "Asia/Shanghai",
    }
    meta["endpoints"]["det"] = str(client.build_request("GET", FORECAST_URL, params=params).url)
    try:
        payload = client.get(FORECAST_URL, params=params).raise_for_status().json()
    except (httpx.HTTPError, ValueError) as exc:
        meta["errors"].append(f"deterministic: {type(exc).__name__}: {exc}")
    else:
        hourly = payload["hourly"]
        index = {stamp: position for position, stamp in enumerate(hourly["time"])}
        for series in det:
            columns = {name: hourly.get(f"{name}_{series.spec.key}")
                       for name in ("cloud_cover", "cloud_cover_low",
                                    "cloud_cover_mid", "cloud_cover_high")}
            if columns["cloud_cover"] is None:
                series.error = "接口未返回"
                continue
            for name, bucket in (("cloud_cover", series.total), ("cloud_cover_low", series.low),
                                 ("cloud_cover_mid", series.mid), ("cloud_cover_high", series.high)):
                column = columns[name]
                if column is None:
                    continue
                for slot in slots:
                    position = index.get(slot)
                    if position is not None and column[position] is not None:
                        bucket[slot] = float(column[position])
            valid = [stamp for stamp, value in zip(hourly["time"], columns["cloud_cover"])
                     if value is not None]
            series.last_valid = valid[-1] if valid else None
            if not series.total:
                series.error = "目标夜已超出时效"

    params = {
        "latitude": site.latitude,
        "longitude": site.longitude,
        "hourly": "cloud_cover",
        "models": ",".join(spec.key for spec in ENS_MODELS),
        "forecast_days": 16,
        "timezone": "Asia/Shanghai",
    }
    meta["endpoints"]["ens"] = str(client.build_request("GET", ENSEMBLE_URL, params=params).url)
    try:
        payload = client.get(ENSEMBLE_URL, params=params).raise_for_status().json()
    except (httpx.HTTPError, ValueError) as exc:
        meta["errors"].append(f"ensemble: {type(exc).__name__}: {exc}")
    else:
        hourly = payload["hourly"]
        index = {stamp: position for position, stamp in enumerate(hourly["time"])}
        for series in ens:
            columns = [name for name in hourly if _belongs(name, series.spec.key)]
            if not columns:
                series.error = "接口未返回"
                continue
            series.spec = ModelSpec(series.spec.key, series.spec.label, series.spec.org,
                                    members=len(columns))
            for slot in slots:
                position = index.get(slot)
                if position is None:
                    continue
                values = [hourly[name][position] for name in columns
                          if hourly[name][position] is not None]
                if len(values) >= max(3, len(columns) // 3):
                    series.members[slot] = [float(value) for value in values]
                    series.total[slot] = sum(values) / len(values)
            valid = [stamp for stamp, value in zip(hourly["time"], hourly[columns[0]])
                     if value is not None]
            series.last_valid = valid[-1] if valid else None
            if not series.members:
                series.error = "目标夜已超出时效"

    return det + ens, meta


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    pairs = sorted(pairs)
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return None
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= total / 2:
            return value
    return pairs[-1][0]


def star_score(total: float, low: float | None, high: float | None) -> float:
    """0-100 observability heuristic: low cloud blocks, high cloud costs transparency."""
    low_value = total if low is None else low
    high_value = total * 0.5 if high is None else high
    return 100 * (0.55 * (1 - total / 100) + 0.25 * (1 - low_value / 100)
                  + 0.20 * (1 - high_value / 100))


def probability_below(series: Series, slot: str, threshold: float) -> float | None:
    members = series.members.get(slot)
    if not members:
        return None
    return 100.0 * sum(1 for value in members if value < threshold) / len(members)


def best_window(slots: list[str], scores: dict[str, float], width: int = 3) -> tuple[tuple[str, str] | None, float]:
    best: tuple[str, str] | None = None
    best_value = -1.0
    for start in range(len(slots) - width + 1):
        window = [scores.get(slot) for slot in slots[start:start + width]]
        if any(value is None for value in window):
            continue
        value = sum(window) / width
        if value > best_value:
            best, best_value = (slots[start], slots[start + width - 1]), value
    return best, best_value


def lead_days(date: str, today: dt.date) -> int:
    return max(0, (dt.date.fromisoformat(date) - today).days)


def consensus_scores(det: list[Series], slots: list[str], target_date: str,
                     today: dt.date) -> tuple[dict[str, float], dict[str, float]]:
    """Time-decayed weighted median cloud cover, and the derived star score."""
    lead = 24.0 * lead_days(target_date, today)
    weights = {spec.key: spec.weight for spec in DET_MODELS}
    scored = [(series, weights[series.spec.key]
               * math.exp(-max(0.0, lead - 24) / (24 * DECAY_DAYS)))
              for series in det if series.total]
    consensus: dict[str, float] = {}
    scores: dict[str, float] = {}
    for slot in slots:
        pairs = [(series.total[slot], weight) for series, weight in scored if slot in series.total]
        value = weighted_median(pairs)
        if value is None:
            continue
        consensus[slot] = value
        lows = [series.low[slot] for series, _ in scored if slot in series.low]
        highs = [series.high[slot] for series, _ in scored if slot in series.high]
        scores[slot] = star_score(value,
                                  sum(lows) / len(lows) if lows else None,
                                  sum(highs) / len(highs) if highs else None)
    return consensus, scores


def build_rows(det: list[Series], ens: list[Series], consensus: dict[str, float]) -> list[dict]:
    rows = [dict(kind="det", label=series.spec.label, n=0, active=series.active,
                 values=series.total, reason=series.reason) for series in det]
    rows += [dict(kind="ens", label=series.spec.label, n=series.spec.members,
                  active=bool(series.members), values=series.total, reason=series.reason)
             for series in ens]
    rows.append(dict(kind="cons", label="共识", n=0, active=bool(consensus),
                     values=consensus, reason="无有效共识"))
    return rows


# ---------------------------------------------------------------- rendering
def register_fonts(font_dir: Path | None) -> list[str]:
    from matplotlib import font_manager

    families: list[str] = []
    if font_dir and font_dir.is_dir():
        for path in sorted(font_dir.glob("*.otf")) + sorted(font_dir.glob("*.ttf")):
            try:
                font_manager.fontManager.addfont(str(path))
                families.append(font_manager.FontProperties(fname=str(path)).get_name())
            except (RuntimeError, OSError, ValueError):
                continue
    installed = {entry.name for entry in font_manager.fontManager.ttflist}
    for candidate in ("Noto Sans SC", "Noto Sans CJK SC", "Source Han Sans SC",
                      "WenQuanYi Micro Hei", "Droid Sans Fallback"):
        if candidate in installed:
            families.append(candidate)
    seen: list[str] = []
    for family in families + ["DejaVu Sans"]:
        if family not in seen:
            seen.append(family)
    return seen


def cloud_cmap():
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list("cloud", CLOUD_STOPS, N=256)


def _draw_panel(ax, site: Site, verdict: dict, slots: list[str]) -> None:
    from matplotlib.colors import to_rgb
    from matplotlib.patches import FancyBboxPatch, Rectangle

    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.add_patch(FancyBboxPatch((0.004, 0.004), 0.992, 0.992,
                                boxstyle="round,pad=0,rounding_size=0.018",
                                facecolor=C_PANEL, edgecolor=C_PANEL_EDGE,
                                linewidth=1.0, transform=ax.transAxes, zorder=0))

    ax.text(0.042, 0.982, site.name, fontsize=25, fontweight="bold", color=C_TXT,
            va="top", ha="left")
    ax.text(0.042, 0.918, f"{site.alias}　·　{abs(site.latitude):.3f}°N "
                          f"{abs(site.longitude):.3f}°E",
            fontsize=8.8, color=C_TXT_FAINT, va="top", ha="left")

    ens_hours = verdict.get("ens_hours", NIGHT_HOURS)
    p_label = "P(云 < 30%)" if ens_hours >= NIGHT_HOURS else f"P(云 < 30%) · 集合 {ens_hours}h"
    for x, value, label, color in ((0.958, verdict["median_cloud"], "中位云量", C_TXT),
                                   (0.792, verdict["p_clear"], p_label, C_ACCENT)):
        if value is None:
            ax.text(x, 0.958, "—", fontsize=15, color=C_TXT_FAINT, ha="right", va="center")
        else:
            ax.text(x, 0.986, f"{value:.0f}%", fontsize=19, fontweight="bold",
                    color=color, ha="right", va="top")
        ax.text(x, 0.926, label, fontsize=8.0, color=C_TXT_FAINT, ha="right", va="top")
    ax.plot([0.845, 0.845], [0.918, 0.972], color=C_RULE, linewidth=1.0)

    gx0, gx1 = 0.258, 0.958
    rows = verdict["rows"]
    row_height = 0.0395
    gy1 = 0.836
    gy0 = gy1 - row_height * len(rows)
    cell_width = (gx1 - gx0) / len(slots)
    cmap = cloud_cmap()

    ax.plot([0.042, 0.958], [0.876, 0.876], color=C_RULE, linewidth=1.0)
    ax.text(gx0 - 0.010, 0.862, "BJT", fontsize=7.2, color=C_TXT_FAINT, ha="right", va="top")
    for column, slot in enumerate(slots):
        hour = int(slot[11:13])
        night = hour >= 19 or hour <= 4
        ax.text(gx0 + cell_width * (column + 0.5), 0.862, f"{hour:02d}", fontsize=9.8,
                color=C_TXT if night else C_TXT_FAINT, ha="center", va="top",
                fontweight="bold" if night else "normal")
        if slot[11:16] == "00:00":
            x = gx0 + cell_width * column
            ax.plot([x, x], [gy0 - 0.014, gy1 + 0.006], color=C_ACCENT_SOFT, linewidth=0.9,
                    alpha=0.6, linestyle=(0, (2, 3)), zorder=2)

    for index, row in enumerate(rows):
        y = gy1 - row_height * (index + 1)
        is_consensus = row["kind"] == "cons"
        if is_consensus:
            ax.plot([0.042, 0.958], [y + row_height + 0.004] * 2, color=C_ACCENT_SOFT,
                    linewidth=0.9, alpha=0.55)

        label = row["label"] + (f" {row['n']}" if row["n"] and row["active"] else "")
        ax.text(0.235, y + row_height / 2, label, fontsize=9.0,
                color=(C_ACCENT if is_consensus else C_TXT if row["active"] else C_TXT_FAINT),
                ha="right", va="center", fontweight="bold" if is_consensus else "normal")

        if not row["active"]:
            ax.add_patch(Rectangle((gx0, y), gx1 - gx0, row_height, facecolor=C_MISS,
                                   edgecolor="none", zorder=1))
            ax.add_patch(Rectangle((gx0, y), gx1 - gx0, row_height, facecolor="none",
                                   edgecolor=C_MISS_HATCH, linewidth=0, hatch="///",
                                   alpha=0.9, zorder=1))
            ax.text(gx0 + (gx1 - gx0) / 2, y + row_height / 2, row["reason"], fontsize=6.9,
                    color=C_TXT_FAINT, ha="center", va="center", style="italic")
            continue

        for column, slot in enumerate(slots):
            x = gx0 + cell_width * column
            value = row["values"].get(slot)
            if value is None:
                ax.add_patch(Rectangle((x, y), cell_width, row_height, facecolor=C_MISS,
                                       edgecolor="none", zorder=1))
                continue
            color = cmap(min(1.0, max(0.0, value / 100.0)))
            ax.add_patch(Rectangle((x, y), cell_width, row_height, facecolor=color,
                                   edgecolor=C_PANEL, linewidth=0.6, zorder=1))
            luminance = sum(to_rgb(color)) / 3
            ax.text(x + cell_width / 2, y + row_height / 2, f"{value:.0f}",
                    fontsize=7.6 if is_consensus else 6.9,
                    color="#0b1020" if luminance > 0.62 else "#dde5f5",
                    ha="center", va="center", zorder=3,
                    fontweight="bold" if is_consensus else "normal",
                    alpha=1.0 if is_consensus else 0.92)

    if any(row["active"] for row in rows if row["kind"] == "cons"):
        ax.add_patch(Rectangle((gx0 - 0.004, gy0), gx1 - gx0 + 0.008, row_height,
                               facecolor="none", edgecolor=C_ACCENT, linewidth=1.0,
                               alpha=0.45, zorder=4))

    # observability curve
    sy1 = gy0 - 0.020
    sy0 = sy1 - 0.082
    series = [verdict["scores"].get(slot) for slot in slots]
    xs = [gx0 + cell_width * (column + 0.5) for column in range(len(slots))]
    ax.text(0.235, sy1 - 0.004, "适宜度", fontsize=8.0, color=C_TXT_DIM, ha="right", va="center")
    ax.plot([gx0 - 0.004, gx1 + 0.004], [sy1, sy1], color=C_RULE, linewidth=0.8)

    if not any(value is not None for value in series):
        ax.text(gx0 + (gx1 - gx0) / 2, (sy0 + sy1) / 2, "无共识，不计算适宜度",
                fontsize=7.6, color=C_TXT_FAINT, ha="center", va="center", style="italic")
    else:
        threshold_y = sy0 + (sy1 - sy0) * GOOD_WINDOW_SCORE / 100.0
        ax.plot([gx0, gx1], [threshold_y, threshold_y], color="#33405e", linewidth=0.7,
                linestyle=(0, (3, 3)), zorder=1)
        ax.text(gx1 - 0.002, threshold_y + 0.004, "70", fontsize=6.0, color=C_TXT_FAINT,
                ha="right", va="bottom")
        points = [(x, sy0 + (sy1 - sy0) * (value / 100.0))
                  for x, value in zip(xs, series) if value is not None]
        if len(points) >= 2:
            px, py = zip(*points)
            ax.fill_between(px, sy0, py, color=C_ACCENT, alpha=0.12, linewidth=0)
            ax.plot(px, py, color=C_ACCENT, linewidth=1.8, solid_capstyle="round", zorder=4)
            for (x, y), value in zip(points, [v for v in series if v is not None]):
                good = value >= GOOD_WINDOW_SCORE
                ax.scatter([x], [y], s=26 if good else 10,
                           color=C_GOOD if good else C_ACCENT,
                           edgecolor=C_PANEL if good else "none", linewidth=1.1, zorder=5)

    rule_y = sy0 - 0.038
    ax.plot([0.042, 0.958], [rule_y, rule_y], color=C_RULE, linewidth=1.0)
    ax.text(0.042, rule_y - 0.022, verdict["headline"], fontsize=10.6, color=C_TXT,
            va="top", ha="left", fontweight="bold")
    ax.text(0.042, rule_y - 0.062, verdict["detail"], fontsize=8.5, color=C_TXT_DIM,
            va="top", ha="left", linespacing=1.65)
    if verdict.get("note"):
        ax.text(0.042, rule_y - 0.112, verdict["note"], fontsize=8.2, color=C_WARN,
                va="top", ha="left")
    else:
        ax.plot([0.042, 0.958], [0.056, 0.056], color=C_RULE, linewidth=0.8)
        ax.text(0.042, 0.032, f"当晚实况复核：{site.metar} 逐 30 分钟 METAR　·　卫星看云团移动",
                fontsize=7.8, color=C_TXT_FAINT, va="center", ha="left")


def render(verdicts: list[tuple[Site, dict]], slots: list[str], date: str, out_png: Path,
           rendered_at: dt.datetime, days_ahead: int, label: str = "",
           font_dir: Path | None = None) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    from matplotlib.patches import Rectangle

    families = register_fonts(font_dir)
    plt.rcParams.update({
        "font.family": families,
        "axes.unicode_minus": False,
        "figure.facecolor": C_BG_BOT,
        "savefig.facecolor": C_BG_BOT,
        "text.color": C_TXT,
    })

    figure = plt.figure(figsize=(17, 9.6), dpi=200)
    background = figure.add_axes((0, 0, 1, 1), zorder=-10)
    background.set_axis_off()
    gradient = np.linspace(0, 1, 256).reshape(-1, 1)
    background.imshow(gradient, aspect="auto", origin="upper", extent=(0, 1, 0, 1),
                      cmap=LinearSegmentedColormap.from_list("bg", [C_BG_TOP, C_BG_BOT]),
                      interpolation="bicubic")

    grid = gridspec.GridSpec(1, 3, figure=figure, left=0.011, right=0.989,
                             top=0.752, bottom=0.108, wspace=0.072)

    figure.text(0.018, 0.958, "2026 双子座流星雨 · 候选地点", fontsize=12.5,
                color=C_TXT_DIM, va="top", ha="left")
    figure.text(0.017, 0.905, "观测夜云量整合", fontsize=33, fontweight="bold",
                color=C_TXT, va="top", ha="left")
    figure.text(0.017, 0.822, "冬季夜 · 逐小时总云量 · 多模式共识与集合概率",
                fontsize=10.5, color=C_TXT_FAINT, va="top", ha="left")
    det_count, ens_count = verdicts[0][1]["coverage"]
    next_day = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    figure.text(0.325, 0.822,
                f"{date} 18:00 → {next_day} 06:00 BJT　｜　本夜有效覆盖："
                f"{det_count}/8 确定性模式、{ens_count}/4 集合",
                fontsize=10.5, color=C_TXT_DIM, va="top", ha="left")

    medians = [verdict["median_cloud"] for _, verdict in verdicts
               if verdict["median_cloud"] is not None]
    peaks = [(site, max(verdict["scores"].values())) for site, verdict in verdicts
             if verdict["scores"]]
    hours = [verdict.get("hours", 0) for _, verdict in verdicts]
    if peaks and min(hours) < MIN_CONSENSUS_HOURS:
        summary = (f"确定性模式只覆盖了 {max(hours)}/{NIGHT_HOURS} 小时，暂不排三地优劣"
                   f" · 等 T-10 之后再判断")
        summary_color = C_WARN
    elif not peaks:
        if ens_count:
            summary = (f"目前只有 {ens_count} 个集合覆盖这一夜，还谈不到多源共识"
                       f" · 等 T-10 之后确定性模式到位再跑")
        else:
            summary = "这一夜目前没有任何模式覆盖 · 等 T-10 再跑"
        summary_color = C_WARN
    else:
        best, peak = max(peaks, key=lambda item: item[1])
        if peak >= GOOD_WINDOW_SCORE:
            tag, summary_color = "可拍", C_GOOD
        elif peak >= 40:
            tag, summary_color = "只能蹭云缝", C_WARN
        else:
            tag, summary_color = "不建议专程跑一趟", C_BAD
        spread = (max(medians) - min(medians)) if len(medians) > 1 else 0
        summary = (f"本夜相对最好：{best.name}（适宜度峰值 {peak:.0f}，{tag}）"
                   f"　·　三地中位云量差 {spread:.0f} 个百分点")
    figure.add_artist(Rectangle((0.011, 0.762), 0.978, 0.028, transform=figure.transFigure,
                                facecolor="#111a2b", edgecolor="none"))
    figure.text(0.020, 0.776, "▎", fontsize=9, color=summary_color, va="center", ha="left")
    figure.text(0.031, 0.776, summary, fontsize=9.6, color=summary_color, va="center", ha="left")

    figure.text(0.982, 0.952, f"数据刷新 {rendered_at.strftime('%Y-%m-%d %H:%M')} CST",
                fontsize=9.6, color=C_TXT_FAINT, ha="right", va="top")
    confidence = "低" if days_ahead > 10 else ("中" if days_ahead > 6 else "较高")
    figure.text(0.982, 0.918, f"T-{days_ahead}d", fontsize=16, fontweight="bold",
                color=C_TXT, ha="right", va="top")
    figure.text(0.982, 0.880, f"预报信心 {confidence}", fontsize=9.2,
                color=C_WARN if days_ahead > 10 else C_GOOD, ha="right", va="top",
                fontweight="bold")
    if label:
        figure.text(0.982, 0.852, label, fontsize=9.2, color=C_WARN, ha="right", va="top")

    for column, (site, verdict) in enumerate(verdicts):
        _draw_panel(figure.add_subplot(grid[0, column]), site, verdict, slots)

    colorbar_axis = figure.add_axes((0.018, 0.047, 0.155, 0.014))
    colorbar = figure.colorbar(ScalarMappable(cmap=cloud_cmap(), norm=Normalize(0, 100)),
                               cax=colorbar_axis, orientation="horizontal")
    colorbar.set_ticks([0, 25, 50, 75, 100])
    colorbar.ax.tick_params(labelsize=6.6, colors=C_TXT_FAINT, length=0, pad=2)
    colorbar.outline.set_visible(False)
    figure.text(0.018, 0.070, "总云量 %　　越白 = 云越多", fontsize=8.2, color=C_TXT_DIM,
                ha="left", va="bottom")

    legend_axis = figure.add_axes((0.196, 0.041, 0.022, 0.026))
    legend_axis.set_axis_off()
    legend_axis.add_patch(Rectangle((0, 0), 1, 1, facecolor=C_MISS,
                                    edgecolor=C_MISS_HATCH, hatch="///", linewidth=0))
    figure.text(0.224, 0.054, "该源时效未覆盖这一夜（空白 ≠ 无云）", fontsize=8.2,
                color=C_TXT_FAINT, ha="left", va="center")
    figure.text(0.196, 0.019,
                "适宜度 = 0.55·(1−总云) + 0.25·(1−低云) + 0.20·(1−高云)，"
                "灰色虚线为 70 阈值；集合概率 = 成员中云量 < 30% 的比例",
                fontsize=7.8, color=C_TXT_FAINT, ha="left", va="center")
    figure.text(0.982, 0.054,
                "源：ECMWF · NOAA/NCEP · DWD · UKMO · Météo-France · JMA · CMA · ECCC"
                "（经 Open-Meteo）", fontsize=7.8, color=C_TXT_FAINT, ha="right", va="center")
    figure.text(0.982, 0.024, "模式预报非实况：出行前请用 METAR / 风云四号 / 葵花九号复核",
                fontsize=7.8, color=C_TXT_FAINT, ha="right", va="center")

    out_png.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out_png, dpi=200, facecolor=C_BG_BOT)
    plt.close(figure)
    return out_png


def build(date: str, sites: tuple[Site, ...], out_dir: Path, label: str = "",
          font_dir: Path | None = None, today: dt.date | None = None,
          transport: httpx.BaseTransport | None = None) -> tuple[Path, list[tuple[Site, dict]]]:
    today = today or dt.date.today()
    slots = night_slots(date)
    rendered_at = dt.datetime.now()
    days_ahead = lead_days(date, today)
    verdicts: list[tuple[Site, dict]] = []
    meta_out: dict = {"date": date, "fetched_at": rendered_at.isoformat(timespec="seconds"),
                      "slots": slots, "sites": {}}
    coverage = (0, 0)

    with httpx.Client(timeout=40.0, headers={"User-Agent": USER_AGENT},
                      transport=transport, follow_redirects=True) as client:
        for site in sites:
            series, meta = fetch_site(client, site, slots)
            det = [item for item in series if item.kind == "det"]
            ens = [item for item in series if item.kind == "ens"]
            consensus, scores = consensus_scores(det, slots, date, today)
            rows = build_rows(det, ens, consensus)

            window, window_score = best_window(slots, scores)
            values = sorted(value for value in consensus.values() if value is not None)
            median = values[len(values) // 2] if values else None
            usable = [item for item in ens if item.members]
            probabilities = [probability_below(item, slot, CLEAR_THRESHOLD_PCT)
                             for item in usable for slot in slots if slot in item.members]
            p_clear = (sum(p for p in probabilities if p is not None)
                       / len(probabilities)) if probabilities else None

            det_n, ens_n = len([item for item in det if item.total]), len(usable)
            coverage = (max(coverage[0], det_n), max(coverage[1], ens_n))
            hours = len(consensus)
            ens_hours = max((len(item.members) for item in usable), default=0)
            if hours < MIN_CONSENSUS_HOURS:
                median = None

            if window and hours >= MIN_CONSENSUS_HOURS:
                headline = (f"最佳窗口 {window[0][11:16]} – {window[1][11:16]}"
                            f"　适宜度 {window_score:.0f}")
            elif not det_n and not ens_n:
                headline = "时效未到，这一夜还没有任何模式"
            elif not det_n:
                headline = "仅有集合覆盖，暂不给共识"
            elif hours < MIN_CONSENSUS_HOURS:
                headline = f"仅 {hours}/{NIGHT_HOURS} 小时有模式覆盖，不给整夜结论"
            else:
                headline = "覆盖不完整，暂不给窗口"

            detail = (f"{det_n}/8 确定性模式 + {ens_n}/4 集合参与，共识覆盖 "
                      f"{hours}/{NIGHT_HOURS} 小时；"
                      f"{'样本充足' if det_n >= 5 and hours >= NIGHT_HOURS - 1 else '样本偏少'}，"
                      f"{'结论可作行程依据' if det_n >= 5 and hours >= NIGHT_HOURS - 1 and days_ahead <= 5 else '仅作趋势参考'}。")
            if det_n == 0:
                note = "→ T-10 后再跑，届时 ECMWF / ICON / JMA 才覆盖到这一夜。"
            elif hours < MIN_CONSENSUS_HOURS:
                note = f"→ 现在只有前 {hours} 小时落在模式时效内，越靠近这一夜覆盖越完整。"
            else:
                note = None

            verdicts.append((site, dict(rows=rows, scores=scores, median_cloud=median,
                                        p_clear=p_clear, headline=headline, detail=detail,
                                        note=note, coverage=(det_n, ens_n), hours=hours,
                                        ens_hours=ens_hours)))
            meta_out["sites"][site.key] = {
                "meta": meta,
                "sources": [dict(key=item.spec.key, label=item.spec.label, kind=item.kind,
                                 last_valid=item.last_valid, error=item.error,
                                 hours=len(item.total)) for item in series],
            }

    for _, verdict in verdicts:
        verdict["coverage"] = coverage

    png = render(verdicts, slots, date, out_dir / "cloud_all.png", rendered_at,
                 days_ahead, label, font_dir)
    (out_dir / "meta.json").write_text(json.dumps(meta_out, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    return png, verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the Geminids cloud-cover chart.")
    parser.add_argument("--date", default="2026-12-12", help="Night starting date, Asia/Shanghai")
    parser.add_argument("--sites", default="all", help="Comma separated site keys, or 'all'")
    parser.add_argument("--out", type=Path, default=Path("charts"), help="Output directory")
    parser.add_argument("--label", default="", help="Optional badge on the chart")
    parser.add_argument("--font-dir", type=Path, default=None, help="Directory with CJK .otf/.ttf files")
    args = parser.parse_args(argv)

    sites = SITES if args.sites == "all" else tuple(
        site for site in SITES if site.key in args.sites.split(","))
    if not sites:
        parser.error(f"unknown site selection: {args.sites}")

    png, verdicts = build(args.date, sites, args.out, args.label, args.font_dir)
    print(f"chart: {png}")
    for site, verdict in verdicts:
        median = "—" if verdict["median_cloud"] is None else f'{verdict["median_cloud"]:.0f}%'
        p_clear = "—" if verdict["p_clear"] is None else f'{verdict["p_clear"]:.0f}%'
        print(f'{site.name}\t中位云量 {median}\tP(云<30%) {p_clear}\t{verdict["headline"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
