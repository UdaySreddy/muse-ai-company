#!/usr/bin/env python3
"""Traffic report generator — generates a self-contained HTML report from the github-repo-stats data branch.

Replaces ghrs's bundled report.html (misaligned images, poor layout). Zero external-link dependencies:
CSS/JS/data are all embedded, so it opens offline, prints without breaking layout, and adapts to light/dark color schemes.

Usage:
    python3 scripts/traffic_report.py [--out /tmp/traffic-report.html]
                                      [--data-dir <ghrs-data directory>]

By default it reads the latest CSV from the git data branch origin/github-repo-stats;
when --data-dir specifies a local directory it reads files instead (offline fallback).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

GIT_REF = "origin/github-repo-stats"
DATA_PREFIX = "CronusL-1141/AI-company/ghrs-data"
REPO_LABEL = "CronusL-1141/AI-company"

# dataviz reference palette (verified): series-1 blue / series-2 aqua + dual-mode surfaces
LIGHT = {
    "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e",
    "grid": "#e9e8e4", "border": "#e2e1dc", "s1": "#2a78d6", "s2": "#1baf7a",
    "s1_fill": "#cde2fb",
}
DARK = {
    "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7",
    "grid": "#33322f", "border": "#3a3936", "s1": "#3987e5", "s2": "#199e70",
    "s1_fill": "#0d366b",
}

# ── Data reading ────────────────────────────────────────────────────────────


def _git_show(path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "show", f"{GIT_REF}:{path}"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout
    except subprocess.CalledProcessError:
        return None


def _git_ls(dir_path: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-tree", "--name-only", GIT_REF, dir_path + "/"],
            capture_output=True, text=True, check=True,
        )
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        return []


def _read_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def load_data(data_dir: Path | None) -> dict[str, list[dict[str, str]]]:
    """Read the five tables; the git branch takes priority, data_dir is the fallback. Snapshots take the newest-named pair."""
    def read(name: str) -> list[dict[str, str]]:
        if data_dir is not None:
            p = data_dir / name
            return _read_csv(p.read_text(encoding="utf-8")) if p.exists() else []
        text = _git_show(f"{DATA_PREFIX}/{name}")
        return _read_csv(text) if text else []

    def latest_snapshot(kind: str) -> list[dict[str, str]]:
        if data_dir is not None:
            files = sorted((data_dir / "snapshots").glob(f"*_{kind}_snapshot.csv"))
            return _read_csv(files[-1].read_text(encoding="utf-8")) if files else []
        names = [f for f in _git_ls(f"{DATA_PREFIX}/snapshots") if f"_{kind}_snapshot.csv" in f]
        if not names:
            return []
        text = _git_show(sorted(names)[-1])
        return _read_csv(text) if text else []

    return {
        "views_clones": read("views_clones_aggregate.csv"),
        "stars": read("stargazers.csv"),
        "forks": read("forks.csv"),
        "referrers": latest_snapshot("top_referrers"),
        "paths": latest_snapshot("top_paths"),
    }


# ── SVG chart building ────────────────────────────────────────────────────────

W, H = 960, 300
PAD_L, PAD_R, PAD_T, PAD_B = 56, 16, 18, 34
PW, PH = W - PAD_L - PAD_R, H - PAD_T - PAD_B


def _nice_ticks(vmax: float, n: int = 4) -> list[int]:
    if vmax <= 0:
        return [0, 1]
    raw = vmax / n
    mag = 10 ** (len(str(int(raw))) - 1)
    step = max(1, round(raw / mag) * mag)
    ticks, v = [], 0
    while v < vmax + step:
        ticks.append(int(v))
        v += step
    return ticks


def _pts(xs: list[float], ys: list[float]) -> str:
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))


def line_chart(
    chart_id: str,
    dates: list[str],
    series: list[tuple[str, list[int], str]],  # (name, values, CSS variable name)
    annotate_peak: bool = False,
    area_first: bool = False,
) -> str:
    """Single-axis line chart: 2px lines, hover vertical-line tooltip, optional direct peak annotation / area fill."""
    n = len(dates)
    if n < 2:
        return '<p class="empty">Insufficient data (fewer than 2 days)</p>'
    vmax = max(max(vals) for _, vals, _ in series) or 1
    ticks = _nice_ticks(vmax)
    vtop = ticks[-1]
    xs = [PAD_L + PW * i / (n - 1) for i in range(n)]

    def ys(vals: list[int]) -> list[float]:
        return [PAD_T + PH * (1 - v / vtop) for v in vals]

    g: list[str] = []
    # Grid (recessive) + y-axis ticks
    for tv in ticks:
        y = PAD_T + PH * (1 - tv / vtop)
        g.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L + PW}" y2="{y:.1f}" class="grid"/>')
        g.append(f'<text x="{PAD_L - 8}" y="{y + 4:.1f}" class="tick" text-anchor="end">{tv:,}</text>')
    # x-axis dates (first/last + 3 evenly spaced midpoints)
    for i in sorted({0, n // 4, n // 2, 3 * n // 4, n - 1}):
        g.append(
            f'<text x="{xs[i]:.1f}" y="{H - 10}" class="tick" text-anchor="middle">'
            f"{dates[i][5:10]}</text>"
        )
    # Area (first series only, for depth on single-series charts like stars)
    if area_first:
        _, vals, _ = series[0]
        area = (
            f"{PAD_L},{PAD_T + PH} " + _pts(xs, ys(vals)) + f" {PAD_L + PW},{PAD_T + PH}"
        )
        g.append(f'<polygon points="{area}" class="area"/>')
    # Lines
    for name, vals, var in series:
        g.append(
            f'<polyline points="{_pts(xs, ys(vals))}" fill="none" '
            f'stroke="var({var})" stroke-width="2" stroke-linejoin="round"/>'
        )
    # Direct peak annotation (selective annotation: only label the maximum point, not every point)
    if annotate_peak:
        name, vals, var = series[0]
        pi = vals.index(max(vals))
        px, py = xs[pi], ys(vals)[pi]
        g.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4" fill="var({var})" class="ring"/>')
        anchor = "end" if pi > n * 0.7 else ("start" if pi < n * 0.3 else "middle")
        g.append(
            f'<text x="{px:.1f}" y="{py - 10:.1f}" class="peak" text-anchor="{anchor}">'
            f"{dates[pi][5:10]} · {vals[pi]:,}</text>"
        )
    # Hover capture layer + vertical line + data (consumed by JS)
    payload = json.dumps(
        {"dates": dates, "series": [{"name": s[0], "vals": s[1], "var": s[2]} for s in series],
         "xs": [round(x, 1) for x in xs], "padT": PAD_T, "plotH": PH},
        ensure_ascii=False,
    )
    g.append(f'<line class="xhair" x1="0" y1="{PAD_T}" x2="0" y2="{PAD_T + PH}" style="display:none"/>')
    g.append(
        f'<rect class="capture" x="{PAD_L}" y="{PAD_T}" width="{PW}" height="{PH}" '
        'fill="transparent"/>'
    )
    legend = ""
    if len(series) > 1:  # No legend for a single series (the title is the name)
        items = "".join(
            f'<span class="li"><i style="background:var({var})"></i>{name}</span>'
            for name, _, var in series
        )
        legend = f'<div class="legend">{items}</div>'
    return (
        f'{legend}<div class="chart-wrap"><svg id="{chart_id}" viewBox="0 0 {W} {H}" '
        f'preserveAspectRatio="xMidYMid meet" role="img">{"".join(g)}</svg>'
        f'<div class="tooltip" hidden></div>'
        f'<script type="application/json" class="chart-data">{payload}</script></div>'
    )


def bar_chart(rows: list[tuple[str, int, int]]) -> str:
    """Horizontal bar chart (magnitude → a single hue; 4px rounded data-end; 2px gaps; value labeled directly on the right)."""
    if not rows:
        return '<p class="empty">No referrer data yet</p>'
    vmax = max(r[1] for r in rows) or 1
    bar_h, gap, label_w, val_w = 26, 8, 190, 90
    bw = W - label_w - val_w - 24
    height = len(rows) * (bar_h + gap) + gap
    g: list[str] = []
    for i, (name, total, uniq) in enumerate(rows):
        y = gap + i * (bar_h + gap)
        w = max(2.0, bw * total / vmax)
        g.append(
            f'<text x="{label_w - 10}" y="{y + bar_h / 2 + 4}" class="blabel" '
            f'text-anchor="end">{_esc(name)}</text>'
        )
        g.append(
            f'<rect x="{label_w}" y="{y}" width="{w:.1f}" height="{bar_h}" rx="4" '
            'class="bar"><title>'
            f"{_esc(name)}: {total:,} views / {uniq:,} unique visitors</title></rect>"
        )
        g.append(
            f'<text x="{label_w + w + 10:.1f}" y="{y + bar_h / 2 + 4}" class="bval">'
            f"{total:,} <tspan class='bsub'>({uniq:,} people)</tspan></text>"
        )
    return (
        f'<svg viewBox="0 0 {W} {height}" preserveAspectRatio="xMidYMid meet" '
        f'role="img">{"".join(g)}</svg>'
    )


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Report assembly ────────────────────────────────────────────────────────────


def build_html(data: dict[str, list[dict[str, str]]]) -> str:
    vc = data["views_clones"]
    dates = [r["time_iso8601"][:10] for r in vc]
    views = [int(r["views_total"]) for r in vc]
    vuniq = [int(r["views_unique"]) for r in vc]
    clones = [int(r["clones_total"]) for r in vc]
    cuniq = [int(r["clones_unique"]) for r in vc]

    stars_rows = data["stars"]
    star_dates = [r["time_iso8601"][:10] for r in stars_rows]
    star_vals = [int(r["stars_cumulative"]) for r in stars_rows]
    forks_now = int(data["forks"][-1]["forks_cumulative"]) if data["forks"] else 0

    peak_i = views.index(max(views)) if views else 0
    kpis = [
        ("Total views", f"{sum(views):,}", f"Peak {dates[peak_i][5:10]} · {views[peak_i]:,}"),
        ("Unique visitors", f"{sum(vuniq):,}", f"Peak day {max(vuniq):,} people"),
        ("Clones", f"{sum(clones):,}", f"{sum(cuniq):,} unique cloners"),
        ("Stars", f"{star_vals[-1]:,}" if star_vals else "—", f"Forks {forks_now:,}"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-v">{v}</div>'
        f'<div class="kpi-l">{label}</div><div class="kpi-s">{sub}</div></div>'
        for label, v, sub in kpis
    )

    ref_rows = [
        (r["referrer"], int(r["views_total"]), int(r["views_unique"]))
        for r in data["referrers"]
    ]
    path_rows = "".join(
        f'<tr><td class="mono">{_esc(r["url_path"].removeprefix("/" + REPO_LABEL) or "/")}</td>'
        f'<td class="num">{int(r["views_total"]):,}</td>'
        f'<td class="num">{int(r["views_unique"]):,}</td></tr>'
        for r in data["paths"]
    )

    date_range = f"{dates[0]} ~ {dates[-1]} (UTC day convention)" if dates else "No data"

    charts = {
        "views": line_chart(
            "c-views", dates,
            [("Views", views, "--s1"), ("Unique visitors", vuniq, "--s2")],
            annotate_peak=True,
        ),
        "clones": line_chart(
            "c-clones", dates,
            [("Clones", clones, "--s1"), ("Unique cloners", cuniq, "--s2")],
        ),
        "stars": line_chart(
            "c-stars", star_dates, [("Stars cumulative", star_vals, "--s1")],
            area_first=True,
        ),
    }

    css = _css()
    js = _js()
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{REPO_LABEL} Traffic Report</title>
<style>{css}</style>
</head>
<body>
<main>
  <header>
    <h1>{REPO_LABEL} · Traffic Report</h1>
    <p class="sub">Data range {date_range} — accumulated daily by the github-repo-stats data branch</p>
  </header>
  <section class="kpis">{kpi_html}</section>
  <section class="card"><h2>Views Trend</h2>{charts["views"]}</section>
  <section class="card"><h2>Clones Trend</h2>{charts["clones"]}</section>
  <section class="card"><h2>Stars Cumulative Growth</h2>{charts["stars"]}</section>
  <section class="card"><h2>Traffic Sources Top {len(ref_rows)}</h2>
    <p class="note">Most recent 14-day window snapshot · values are view counts (unique visitors in parentheses)</p>
    {bar_chart(ref_rows)}</section>
  <section class="card"><h2>Popular Paths</h2>
    <table><thead><tr><th>Path</th><th class="num">Views</th><th class="num">Unique visitors</th></tr></thead>
    <tbody>{path_rows}</tbody></table></section>
  <footer>Generated locally · scripts/traffic_report.py · Data source {GIT_REF}</footer>
</main>
<script>{js}</script>
</body>
</html>"""


