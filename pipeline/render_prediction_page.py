#!/usr/bin/env python3
"""
Render a static HTML overview page from prediction output.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("render_prediction_page")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render prediction overview HTML.")
    parser.add_argument("--prediction", default="outputs/prediction.json", help="Prediction JSON input path.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON input path.")
    parser.add_argument("--output", default="outputs/prediction_report.html", help="Rendered HTML output path.")
    parser.add_argument("--allow-missing-input", action="store_true", help="Exit 0 if prediction input is missing.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def render_page(prediction: dict[str, Any], race_config: dict[str, Any]) -> str:
    drivers = prediction.get("drivers")
    if not isinstance(drivers, list) or not drivers:
        raise ValueError("Prediction JSON missing non-empty 'drivers' list.")

    rows: list[dict[str, Any]] = []
    for raw in drivers:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "win_probability": max(0.0, min(1.0, to_float(raw.get("win_probability"), 0.0))),
                "podium_probability": max(0.0, min(1.0, to_float(raw.get("podium_probability"), 0.0))),
                "expected_finish": max(1.0, to_float(raw.get("expected_finish"), 99.0)),
            }
        )
    rows.sort(key=lambda x: (-x["win_probability"], x["expected_finish"], x["name"].lower()))
    top3 = rows[:3]

    race_name = html.escape(str(prediction.get("race") or race_config.get("race") or "Next GP"))
    generated_at = html.escape(str(prediction.get("generated_at") or race_config.get("generated_at") or ""))
    race_date = html.escape(str(race_config.get("race_date") or ""))
    weather = html.escape(str(race_config.get("weather") or "unknown"))
    simulations = int(to_float(race_config.get("simulations"), 0))
    seed = int(to_float(race_config.get("seed"), 0))
    safety_car = to_float(race_config.get("safety_car_probability"), 0.0)
    overtake = to_float(race_config.get("overtaking_difficulty"), 0.0)

    podium_cards = "\n".join(
        [
            (
                f'<article class="podium-card">'
                f'<p class="rank">P{idx}</p>'
                f'<h3>{html.escape(row["name"])}</h3>'
                f'<p class="metric">Win: {row["win_probability"] * 100:.2f}%</p>'
                f'<p class="metric">Podium: {row["podium_probability"] * 100:.2f}%</p>'
                f"<p class=\"metric\">Exp. finish: {row['expected_finish']:.3f}</p>"
                f"</article>"
            )
            for idx, row in enumerate(top3, start=1)
        ]
    )

    table_rows = "\n".join(
        [
            (
                f"<tr>"
                f"<td class=\"driver\">{html.escape(row['name'])}</td>"
                f"<td><div class=\"bar\"><span style=\"width:{row['win_probability'] * 100:.3f}%\"></span></div>"
                f"<small>{row['win_probability'] * 100:.3f}%</small></td>"
                f"<td><div class=\"bar podium\"><span style=\"width:{row['podium_probability'] * 100:.3f}%\"></span></div>"
                f"<small>{row['podium_probability'] * 100:.3f}%</small></td>"
                f"<td class=\"finish\">{row['expected_finish']:.3f}</td>"
                f"</tr>"
            )
            for row in rows
        ]
    )

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{race_name} | APEX-F1 Prediction</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
    <style>
      :root {{
        --bg: #0b1118;
        --panel: #111b26cc;
        --panel-strong: #152232;
        --ink: #eaf3ff;
        --muted: #9bb0c6;
        --accent: #2ad2c9;
        --accent-2: #ff7a45;
        --grid: #203142;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Sora", "Segoe UI", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(1200px 700px at -10% -20%, #184160 0%, transparent 60%),
          radial-gradient(900px 600px at 120% 0%, #5a2d1f 0%, transparent 55%),
          linear-gradient(165deg, #081019 0%, #0a141f 50%, #0d1a28 100%);
        min-height: 100vh;
      }}
      .wrap {{
        width: min(1100px, 95vw);
        margin: 0 auto;
        padding: 24px 0 36px;
      }}
      .hero {{
        display: grid;
        gap: 14px;
        background: var(--panel);
        border: 1px solid var(--grid);
        border-radius: 18px;
        padding: 20px;
        backdrop-filter: blur(8px);
      }}
      h1 {{
        margin: 0;
        font-size: clamp(1.5rem, 3vw, 2.2rem);
        letter-spacing: 0.02em;
      }}
      .meta {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }}
      .chip {{
        font-family: "IBM Plex Mono", monospace;
        font-size: 0.82rem;
        color: var(--muted);
        border: 1px solid var(--grid);
        border-radius: 999px;
        padding: 6px 10px;
        background: #0d1722;
      }}
      .podium {{
        margin-top: 16px;
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
      }}
      .podium-card {{
        background: linear-gradient(160deg, #13202e 0%, #0e1823 100%);
        border: 1px solid var(--grid);
        border-radius: 14px;
        padding: 14px;
      }}
      .rank {{
        margin: 0;
        color: var(--accent);
        font-family: "IBM Plex Mono", monospace;
        font-size: 0.85rem;
      }}
      .podium-card h3 {{
        margin: 6px 0 10px;
        font-size: 1.2rem;
      }}
      .metric {{
        margin: 4px 0;
        color: var(--muted);
        font-size: 0.92rem;
      }}
      .table-wrap {{
        margin-top: 16px;
        background: var(--panel-strong);
        border: 1px solid var(--grid);
        border-radius: 16px;
        overflow: hidden;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
      }}
      th, td {{
        padding: 12px 14px;
        border-bottom: 1px solid #1e3143;
        text-align: left;
      }}
      th {{
        color: var(--muted);
        font-size: 0.82rem;
        letter-spacing: 0.06em;
        text-transform: uppercase;
      }}
      td {{ font-size: 0.92rem; }}
      td.driver {{ font-weight: 700; letter-spacing: 0.02em; }}
      .bar {{
        width: 100%;
        max-width: 240px;
        height: 8px;
        border-radius: 999px;
        background: #0c141d;
        border: 1px solid #22384c;
        overflow: hidden;
      }}
      .bar span {{
        display: block;
        height: 100%;
        background: linear-gradient(90deg, var(--accent), #6ff5ef);
      }}
      .bar.podium span {{
        background: linear-gradient(90deg, var(--accent-2), #ffb26b);
      }}
      small {{
        display: inline-block;
        margin-top: 5px;
        color: var(--muted);
        font-family: "IBM Plex Mono", monospace;
      }}
      td.finish {{
        font-family: "IBM Plex Mono", monospace;
        font-weight: 500;
      }}
      @media (max-width: 900px) {{
        .podium {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="wrap">
      <section class="hero">
        <h1>APEX-F1 Prediction | {race_name}</h1>
        <div class="meta">
          <span class="chip">Race date: {race_date}</span>
          <span class="chip">Weather: {weather}</span>
          <span class="chip">Simulations: {simulations}</span>
          <span class="chip">Seed: {seed}</span>
          <span class="chip">Safety car p: {safety_car:.3f}</span>
          <span class="chip">Overtake difficulty: {overtake:.3f}</span>
          <span class="chip">Generated: {generated_at}</span>
        </div>
      </section>
      <section class="podium">{podium_cards}</section>
      <section class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Driver</th>
              <th>Win Probability</th>
              <th>Podium Probability</th>
              <th>Expected Finish</th>
            </tr>
          </thead>
          <tbody>{table_rows}</tbody>
        </table>
      </section>
    </main>
  </body>
</html>
"""


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    prediction_path = Path(args.prediction)
    if not prediction_path.exists():
        if args.allow_missing_input:
            LOGGER.warning("Skipping render step, prediction input missing: %s", prediction_path)
            return 0
        LOGGER.error("render_prediction_page failed, prediction input missing: %s", prediction_path)
        return 1

    try:
        prediction = load_json(prediction_path)
        if not isinstance(prediction, dict):
            raise ValueError("Prediction input must be a JSON object.")
        race_config: dict[str, Any] = {}
        race_config_path = Path(args.race_config)
        if race_config_path.exists():
            raw = load_json(race_config_path)
            if isinstance(raw, dict):
                race_config = raw

        rendered = render_page(prediction, race_config)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    except Exception as exc:
        LOGGER.error("render_prediction_page failed: %s", exc)
        return 1

    LOGGER.info("Rendered prediction page: %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
