#!/usr/bin/env python3
"""
Render a static HTML dashboard from target-aware prediction output.
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

TEAM_COLORS = {
    "red bull": "#3671C6",
    "mercedes": "#27F4D2",
    "ferrari": "#E80020",
    "mclaren": "#FF8000",
    "aston martin": "#229971",
    "alpine": "#0093CC",
    "williams": "#64C4FF",
    "haas": "#B6BABD",
    "racing bulls": "#6692FF",
    "rb": "#6692FF",
    "sauber": "#52E252",
    "audi": "#52E252",
    "default": "#9bb0c6",
}

QUALIFYING_TARGETS = {"qualifying", "sprint_qualifying"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render prediction dashboard HTML.")
    parser.add_argument("--prediction", default="outputs/prediction.json", help="Single prediction JSON input path.")
    parser.add_argument("--prediction-dry", default="outputs/prediction_dry.json", help="Dry scenario prediction JSON input path.")
    parser.add_argument("--prediction-wet", default="outputs/prediction_wet.json", help="Wet scenario prediction JSON input path.")
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


def get_team_color(team_name: str) -> str:
    cleaned = str(team_name or "").strip().lower()
    for key, color in TEAM_COLORS.items():
        if key in cleaned:
            return color
    return TEAM_COLORS["default"]


def parse_prediction_rows(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    target = str(prediction.get("prediction_target") or "race")
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
        row = {
            "name": name,
            "team": str(raw.get("team") or "Unknown"),
            "driver_share": to_float(raw.get("driver_share"), 50.0),
            "team_share": to_float(raw.get("team_share"), 50.0),
            "weekend_form_delta": to_float(raw.get("weekend_form_delta"), 0.0),
        }
        if target in QUALIFYING_TARGETS:
            row["headline_probability"] = max(0.0, min(1.0, to_float(raw.get("pole_probability"), 0.0)))
            row["secondary_probability"] = max(0.0, min(1.0, to_float(raw.get("front_row_probability"), 0.0)))
            row["third_probability"] = max(0.0, min(1.0, to_float(raw.get("top10_probability"), 0.0)))
            row["expected_metric"] = max(1.0, to_float(raw.get("expected_position"), 99.0))
        else:
            row["headline_probability"] = max(0.0, min(1.0, to_float(raw.get("win_probability"), 0.0)))
            row["secondary_probability"] = max(0.0, min(1.0, to_float(raw.get("podium_probability"), 0.0)))
            row["third_probability"] = 0.0
            row["expected_metric"] = max(1.0, to_float(raw.get("expected_finish"), 99.0))
        rows.append(row)

    rows.sort(key=lambda item: (-item["headline_probability"], item["expected_metric"], item["name"].lower()))
    return rows


def manifest_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return '<div class="empty-card">No active weighted inputs.</div>'
    cards = []
    for item in items:
        source = html.escape(str(item.get("source") or "unknown"))
        key = html.escape(str(item.get("source_key") or ""))
        weight = to_float(item.get("weight"), 0.0) * 100.0
        cards.append(
            '<article class="input-card">'
            f'<p class="input-source">{source}</p>'
            f'<p class="input-key">{key}</p>'
            f'<div class="input-bar"><span style="width:{weight:.2f}%"></span></div>'
            f'<p class="input-weight">{weight:.2f}%</p>'
            "</article>"
        )
    return "".join(cards)


def metric_labels(target: str) -> tuple[str, str, str]:
    if target in QUALIFYING_TARGETS:
        return ("Pole", "Front Row", "Top 10")
    return ("Win", "Podium", "Expected")


def scenario_panel_html(prediction: dict[str, Any], scenario_key: str, scenario_label: str, active: bool) -> str:
    target = str(prediction.get("prediction_target") or "race")
    rows = parse_prediction_rows(prediction)
    primary_label, secondary_label, tertiary_label = metric_labels(target)

    top_cards = []
    for idx, row in enumerate(rows[:3], start=1):
        color = get_team_color(row["team"])
        top_cards.append(
            '<article class="hero-card" style="--team-color: {color}">'.format(color=color)
            + f'<p class="hero-rank">P{idx}</p>'
            + f'<h3>{html.escape(row["name"])}</h3>'
            + f'<p class="hero-team">{html.escape(row["team"])}</p>'
            + f'<p class="hero-metric">{primary_label}: {row["headline_probability"] * 100:.2f}%</p>'
            + f'<p class="hero-metric">{secondary_label}: {row["secondary_probability"] * 100:.2f}%</p>'
            + "</article>"
        )

    table_rows = []
    for idx, row in enumerate(rows, start=1):
        color = get_team_color(row["team"])
        tertiary = (
            f'{row["third_probability"] * 100:.2f}%'
            if target in QUALIFYING_TARGETS
            else f'{row["expected_metric"]:.2f}'
        )
        expected_label = "Expected Position" if target in QUALIFYING_TARGETS else "Expected Finish"
        table_rows.append(
            '<tr style="--team-color: {color}">'.format(color=color)
            + f"<td>{idx}</td>"
            + f'<td><strong>{html.escape(row["name"])}</strong><small>{html.escape(row["team"])}</small></td>'
            + f"<td>{row['headline_probability'] * 100:.2f}%</td>"
            + f"<td>{row['secondary_probability'] * 100:.2f}%</td>"
            + f"<td>{tertiary}</td>"
            + f"<td>{row['expected_metric']:.2f}</td>"
            + f"<td>{row['weekend_form_delta']:+.2f}</td>"
            + "</tr>"
        )

    mobile_cards = []
    for row in rows:
        color = get_team_color(row["team"])
        mobile_cards.append(
            '<article class="mobile-driver-card" style="--team-color: {color}">'.format(color=color)
            + f'<div class="mobile-top"><h4>{html.escape(row["name"])}</h4><span>{html.escape(row["team"])}</span></div>'
            + f'<p>{primary_label}: {row["headline_probability"] * 100:.2f}%</p>'
            + f'<p>{secondary_label}: {row["secondary_probability"] * 100:.2f}%</p>'
            + (
                f'<p>{tertiary_label}: {row["third_probability"] * 100:.2f}%</p>'
                if target in QUALIFYING_TARGETS
                else f'<p>{tertiary_label}: {row["expected_metric"]:.2f}</p>'
            )
            + f'<p>{expected_label}: {row["expected_metric"]:.2f}</p>'
            + f'<p>Weekend Delta: {row["weekend_form_delta"]:+.2f}</p>'
            + "</article>"
        )

    active_class = " is-active" if active else ""
    return (
        f'<section class="scenario-panel{active_class}" data-scenario="{scenario_key}">'
        f'<div class="scenario-heading">{html.escape(scenario_label)} scenario</div>'
        f'<section class="hero-grid">{"".join(top_cards)}</section>'
        f'<section class="desktop-table"><table>'
        f"<thead><tr><th>#</th><th>Driver</th><th>{primary_label}</th><th>{secondary_label}</th><th>{tertiary_label}</th><th>Expected</th><th>Weekend Delta</th></tr></thead>"
        f"<tbody>{''.join(table_rows)}</tbody></table></section>"
        f'<section class="mobile-list">{"".join(mobile_cards)}</section>'
        "</section>"
    )


def render_page(prediction: dict[str, Any], race_config: dict[str, Any], prediction_wet: dict[str, Any] | None = None) -> str:
    target = str(prediction.get("prediction_target") or race_config.get("prediction_target") or "race")
    target_label = str(prediction.get("prediction_target_label") or race_config.get("prediction_target_label") or "Race")
    target_output_type = str(prediction.get("target_output_type") or race_config.get("target_output_type") or "race")
    race_name = html.escape(str(prediction.get("race") or race_config.get("race") or "Next GP"))
    generated_at = html.escape(str(prediction.get("generated_at") or race_config.get("generated_at") or ""))
    weekend_format = html.escape(str(prediction.get("weekend_format") or race_config.get("weekend_format") or "standard"))
    target_session_code = html.escape(str(prediction.get("target_session_code") or race_config.get("target_session_code") or "R"))
    available_sessions = prediction.get("simulation", {}).get("available_sessions") or race_config.get("available_sessions") or []
    available_sessions_label = ", ".join(str(code) for code in available_sessions) if available_sessions else "none"
    inputs_used = prediction.get("inputs_used") or race_config.get("inputs_used") or []
    grid_source = html.escape(str(prediction.get("simulation", {}).get("grid_source") or race_config.get("grid_source") or "simulation"))
    simulations = int(to_float(prediction.get("simulation", {}).get("simulations"), to_float(race_config.get("simulations"), 0)))
    signal_count = int(to_float(race_config.get("signal_count"), 0))

    toggle_html = ""
    script_html = ""
    if isinstance(prediction_wet, dict):
        toggle_html = (
            '<div class="scenario-toggle">'
            '<button class="toggle-btn is-active" data-target="dry" type="button">Dry</button>'
            '<button class="toggle-btn" data-target="wet" type="button">Wet</button>'
            "</div>"
        )
        script_html = """
    <script>
      const buttons = Array.from(document.querySelectorAll('.toggle-btn'));
      const panels = Array.from(document.querySelectorAll('.scenario-panel'));
      for (const button of buttons) {
        button.addEventListener('click', () => {
          const target = button.dataset.target;
          for (const other of buttons) {
            other.classList.toggle('is-active', other === button);
          }
          for (const panel of panels) {
            panel.classList.toggle('is-active', panel.dataset.scenario === target);
          }
        });
      }
    </script>