def _css() -> str:
    li, dk = LIGHT, DARK
    return f"""
:root {{
  --surface:{li["surface"]}; --ink:{li["ink"]}; --ink2:{li["ink2"]};
  --grid:{li["grid"]}; --border:{li["border"]};
  --s1:{li["s1"]}; --s2:{li["s2"]}; --s1-fill:{li["s1_fill"]};
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --surface:{dk["surface"]}; --ink:{dk["ink"]}; --ink2:{dk["ink2"]};
    --grid:{dk["grid"]}; --border:{dk["border"]};
    --s1:{dk["s1"]}; --s2:{dk["s2"]}; --s1-fill:{dk["s1_fill"]};
  }}
}}
* {{ box-sizing: border-box; margin: 0; }}
body {{
  background: var(--surface); color: var(--ink);
  font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
}}
main {{ max-width: 1020px; margin: 0 auto; padding: 28px 20px 40px; }}
header h1 {{ font-size: 21px; font-weight: 650; }}
.sub, .note, footer {{ color: var(--ink2); font-size: 12.5px; }}
.sub {{ margin-top: 4px; }}
.kpis {{
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0;
}}
@media (max-width: 720px) {{ .kpis {{ grid-template-columns: repeat(2, 1fr); }} }}
.kpi {{ border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }}
.kpi-v {{ font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }}
.kpi-l {{ color: var(--ink2); font-size: 13px; margin-top: 2px; }}
.kpi-s {{ color: var(--ink2); font-size: 12px; margin-top: 6px; }}
.card {{
  border: 1px solid var(--border); border-radius: 10px;
  padding: 18px 18px 12px; margin-bottom: 16px;
}}
.card h2 {{ font-size: 15px; font-weight: 650; margin-bottom: 10px; }}
.note {{ margin: -6px 0 10px; }}
svg {{ width: 100%; height: auto; display: block; }}
.grid {{ stroke: var(--grid); stroke-width: 1; }}
.tick, .blabel, .bval, .peak {{ fill: var(--ink2); font-size: 11px; }}
.blabel {{ fill: var(--ink); }}
.bval {{ fill: var(--ink); font-variant-numeric: tabular-nums; }}
.bsub {{ fill: var(--ink2); }}
.peak {{ fill: var(--ink); font-weight: 600; }}
.bar {{ fill: var(--s1); }}
.area {{ fill: var(--s1-fill); opacity: .45; }}
.ring {{ stroke: var(--surface); stroke-width: 2; }}
.legend {{ display: flex; gap: 16px; margin-bottom: 6px; }}
.li {{ display: inline-flex; align-items: center; gap: 6px; color: var(--ink2); font-size: 12.5px; }}
.li i {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
.chart-wrap {{ position: relative; }}
.xhair {{ stroke: var(--ink2); stroke-width: 1; stroke-dasharray: 3 3; }}
.tooltip {{
  position: absolute; pointer-events: none; background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
  font-size: 12px; box-shadow: 0 4px 14px rgb(0 0 0 / .12); white-space: nowrap; z-index: 5;
}}
.tooltip .tt-d {{ color: var(--ink2); margin-bottom: 2px; }}
.tooltip .tt-r {{ display: flex; align-items: center; gap: 6px; }}
.tooltip .tt-r i {{ width: 8px; height: 8px; border-radius: 2px; }}
.tooltip .tt-v {{ margin-left: auto; padding-left: 14px; font-variant-numeric: tabular-nums; font-weight: 600; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ padding: 7px 10px; border-bottom: 1px solid var(--border); text-align: left; }}
th {{ color: var(--ink2); font-size: 12px; font-weight: 600; }}
td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }}
.empty {{ color: var(--ink2); padding: 18px 0; }}
footer {{ margin-top: 20px; text-align: center; }}
@media print {{
  .tooltip, .xhair {{ display: none !important; }}
  .card {{ break-inside: avoid; }}
}}
"""


