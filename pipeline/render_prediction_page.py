#!/usr/bin/env python3
"""
Render a high-end analytics dashboard for F1 predictions.
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

# Expanded and more robust team color mapping for 2026
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
    "audi": "#52E252",
    "sauber": "#52E252",
    "cadillac": "#FFD700",
    "andretti": "#FFD700",
    "default": "#9bb0c6"
}

def get_team_color(team_name: str) -> str:
    cleaned = str(team_name).lower().strip()
    # Try exact matches or key phrases
    for key, color in TEAM_COLORS.items():
        if key in cleaned:
            return color
    return TEAM_COLORS["default"]

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render prediction dashboard HTML.")
    parser.add_argument("--prediction", default="outputs/prediction.json", help="Single prediction JSON input path.")
    parser.add_argument("--prediction-dry", default="outputs/prediction_dry.json", help="Dry scenario prediction JSON input path.")
    parser.add_argument("--prediction-wet", default="outputs/prediction_wet.json", help="Wet scenario prediction JSON input path.")
    parser.add_argument("--race-config", default="config/race_config.json", help="Race config JSON input path.")
    parser.add_argument("--output", default="outputs/prediction_report.html", help="Rendered HTML output path.")
    parser.add_argument("--allow-missing-input", action="store_true", help="Exit 0 if prediction input is missing.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))

def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default

def parse_prediction_rows(prediction: dict[str, Any]) -> list[dict[str, Any]]:
    drivers = prediction.get("drivers", [])
    rows: list[dict[str, Any]] = []
    for raw in drivers:
        name = str(raw.get("name") or "").strip()
        if not name: continue
        rows.append({
            "name": name,
            "team": str(raw.get("team") or "Unknown"),
            "win_probability": max(0.0, min(1.0, to_float(raw.get("win_probability"), 0.0))),
            "podium_probability": max(0.0, min(1.0, to_float(raw.get("podium_probability"), 0.0))),
            "expected_finish": max(1.0, to_float(raw.get("expected_finish"), 99.0)),
        })
    rows.sort(key=lambda x: (-x["win_probability"], x["expected_finish"], x["name"].lower()))
    return rows

def build_insights_html(dry_rows: list[dict[str, Any]], wet_rows: list[dict[str, Any]] | None, race_config: dict[str, Any]) -> str:
    if not dry_rows: return ""
    
    source_info = "FastF1 Data + AI Signals"
    try:
        ratings_path = Path("models/driver_ratings.json")
        if ratings_path.exists():
            ratings_data = load_json(ratings_path)
            source_info = ratings_data.get("source_summary", source_info)
    except Exception: pass

    grid_source = str(race_config.get("grid_source") or "simulation").lower()
    if grid_source == "qualifying":
        prediction_target, confidence_label, status_class = "SUNDAY RACE", "Verified", "status-high"
        source_details = "Live starting grid detected"
    else:
        prediction_target, confidence_label, status_class = "SATURDAY QUALIFYING", "Estimated", "status-low"
        source_details = "Predicting Q positions"

    cards = [
        (f"Objective: {prediction_target}", confidence_label, source_details, status_class),
        ("Primary Data Source", source_info, "FastF1 Hard Data + AI Signals", ""),
        ("Track Context", str(race_config.get("race") or "GP").replace(" Grand Prix", ""), 
         f"Overtaking: {int((1.0 - race_config.get('overtaking_difficulty', 0.5)) * 100)}% | Wear: {int(race_config.get('track', {}).get('tyre_degradation_factor', 0.5) * 100)}%", "")
    ]

    rendered_cards = "".join([
        f'<article class="insight-card {c[3]}">'
        f'<div class="insight-header"><p class="insight-kicker">{html.escape(c[0])}</p>'
        + (f'<span class="pulse"></span>' if "status-high" in c[3] else '') + '</div>'
        f'<h3 class="insight-value">{html.escape(c[1])}</h3><p class="insight-sub">{html.escape(c[2])}</p></article>'
        for c in cards
    ])
    return f'<section class="insights">{rendered_cards}</section>'

def scenario_block_html(rows: list[dict[str, Any]], scenario_key: str, scenario_label: str, active: bool) -> str:
    p1 = rows[0]
    p2 = rows[1] if len(rows) > 1 else None
    p3 = rows[2] if len(rows) > 2 else None

    def podium_card(row, rank, is_hero=False):
        if not row: return ""
        hero_class = " is-hero" if is_hero else ""
        color = get_team_color(row["team"])
        return f"""
        <article class="podium-card{hero_class}" style="--team-color: {color}">
            <p class="rank">P{rank}</p>
            <div class="podium-header">
                <h3>{html.escape(row['name'])}</h3>
                <span class="team-tag">{html.escape(row['team'])}</span>
            </div>
            <div class="podium-metrics">
                <div class="p-metric"><span>Win</span><strong>{row['win_probability']*100:.1f}%</strong></div>
                <div class="p-metric"><span>Podium</span><strong>{row['podium_probability']*100:.1f}%</strong></div>
            </div>
            <div class="bar-lite"><span style="width:{row['win_probability']*100:.1f}%"></span></div>
        </article>"""

    podium_html = f"""
    <section class="podium-grid">
        {podium_card(p2, 2)}
        {podium_card(p1, 1, True)}
        {podium_card(p3, 3)}
    </section>"""

    table_rows = ""
    for idx, row in enumerate(rows, start=1):
        color = get_team_color(row["team"])
        table_rows += f"""
        <tr class="driver-row" style="--team-color: {color}">
            <td class="td-rank">{idx}</td>
            <td class="td-driver"><strong>{html.escape(row['name'])}</strong><small>{html.escape(row['team'])}</small></td>
            <td class="td-prob">
                <div class="bar-wrap">
                    <div class="bar-main"><span style="width:{row['win_probability']*100:.2f}%"></span></div>
                    <code>{row['win_probability']*100:.2f}%</code>
                </div>
            </td>
            <td class="td-prob">
                <div class="bar-wrap">
                    <div class="bar-podium"><span style="width:{row['podium_probability']*100:.2f}%"></span></div>
                    <code>{row['podium_probability']*100:.2f}%</code>
                </div>
            </td>
            <td class="td-finish"><code>{row['expected_finish']:.2f}</code></td>
        </tr>"""

    active_attr = " is-active" if active else ""
    return f"""
    <section class="scenario-panel{active_attr}" data-scenario="{scenario_key}">
        <div class="scenario-header">
            <span class="scenario-tag">{html.escape(scenario_label)} Analysis</span>
        </div>
        {podium_html}
        <div class="table-container">
            <div class="table-scroll-hint">Scroll horizontally to see more &rarr;</div>
            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr><th>#</th><th>Driver</th><th>Win Prob.</th><th>Podium Prob.</th><th>Exp. Finish</th></tr>
                    </thead>
                    <tbody>{table_rows}</tbody>
                </table>
            </div>
        </div>
    </section>"""

def render_page(prediction: dict[str, Any], race_config: dict[str, Any], prediction_wet: dict[str, Any] | None = None) -> str:
    dry_rows = parse_prediction_rows(prediction)
    wet_rows = parse_prediction_rows(prediction_wet) if isinstance(prediction_wet, dict) else None
    
    current_gp = str(race_config.get("race") or "Next GP")
    location = str(race_config.get("location") or "Circuit")
    gen_at = str(prediction.get("generated_at") or race_config.get("generated_at") or "Unknown")
    
    insights_html = build_insights_html(dry_rows, wet_rows, race_config)
    panels_html = scenario_block_html(dry_rows, "dry", "Dry", True)
    if wet_rows:
        panels_html += scenario_block_html(wet_rows, "wet", "Wet", False)

    toggle_html = ""
    if wet_rows:
        toggle_html = """
        <div class="dashboard-toggles">
            <button class="toggle-btn is-active" data-target="dry">Dry</button>
            <button class="toggle-btn" data-target="wet">Wet</button>
        </div>"""

    # System facts for debug section
    system_facts = {
        "Season": race_config.get("season"),
        "Round": race_config.get("next_round"),
        "Simulations": race_config.get("simulations"),
        "Seed": race_config.get("seed"),
        "Grid Source": race_config.get("grid_source"),
        "Safety Car P": race_config.get("safety_car_probability"),
        "Overtake Diff": race_config.get("overtaking_difficulty"),
        "Generated At": gen_at
    }
    debug_rows = "".join([f"<div><span>{k}:</span><code>{v}</code></div>" for k,v in system_facts.items()])

    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>{html.escape(current_gp)} | APEX-F1</title>
    <link href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700;800&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet"/>
    <style>
        :root {{
            --bg: #06090f; --panel: #0d1117; --grid: #21262d; --ink: #c9d1d9; --muted: #8b949e;
            --accent: #2ad2c9; --success: #238636; --warning: #d29922; --danger: #da3633;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0; font-family: 'Sora', sans-serif; background: var(--bg); color: var(--ink); line-height: 1.5;
            background-image: radial-gradient(circle at 50% -20%, #161b22 0%, var(--bg) 80%);
        }}
        .app {{ max-width: 1200px; margin: 0 auto; padding: 2rem 1rem; padding-bottom: 5rem; }}
        header {{ margin-bottom: 2rem; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 1rem; }}
        .header-main h1 {{ margin: 0; font-size: 2.5rem; font-weight: 800; letter-spacing: -0.02em; color: #fff; }}
        .header-main p {{ margin: 0.5rem 0 0; color: var(--muted); font-family: 'JetBrains Mono', monospace; font-size: 0.9rem; }}
        
        .insights {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 1rem; margin-bottom: 2rem; }}
        .insight-card {{ background: var(--panel); border: 1px solid var(--grid); padding: 1.5rem; border-radius: 12px; }}
        .insight-card.status-high {{ border-top: 3px solid var(--success); }}
        .insight-card.status-low {{ border-top: 3px solid var(--warning); }}
        .insight-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }}
        .insight-kicker {{ margin: 0; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; color: var(--muted); letter-spacing: 0.05em; }}
        .insight-value {{ margin: 0; font-size: 1.25rem; color: #fff; }}
        .insight-sub {{ margin: 0.25rem 0 0; font-size: 0.85rem; color: var(--muted); }}
        
        .pulse {{ width: 8px; height: 8px; background: var(--success); border-radius: 50%; box-shadow: 0 0 0 rgba(35, 134, 54, 0.4); animation: pulse 2s infinite; }}
        @keyframes pulse {{ 0% {{ box-shadow: 0 0 0 0 rgba(35, 134, 54, 0.7); }} 70% {{ box-shadow: 0 0 0 10px rgba(35, 134, 54, 0); }} 100% {{ box-shadow: 0 0 0 0 rgba(35, 134, 54, 0); }} }}

        .dashboard-toggles {{ display: flex; gap: 0.5rem; background: var(--panel); padding: 0.25rem; border-radius: 8px; border: 1px solid var(--grid); }}
        .toggle-btn {{ background: transparent; border: none; color: var(--muted); padding: 0.5rem 1.25rem; border-radius: 6px; cursor: pointer; font-weight: 600; transition: 0.2s; }}
        .toggle-btn.is-active {{ background: var(--grid); color: #fff; }}

        .podium-grid {{ display: grid; grid-template-columns: 1fr 1.2fr 1fr; gap: 1rem; align-items: end; margin-bottom: 2rem; }}
        .podium-card {{ background: var(--panel); border: 1px solid var(--grid); padding: 1.5rem; border-radius: 16px; position: relative; border-left: 4px solid var(--team-color); transition: transform 0.3s; }}
        .podium-card:hover {{ transform: translateY(-5px); }}
        .podium-card.is-hero {{ padding: 2rem 1.5rem; border-bottom: 4px solid var(--team-color); }}
        .podium-card .rank {{ font-weight: 800; color: var(--team-color); font-size: 0.9rem; margin: 0; font-family: 'JetBrains Mono', monospace; }}
        .podium-header h3 {{ margin: 0.5rem 0 0.25rem; font-size: 1.5rem; color: #fff; }}
        .team-tag {{ font-size: 0.75rem; color: var(--muted); font-weight: 600; text-transform: uppercase; }}
        .podium-metrics {{ display: flex; gap: 1.5rem; margin: 1.5rem 0; }}
        .p-metric span {{ display: block; font-size: 0.7rem; color: var(--muted); text-transform: uppercase; font-weight: 700; }}
        .p-metric strong {{ font-size: 1.2rem; color: #fff; font-family: 'JetBrains Mono', monospace; }}
        .bar-lite {{ height: 4px; background: var(--grid); border-radius: 2px; overflow: hidden; }}
        .bar-lite span {{ display: block; height: 100%; background: var(--team-color); }}

        .table-container {{ background: var(--panel); border: 1px solid var(--grid); border-radius: 12px; overflow: hidden; position: relative; }}
        .table-wrapper {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
        .table-scroll-hint {{ display: none; padding: 0.5rem 1rem; font-size: 0.7rem; color: var(--warning); background: #1a1a00; border-bottom: 1px solid var(--grid); font-family: 'JetBrains Mono', monospace; }}
        
        table {{ width: 100%; border-collapse: collapse; min-width: 600px; }}
        th {{ text-align: left; padding: 1rem; font-size: 0.75rem; text-transform: uppercase; color: var(--muted); border-bottom: 1px solid var(--grid); }}
        td {{ padding: 1rem; border-bottom: 1px solid var(--grid); }}
        .driver-row {{ border-left: 4px solid transparent; transition: 0.2s; }}
        .driver-row:hover {{ background: #161b22; border-left-color: var(--team-color); }}
        .td-rank {{ font-family: 'JetBrains Mono', monospace; color: var(--muted); font-weight: 500; }}
        .td-driver strong {{ display: block; color: #fff; font-size: 1rem; }}
        .td-driver small {{ font-size: 0.75rem; color: var(--muted); }}
        .bar-wrap {{ display: flex; align-items: center; gap: 1rem; }}
        .bar-main, .bar-podium {{ height: 6px; flex: 1; background: var(--grid); border-radius: 3px; overflow: hidden; min-width: 80px; }}
        .bar-main span {{ display: block; height: 100%; background: linear-gradient(90deg, var(--accent), #fff); }}
        .bar-podium span {{ display: block; height: 100%; background: var(--team-color); opacity: 0.8; }}
        code {{ font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; color: var(--ink); }}

        .scenario-panel {{ display: none; }}
        .scenario-panel.is-active {{ display: block; }}
        .scenario-header {{ margin-bottom: 1rem; }}
        .scenario-tag {{ background: var(--grid); color: #fff; padding: 0.25rem 0.75rem; border-radius: 4px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }}

        footer {{ margin-top: 4rem; border-top: 1px solid var(--grid); padding-top: 2rem; display: flex; flex-direction: column; align-items: center; gap: 1rem; }}
        .debug-toggle {{ background: transparent; border: 1px solid var(--grid); color: var(--muted); padding: 0.5rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; }}
        .debug-panel {{ display: none; background: #000; border: 1px solid var(--grid); padding: 1rem; border-radius: 8px; width: 100%; max-width: 600px; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; }}
        .debug-panel.is-active {{ display: block; }}
        .debug-panel div {{ display: flex; justify-content: space-between; padding: 0.25rem 0; border-bottom: 1px solid #111; }}
        .debug-panel div span {{ color: var(--muted); }}

        @media (max-width: 800px) {{
            .podium-grid {{ grid-template-columns: 1fr; }}
            .podium-card.is-hero {{ order: -1; }}
            header {{ flex-direction: column; align-items: flex-start; }}
            .header-main h1 {{ font-size: 1.8rem; }}
            .table-scroll-hint {{ display: block; }}
        }}
    </style>
</head>
<body>
    <div class="app">
        <header>
            <div class="header-main">
                <p>APEX-F1 // INTELLIGENCE</p>
                <h1>{html.escape(current_gp)}</h1>
                <p>{html.escape(location)} &bull; {html.escape(str(race_config.get("race_date", "")))}</p>
            </div>
            {toggle_html}
        </header>
        {insights_html}
        {panels_html}
        <footer>
            <button class="debug-toggle" onclick="document.getElementById('debug').classList.toggle('is-active')">SYSTEM_TRACE.LOG</button>
            <div id="debug" class="debug-panel">
                {debug_rows}
            </div>
        </footer>
    </div>
    <script>
        document.querySelectorAll('.toggle-btn').forEach(btn => {{
            btn.addEventListener('click', () => {{
                document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('is-active'));
                btn.classList.add('is-active');
                document.querySelectorAll('.scenario-panel').forEach(p => {{
                    p.classList.toggle('is-active', p.dataset.scenario === btn.dataset.target);
                }});
            }});
        }});
    </script>
</body>
</html>"""

def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(message)s")
    try:
        race_config = load_json(Path(args.race_config))
        prediction = load_json(Path(args.prediction))
        prediction_wet = None
        if Path(args.prediction_wet).exists():
            prediction_wet = load_json(Path(args.prediction_wet))
        
        html_content = render_page(prediction, race_config, prediction_wet)
        Path(args.output).write_text(html_content, encoding="utf-8")
        LOGGER.info("Rendered improved dashboard: %s", args.output)
    except Exception as exc:
        LOGGER.error("Render failed: %s", exc)
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