"""

    dry_panel = scenario_panel_html(prediction, "dry", "Dry", True)
    wet_panel = scenario_panel_html(prediction_wet, "wet", "Wet", False) if isinstance(prediction_wet, dict) else ""
    manifest_cards = manifest_html(inputs_used if isinstance(inputs_used, list) else [])

    target_blurb = {
        "qualifying": "System is automatically estimating the next qualifying order from history, practice data and signals.",
        "sprint_qualifying": "System is automatically estimating sprint qualifying from history and the current sprint weekend setup.",
        "sprint": "System is automatically simulating the sprint using the sprint qualifying grid when available.",
        "race": "System is automatically simulating the race using the qualifying grid when available.",
    }.get(target, "System is automatically generating the current prediction target.")

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{race_name} | APEX-F1</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet" />
    <style>
      :root {{
        --bg: #07111b;
        --panel: rgba(15, 27, 40, 0.88);
        --panel-2: rgba(18, 34, 49, 0.92);
        --ink: #ebf4ff;
        --muted: #96afc8;
        --grid: #21415c;
        --accent: #4fe0d7;
        --accent-2: #ff9d57;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: "Sora", sans-serif;
        color: var(--ink);
        background:
          radial-gradient(900px 500px at 0% 0%, rgba(27, 82, 120, 0.45), transparent 60%),
          radial-gradient(700px 400px at 100% 0%, rgba(136, 68, 34, 0.35), transparent 55%),
          linear-gradient(180deg, #07111b 0%, #0a1520 48%, #0d1a28 100%);
      }}
      .wrap {{
        width: min(1200px, 94vw);
        margin: 0 auto;
        padding: 26px 0 42px;
      }}
      .hero {{
        background: var(--panel);
        border: 1px solid var(--grid);
        border-radius: 22px;
        padding: 22px;
        backdrop-filter: blur(10px);
      }}
      h1 {{
        margin: 0 0 6px;
        font-size: clamp(1.8rem, 4vw, 3rem);
      }}
      .subtitle {{
        margin: 0;
        color: var(--muted);
        max-width: 800px;
      }}
      .status-grid {{
        margin-top: 18px;
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 12px;
      }}
      .status-card {{
        background: var(--panel-2);
        border: 1px solid var(--grid);
        border-radius: 16px;
        padding: 14px;
      }}
      .status-kicker {{
        margin: 0 0 8px;
        color: var(--muted);
        font-size: 0.76rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}
      .status-value {{
        margin: 0;
        font-size: 1.15rem;
        font-weight: 700;
      }}
      .meta-strip {{
        margin-top: 16px;
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
      }}
      .chip {{
        border: 1px solid var(--grid);
        border-radius: 999px;
        padding: 7px 12px;
        font-family: "IBM Plex Mono", monospace;
        color: var(--muted);
        background: rgba(9, 18, 28, 0.85);
      }}
      .scenario-toggle {{
        margin-top: 16px;
        display: flex;
        gap: 10px;
      }}
      .toggle-btn {{
        border: 1px solid var(--grid);
        border-radius: 999px;
        background: rgba(15, 31, 45, 0.95);
        color: var(--muted);
        padding: 9px 15px;
        cursor: pointer;
        font-family: "IBM Plex Mono", monospace;
      }}
      .toggle-btn.is-active {{
        color: #08141d;
        background: linear-gradient(90deg, var(--accent), #8cf7ef);
      }}
      .section-title {{
        margin: 26px 0 12px;
        font-size: 1rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
      }}
      .inputs-grid {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
      }}
      .input-card, .empty-card {{
        background: var(--panel);
        border: 1px solid var(--grid);
        border-radius: 16px;
        padding: 14px;
      }}
      .input-source {{
        margin: 0;
        font-weight: 700;
      }}
      .input-key, .input-weight {{
        margin: 6px 0 0;
        color: var(--muted);
        font-family: "IBM Plex Mono", monospace;
        font-size: 0.84rem;
      }}
      .input-bar {{
        margin-top: 10px;
        height: 8px;
        border-radius: 999px;
        overflow: hidden;
        background: #09131d;
        border: 1px solid #1d3247;
      }}
      .input-bar span {{
        display: block;
        height: 100%;
        background: linear-gradient(90deg, var(--accent), #7ff3eb);
      }}
      .scenario-panel {{
        display: none;
      }}
      .scenario-panel.is-active {{
        display: block;
      }}
      .scenario-heading {{
        margin-bottom: 12px;
        color: var(--muted);
        font-family: "IBM Plex Mono", monospace;
      }}
      .hero-grid {{
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
      }}
      .hero-card {{
        background: linear-gradient(165deg, rgba(18, 33, 47, 0.98), rgba(10, 20, 30, 0.98));
        border: 1px solid var(--grid);
        border-left: 4px solid var(--team-color);
        border-radius: 18px;
        padding: 16px;
      }}
      .hero-rank {{
        margin: 0 0 10px;
        color: var(--team-color);
        font-family: "IBM Plex Mono", monospace;
      }}
      .hero-card h3 {{
        margin: 0;
        font-size: 1.5rem;
      }}
      .hero-team, .hero-metric {{
        margin: 8px 0 0;
        color: var(--muted);
      }}
      .desktop-table {{
        margin-top: 16px;
        background: var(--panel);
        border: 1px solid var(--grid);
        border-radius: 18px;
        overflow-x: auto;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        min-width: 760px;
      }}
      th, td {{
        padding: 14px 16px;
        border-bottom: 1px solid rgba(33, 65, 92, 0.8);
        text-align: left;
      }}
      th {{
        color: var(--muted);
        font-size: 0.8rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}
      td small {{
        display: block;
        color: var(--muted);
        margin-top: 4px;
      }}
      .mobile-list {{
        display: none;
      }}
      .mobile-driver-card {{
        background: var(--panel);
        border: 1px solid var(--grid);
        border-left: 4px solid var(--team-color);
        border-radius: 16px;
        padding: 14px;
      }}
      .mobile-top {{
        display: flex;
        justify-content: space-between;
        gap: 10px;
      }}
      .mobile-top h4 {{
        margin: 0;
      }}
      .mobile-driver-card p {{
        margin: 8px 0 0;
        color: var(--muted);
      }}
      @media (max-width: 980px) {{
        .status-grid {{
          grid-template-columns: 1fr 1fr;
        }}
        .inputs-grid {{
          grid-template-columns: 1fr;
        }}
        .hero-grid {{
          grid-template-columns: 1fr;
        }}
        .desktop-table {{
          display: none;
        }}
        .mobile-list {{
          display: grid;
          gap: 10px;
          margin-top: 14px;
        }}
      }}
      @media (max-width: 640px) {{
        .status-grid {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main class="wrap">
      <section class="hero">
        <h1>{race_name}</h1>
        <p class="subtitle">{html.escape(target_blurb)}</p>
        <div class="status-grid">
          <article class="status-card">
            <p class="status-kicker">Now Predicting</p>
            <p class="status-value">{html.escape(target_label)}</p>
          </article>
          <article class="status-card">
            <p class="status-kicker">Target Session</p>
            <p class="status-value">{target_session_code}</p>
          </article>
          <article class="status-card">
            <p class="status-kicker">Weekend Format</p>
            <p class="status-value">{weekend_format.title()}</p>
          </article>
          <article class="status-card">
            <p class="status-kicker">Grid Source</p>
            <p class="status-value">{grid_source}</p>
          </article>
        </div>
        <div class="meta-strip">
          <span class="chip">Generated: {generated_at}</span>
          <span class="chip">Sessions Online: {html.escape(available_sessions_label)}</span>
          <span class="chip">Signals: {signal_count}</span>
          <span class="chip">Simulations: {simulations}</span>
          <span class="chip">Output Type: {html.escape(target_output_type)}</span>
        </div>
        {toggle_html}
      </section>

      <h2 class="section-title">Input Weights</h2>
      <section class="inputs-grid">{manifest_cards}</section>

      <h2 class="section-title">Predictions</h2>
      {dry_panel}
      {wet_panel}
    </main>
{script_html}
  </body>
</html>
"""