def _js() -> str:
    # Hover vertical line + tooltip: snap to the nearest point by x, shared by all line charts
    return """
document.querySelectorAll('.chart-wrap').forEach(function (wrap) {
  var svg = wrap.querySelector('svg');
  var dataEl = wrap.querySelector('.chart-data');
  var tip = wrap.querySelector('.tooltip');
  if (!svg || !dataEl || !tip) return;
  var d = JSON.parse(dataEl.textContent);
  var xhair = svg.querySelector('.xhair');
  var cap = svg.querySelector('.capture');
  if (!cap) return;
  function toSvgX(evt) {
    var pt = svg.createSVGPoint();
    pt.x = evt.clientX; pt.y = evt.clientY;
    return pt.matrixTransform(svg.getScreenCTM().inverse()).x;
  }
  cap.addEventListener('mousemove', function (evt) {
    var x = toSvgX(evt), best = 0, bd = 1e9;
    d.xs.forEach(function (px, i) {
      var dd = Math.abs(px - x);
      if (dd < bd) { bd = dd; best = i; }
    });
    var px = d.xs[best];
    xhair.setAttribute('x1', px); xhair.setAttribute('x2', px);
    xhair.style.display = '';
    var rows = d.series.map(function (s) {
      return '<div class="tt-r"><i style="background:var(' + s.var + ')"></i>' +
        s.name + '<span class="tt-v">' + s.vals[best].toLocaleString() + '</span></div>';
    }).join('');
    tip.innerHTML = '<div class="tt-d">' + d.dates[best] + '</div>' + rows;
    tip.hidden = false;
    var rect = wrap.getBoundingClientRect(), sr = svg.getBoundingClientRect();
    var cx = sr.left - rect.left + (px / SVG_W) * sr.width;
    var left = cx + 14;
    if (left + tip.offsetWidth > rect.width - 4) left = cx - tip.offsetWidth - 14;
    tip.style.left = left + 'px';
    tip.style.top = (sr.top - rect.top + 20) + 'px';
  });
  cap.addEventListener('mouseleave', function () {
    tip.hidden = true; xhair.style.display = 'none';
  });
});
""".replace("SVG_W", str(W))


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a traffic HTML report")
    ap.add_argument("--out", default="/tmp/traffic-report.html")
    ap.add_argument("--data-dir", default=None, help="Local ghrs-data directory (offline fallback)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else None
    data = load_data(data_dir)
    if not data["views_clones"]:
        print("Error: cannot read views_clones_aggregate.csv — first git fetch origin github-repo-stats,"
              "or use --data-dir to specify a local data directory", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.write_text(build_html(data), encoding="utf-8")
    print(f"Report generated: {out}  ({out.stat().st_size / 1024:.0f} KB, self-contained with no external links)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
