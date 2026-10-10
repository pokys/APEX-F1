#!/usr/bin/env python3
"""
Accuracy page: how well the model would have predicted this season.

Reads the walk-forward backtest (every GP predicted only from the data that
existed before it) and the live track record (predictions archived before
each session and scored afterwards), and compares the model with the
simplest sensible guess: "the championship order decides" (the standings
leader is the favourite). Writes outputs/accuracy.html, published next to
the dashboard.
"""

from __future__ import annotations

import argparse
import html
import logging
import math
import sys
from pathlib import Path
from typing import Any

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.prediction_targeting import load_json  # noqa: E402

LOGGER = logging.getLogger("render_accuracy_page")

MODEL_COLOR = "#0071e3"
BASELINE_COLOR = "#8e8e93"
# Relative difference of the typical probability below which the model is
# called "on par" with the baseline.
ON_PAR_MARGIN = 0.05


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the prediction accuracy page.")
    parser.add_argument("--race-config", default="config/race_config.json")
    parser.add_argument("--backtest-dir", default="outputs/backtest")
    parser.add_argument("--track-record", default="outputs/track_record.json")
    parser.add_argument("--output", default="outputs/accuracy.html")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def kind_rows(backtest: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Normalised per-event rows: kind 'win' (races), 'sprint' or 'pole'
    (qualifying)."""
    source = backtest.get({"win": "races", "sprint": "sprints"}.get(kind, "qualifying")) or []
    actual_key, predicted_key, hit_key, loss_key, probs_key = (
        ("actual_winner", "predicted_winner", "winner_hit", "winner_log_loss", "win_probabilities")
        if kind in {"win", "sprint"}
        else ("actual_pole", "predicted_pole", "pole_hit", "pole_log_loss", "pole_probabilities")
    )
    rows = []
    for row in source:
        if not isinstance(row, dict) or not row.get(actual_key):
            continue
        actual = str(row[actual_key])
        probs = row.get(probs_key) if isinstance(row.get(probs_key), dict) else {}
        model_p = probs.get(actual)
        if model_p is None and row.get(loss_key) is not None:
            model_p = math.exp(-float(row[loss_key]))
        baseline_loss = (row.get("baseline_log_loss") or {}).get("championship_order")
        rows.append(
            {
                "round": row.get("round"),
                "race": str(row.get("race") or ""),
                "actual": actual,
                "predicted": str(row.get(predicted_key) or ""),
                "hit": bool(row.get(hit_key)),
                "model_p": float(model_p or 0.0),
                "baseline_p": math.exp(-float(baseline_loss)) if baseline_loss is not None else None,
                "baseline_favourite": row.get("baseline_favourite"),
                "baseline_hit": row.get("baseline_hit"),
            }
        )
    return sorted(rows, key=lambda r: int(r["round"] or 0))


def typical_probability(values: list[float]) -> float | None:
    """Geometric mean, i.e. exp(-mean log loss), floored like the scoring."""
    values = [max(v, 1e-3) for v in values if v is not None]
    if not values:
        return None
    return math.exp(sum(math.log(v) for v in values) / len(values))


def verdict(model: float | None, baseline: float | None) -> str:
    if model is None or baseline is None or baseline <= 0:
        return ""
    ratio = model / baseline - 1.0
    if abs(ratio) < ON_PAR_MARGIN:
        return "on par with the championship-order guess"
    word = "better" if ratio > 0 else "worse"
    return f"{abs(ratio) * 100:.0f}% {word} than the championship-order guess"


def summary_card(title: str, rows: list[dict[str, Any]]) -> str:
    if not rows:
        return f'<article class="card"><h2>{html.escape(title)}</h2><p class="muted">No evaluated event yet.</p></article>'
    n = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    model_typical = typical_probability([r["model_p"] for r in rows])
    baseline_values = [r["baseline_p"] for r in rows if r["baseline_p"] is not None]
    baseline_typical = typical_probability(baseline_values) if len(baseline_values) == n else None
    has_baseline_hits = all(r["baseline_hit"] is not None for r in rows)
    baseline_hits = sum(1 for r in rows if r["baseline_hit"]) if has_baseline_hits else None
    lines = [
        f'<p class="big">{hits}/{n}<small> favourite correct</small></p>',
        (f'<p class="muted">Championship leader as favourite: {baseline_hits}/{n}</p>' if baseline_hits is not None else ""),
        f'<p>Typical probability given to the actual result: <strong>{model_typical * 100:.1f}%</strong>'
        + (f' <span class="muted">(championship order: {baseline_typical * 100:.1f}%)</span>' if baseline_typical is not None else "")
        + "</p>",
    ]
    text = verdict(model_typical, baseline_typical)
    if text:
        lines.append(f'<p class="verdict">Model is {html.escape(text)}.</p>')
    return f'<article class="card"><h2>{html.escape(title)}</h2>{"".join(lines)}</article>'


def dumbbell_svg(rows: list[dict[str, Any]], label: str, width: int = 720, left: int = 170, css: str = "chart-wide") -> str:
    """One row per GP: probability the model gave the actual winner (filled
    dot) next to the championship-order guess (hollow dot)."""
    if not rows:
        return ""
    row_h, top, right_pad = 30, 34, 34
    plot_w = width - left - right_pad
    height = top + row_h * len(rows) + 10
    peak = max([r["model_p"] for r in rows] + [r["baseline_p"] or 0.0 for r in rows] + [0.05])
    x_max = min(1.0, math.ceil(peak * 10) / 10)

    def x(p: float) -> float:
        return left + plot_w * min(p, x_max) / x_max

    parts = [
        f'<svg class="chart {css}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">',
    ]
    steps = int(round(x_max * 10))
    for i in range(steps + 1):
        p = i / 10
        parts.append(f'<line class="grid" x1="{x(p):.1f}" y1="{top - 8}" x2="{x(p):.1f}" y2="{height - 6}" />')
        parts.append(f'<text class="axis" x="{x(p):.1f}" y="{top - 14}" text-anchor="middle">{p * 100:.0f}%</text>')
    for idx, row in enumerate(rows):
        y = top + row_h * idx + row_h / 2
        name = f'R{row["round"]} {row["race"].replace(" Grand Prix", "")}'
        if left < 120:
            name = name[:12]
        parts.append(f'<text class="label" x="{left - 10}" y="{y + 4:.1f}" text-anchor="end">{html.escape(name)}</text>')
        tip = (
            f'{row["race"]}: {row["actual"]} won. Model gave {row["model_p"] * 100:.1f}%'
            + (f', championship order {row["baseline_p"] * 100:.1f}%' if row["baseline_p"] is not None else "")
            + f'. Model favourite: {row["predicted"]}' + (" (correct)" if row["hit"] else "")
        )
        parts.append(f'<g class="row"><title>{html.escape(tip)}</title>')
        parts.append(f'<rect class="hit" x="0" y="{y - row_h / 2:.1f}" width="{width}" height="{row_h}" />')
        if row["baseline_p"] is not None:
            x1, x2 = sorted((x(row["model_p"]), x(row["baseline_p"])))
            parts.append(f'<line class="link" x1="{x1:.1f}" y1="{y:.1f}" x2="{x2:.1f}" y2="{y:.1f}" />')
            parts.append(f'<circle class="baseline" cx="{x(row["baseline_p"]):.1f}" cy="{y:.1f}" r="5" />')
        parts.append(f'<circle class="model" cx="{x(row["model_p"]):.1f}" cy="{y:.1f}" r="6" />')
        mark = "&#10003;" if row["hit"] else ""
        parts.append(f'<text class="value" x="{x(row["model_p"]) + 10:.1f}" y="{y - 8:.1f}">{html.escape(row["actual"])} {mark}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


def events_table(rows: list[dict[str, Any]], result_label: str) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>R{html.escape(str(row['round']))}</td>"
            f"<td>{html.escape(row['race'])}</td>"
            f"<td>{html.escape(row['actual'])}</td>"
            f"<td>{html.escape(row['predicted'])} {'&#10003;' if row['hit'] else '&#10007;'}</td>"
            f"<td>{row['model_p'] * 100:.1f}%</td>"
            f"<td>{(row['baseline_p'] or 0.0) * 100:.1f}%</td>"
            "</tr>"
        )
    return (
        '<details><summary>Table view</summary><div class="table-wrap"><table>'
        f"<thead><tr><th>Round</th><th>GP</th><th>{html.escape(result_label)}</th><th>Model favourite</th>"
        "<th>Model P(actual)</th><th>Championship order P(actual)</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div></details>"
    )


def live_section(track_record: dict[str, Any] | None) -> str:
    entries = [e for e in (track_record or {}).get("entries") or [] if isinstance(e, dict)]
    if not entries:
        return (
            '<section class="card"><h2>Live predictions</h2>'
            '<p class="muted">The last prediction before every session is archived and scored once the result is in. '
            "No session has been scored yet.</p></section>"
        )
    rows = []
    for e in entries[::-1]:
        rows.append(
            "<tr>"
            f"<td>R{html.escape(str(e.get('round')))} {html.escape(str(e.get('session_code') or ''))}</td>"
            f"<td>{html.escape(str(e.get('race') or ''))}</td>"
            f"<td>{html.escape(str(e.get('predicted_winner') or ''))} {'&#10003;' if e.get('hit') else '&#10007;'}</td>"
            f"<td>{html.escape(str(e.get('actual_winner') or ''))}</td>"
            f"<td>{float(e.get('winner_probability') or 0.0) * 100:.1f}%</td>"
            "</tr>"
        )
    hits = sum(1 for e in entries if e.get("hit"))
    return (
        '<section class="card"><h2>Live predictions</h2>'
        f"<p>{hits}/{len(entries)} favourites correct in predictions made before the session started.</p>"
        '<div class="table-wrap"><table><thead><tr><th>Session</th><th>GP</th><th>Favourite</th><th>Actual</th><th>P(actual)</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div></section>"
    )


def render(backtest: dict[str, Any] | None, track_record: dict[str, Any] | None, season: Any) -> str:
    backtest = backtest or {}
    win = kind_rows(backtest, "win")
    pole = kind_rows(backtest, "pole")
    sprint = kind_rows(backtest, "sprint")
    legend = (
        '<p class="legend"><span><i class="dot model"></i>Model</span>'
        '<span><i class="dot baseline"></i>Championship order (standings leader favoured)</span>'
        "<span>&#10003; model favourite was right</span></p>"
    )
    sections = []
    for title, rows, label in (("Race winner", win, "Winner"), ("Sprint winner", sprint, "Winner"), ("Pole position", pole, "Pole")):
        if rows:
            sections.append(
                f'<section class="card"><h2>{title}: probability given to the actual result</h2>{legend}'
                f"{dumbbell_svg(rows, title)}{dumbbell_svg(rows, title, 380, 100, 'chart-narrow')}{events_table(rows, label)}</section>"
            )
    season_text = html.escape(str(backtest.get("season") or season or ""))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>APEX-F1 Accuracy</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0a0a0c; --panel: #151517; --ink: #f5f5f7; --muted: #a1a1a6; --grid: rgba(255, 255, 255, 0.08);
    --accent: #0a84ff; --model: #0a84ff; --baseline: #8e8e93; --shadow: none;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 15px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", Inter, "Segoe UI", Roboto, sans-serif;
    color: var(--ink); background: var(--bg); min-height: 100vh; -webkit-font-smoothing: antialiased; font-variant-numeric: tabular-nums; }}
  .wrap {{ width: min(1080px, 100% - 32px); margin: 0 auto; padding: 28px 0 48px; }}
  a {{ color: var(--accent); text-decoration: none; }}
  .back {{ font-size: 0.9rem; }}
  h1 {{ margin: 10px 0 6px; font-size: clamp(1.7rem, 4vw, 2.4rem); font-weight: 700; letter-spacing: -0.02em; }}
  h2 {{ margin: 0 0 10px; font-size: 0.78rem; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }}
  .intro {{ color: var(--muted); max-width: 70ch; line-height: 1.5; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; margin: 18px 0; }}
  .card {{ background: var(--panel); border: 1px solid var(--grid); border-radius: 14px; box-shadow: var(--shadow); padding: 18px 20px; margin-bottom: 16px; }}
  .big {{ font-size: 2rem; font-weight: 800; margin: 4px 0; }}
  .big small {{ font-size: 0.9rem; font-weight: 500; color: var(--muted); margin-left: 6px; }}
  .muted {{ color: var(--muted); }}
  .verdict {{ font-weight: 600; }}
  .legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 0.85rem; color: var(--muted); margin: 0 0 6px; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: -1px; }}
  .dot.model {{ background: var(--model); }}
  .dot.baseline {{ border: 2px solid var(--baseline); }}
  .chart {{ width: 100%; height: auto; display: block; }}
  .chart-narrow {{ display: none; }}
  @media (max-width: 640px) {{
    .chart-wide {{ display: none; }}
    .chart-narrow {{ display: block; }}
  }}
  .chart .grid {{ stroke: var(--grid); stroke-width: 1; }}
  .chart .axis, .chart .label {{ fill: var(--muted); font-size: 12px; }}
  .chart .value {{ fill: var(--ink); font-size: 11px; }}
  .chart .link {{ stroke: var(--grid); stroke-width: 2; }}
  .chart .model {{ fill: var(--model); stroke: var(--panel); stroke-width: 2; }}
  .chart .baseline {{ fill: var(--panel); stroke: var(--baseline); stroke-width: 2; }}
  .chart .hit {{ fill: transparent; }}
  .chart .row:hover .hit {{ fill: rgba(255, 255, 255, 0.04); }}
  details {{ margin-top: 10px; }}
  summary {{ cursor: pointer; color: var(--muted); }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; margin-top: 8px; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }}
  th {{ color: var(--muted); font-weight: 600; }}
  .note {{ font-size: 0.85rem; color: var(--muted); line-height: 1.5; }}
</style>
</head>
<body>
<main class="wrap">
  <a class="back" href="index.html">&larr; Back to the prediction</a>
  <h1>How accurate is APEX-F1? ({season_text})</h1>
  <p class="intro">Every Grand Prix of the season is re-predicted using only the data that existed before it, then compared with
  what actually happened. The yardstick is the simplest sensible guess: <em>the championship order decides</em> (the current
  standings leader is the favourite). A useful model must beat it.</p>
  <section class="cards">{summary_card("Race winner", win)}{summary_card("Sprint winner", sprint) if sprint else ""}{summary_card("Pole position", pole)}</section>
  {"".join(sections)}
  {live_section(track_record)}
  <p class="note">"Typical probability" is the geometric mean of the probability given to the actual winner (equivalent to the
  mean log loss); it rewards confident correct calls and punishes confident wrong ones. The backtest uses the noise and blend
  settings tuned on this same season, so it is slightly optimistic; the live table has no such advantage.</p>
</main>
</body>
</html>
"""


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    try:
        config_path = Path(args.race_config)
        config = load_json(config_path) if config_path.exists() else {}
        season = config.get("season")
        backtest_path = Path(args.backtest_dir) / f"backtest_season_{season}.json"
        backtest = load_json(backtest_path) if backtest_path.exists() else None
        track_path = Path(args.track_record)
        track_record = load_json(track_path) if track_path.exists() else None
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render(backtest, track_record, season), encoding="utf-8")
    except Exception as exc:
        # The accuracy page is secondary; never block the prediction pipeline.
        LOGGER.warning("Accuracy page skipped: %s", exc)
        return 0
    LOGGER.info("Wrote accuracy page: %s", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