def load_prediction_for_render(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any] | None]:
    dry_path = Path(args.prediction_dry)
    wet_path = Path(args.prediction_wet)
    if dry_path.exists() and wet_path.exists():
        dry = load_json(dry_path)
        wet = load_json(wet_path)
        if not isinstance(dry, dict) or not isinstance(wet, dict):
            raise ValueError("Dry/wet prediction input must be JSON objects.")
        return dry, wet

    single_path = Path(args.prediction)
    if single_path.exists():
        single = load_json(single_path)
        if not isinstance(single, dict):
            raise ValueError("Prediction input must be a JSON object.")
        return single, None

    raise FileNotFoundError("Missing prediction input.")


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    try:
        prediction, prediction_wet = load_prediction_for_render(args)
    except FileNotFoundError as exc:
        if args.allow_missing_input:
            LOGGER.warning("Skipping render step, prediction input missing: %s", exc)
            return 0
        LOGGER.error("render_prediction_page failed: %s", exc)
        return 1
    except Exception as exc:
        LOGGER.error("render_prediction_page failed: %s", exc)
        return 1

    try:
        race_config: dict[str, Any] = {}
        race_config_path = Path(args.race_config)
        if race_config_path.exists():
            raw = load_json(race_config_path)
            if isinstance(raw, dict):
                race_config = raw
        rendered = render_page(prediction, race_config, prediction_wet=prediction_wet)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    except Exception as exc:
        LOGGER.error("render_prediction_page failed: %s", exc)
        return 1

    LOGGER.info("Rendered prediction page: %s", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
