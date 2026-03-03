# APEX-F1

Automated Probabilistic Execution for Formula 1 predictions.

This repository is a deterministic data pipeline + Monte Carlo simulation system.

Important constraints:
- No LLM-generated race predictions.
- No hidden runtime state outside this repository.
- Predictions are probabilities, not deterministic rankings.

## What Is Implemented

Implemented pipeline components:
1. `pipeline/collect_articles.py`
2. `pipeline/ingest_fastf1.py`
3. `pipeline/build_features.py`
4. `pipeline/update_ratings.py`
5. `pipeline/simulate_race.py`
6. `pipeline/publish_prediction.py`
7. `pipeline/select_next_gp.py`
8. `pipeline/validate_signals.py`
9. `pipeline/validate_outputs.py`
10. `pipeline/backtest_simulation.py`
11. `pipeline/apply_backtest_calibration.py`

GitHub Actions workflows are configured for independent steps and full end-to-end execution.

## Current Behavior (Important)

- Next GP is auto-selected by date (`select_next_gp.py`), using local snapshot first, then FastF1 schedule fallback.
- At start of a new season (e.g. early 2026), if there are no completed race results yet, feature/rating generation falls back to the latest season with completed data (currently 2025).
- Simulation remains deterministic with fixed seed and minimum simulation count.
- Backtest report provides a recommended `win_temperature`, which is auto-applied before simulation.

## Articles, Weather, and Signals

Article pipeline:
- Automatic: RSS collection into `knowledge/inbox/articles.md`.
- Not automatic: semantic extraction from article text.
- To affect prediction, article insights must be converted to structured JSON in `knowledge/processed/*.json`.

Weather:
- Current simulation uses `weather` + `weather_modifier` from `config/race_config.json` / `config/track_profiles.json`.
- There is no automatic live local weather ingestion yet.

## Repository Layout

The core layout follows project specification:

```text
apex-f1/
├── pipeline/
├── knowledge/
│   ├── feeds.yaml
│   ├── inbox/articles.md
│   └── processed/
├── data/
│   ├── raw/
│   └── processed/
├── models/
├── outputs/
├── config/
├── requirements.txt
└── .github/workflows/
```

## Pipeline Data Flow

1. `collect_articles.py`
   - Loads `knowledge/feeds.yaml`
   - Fetches RSS feeds
   - Deduplicates by `sha256(normalized_title + normalized_url)`
   - Appends to `knowledge/inbox/articles.md`

2. `ingest_fastf1.py`
   - Pulls FastF1 schedule/session results
   - Writes `data/raw/fastf1/season_<year>.json`

3. `select_next_gp.py`
   - Resolves next upcoming GP by date
   - Applies track profile overrides
   - Writes `config/race_config.json`

4. Human + external AI extraction
   - Human reviews inbox, produces structured signals
   - Commits JSON files into `knowledge/processed/`

5. `validate_signals.py`
   - Schema/range checks before model pipeline

6. `build_features.py`
   - Aggregates hard data + processed signals
   - Writes `data/processed/features_season_<year>.json`

7. `update_ratings.py`
   - Computes driver/team/strategy/reliability ratings
   - Writes JSONs in `models/`

8. `backtest_simulation.py` (periodic)
   - Evaluates historical performance and calibration metrics
   - Writes `outputs/backtest/backtest_season_<year>.json`

9. `apply_backtest_calibration.py`
   - Reads backtest recommendation
   - Writes `win_temperature` and calibration metadata into `config/race_config.json`

10. `simulate_race.py`
    - Runs deterministic Monte Carlo simulation
    - Writes `outputs/prediction.json` (raw simulation payload)

11. `publish_prediction.py`
    - Canonicalizes output payload shape
    - Keeps published output in `outputs/prediction.json`

12. `validate_outputs.py`
    - Validates probability invariants and artifact consistency

## Local Execution

Install dependencies:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Run full chain manually:

```bash
python pipeline/collect_articles.py --log-level INFO
python pipeline/ingest_fastf1.py --season 2026 --sessions Q,R --cutoff-date 2026-03-03 --log-level INFO
python pipeline/select_next_gp.py --season 2026 --as-of-date 2026-03-03 --race-config config/race_config.json --log-level INFO
python pipeline/validate_signals.py --signals-dir knowledge/processed --allow-empty --log-level INFO
python pipeline/build_features.py --season 2026 --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --season 2026 --allow-missing-features --log-level INFO
python pipeline/apply_backtest_calibration.py --season 2026 --allow-missing-report --race-config config/race_config.json --log-level INFO
python pipeline/simulate_race.py --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/validate_outputs.py --log-level INFO
```

## GitHub Actions

Primary workflows:
- `collect-articles.yml`: RSS ingestion to inbox (scheduled + manual)
- `archive-signals.yml`: archive stale processed signals after completed races
- `ingest-fastf1.yml`: hard race data snapshot
- `build-features.yml`: features from hard data + signals
- `update-ratings.yml`: rating model generation
- `simulate-race.yml`: GP selection + calibration + simulation + publish
- `full-pipeline.yml`: complete end-to-end pipeline
- `validate-signals.yml`: gate for processed signal quality
- `tests.yml`: unit tests (`pytest`)
- `backtest.yml`: historical backtest metrics

Manual dispatch forms:
- Inputs are optional for standard runs.
- Running with empty fields is valid.

## Outputs and Contracts

Published prediction:
- `outputs/prediction.json`
- Required top-level keys: `race`, `generated_at`, `drivers`
- Driver keys: `name`, `win_probability`, `podium_probability`, `expected_finish`
- Validation invariant: `sum(win_probability) ~= 1.0`, `sum(podium_probability) ~= 3.0`

Race config:
- `config/race_config.json`
- Includes selected event fields (`season`, `next_round`, `race`, `race_date`) and simulation parameters.

Signals contract:
- See `knowledge/processed/README.md`.
- AI extraction instructions:
  - See `knowledge/processed/AI_EXTRACTION_GUIDE.md`.

## Determinism Rules

Deterministic design choices:
- Stable sorting for feeds/events/drivers where applicable.
- Seeded RNG in simulation (`seed` in race config).
- Minimum simulation count enforced (`>= 5000` in simulation workflow).
- Idempotent append logic for article inbox.
- Model updates and outputs fully file-based, versioned in git.

Note:
- Dependency versions are currently bounded, not fully locked (pin/lock can be added later for stronger long-term reproducibility).

## Known Limitations

- No automatic article semantic extraction in CI (by design; human-in-the-loop).
- No automatic live weather forecast ingestion yet.
- Source credibility map is static in code (`build_features.py`).
- RSS feeds may include non-F1 or low-value stories; filtering is currently manual during signal extraction.

Signal lifecycle note:
- Active files in `knowledge/processed/` are intended for upcoming race influence.
- Historical files are moved to `knowledge/processed/archive/` by the archive workflow/step.

## Additional Documentation

- Operations runbook: `RUNBOOK.md`
- Processed signals schema and examples: `knowledge/processed/README.md`
- External AI extraction guide: `knowledge/processed/AI_EXTRACTION_GUIDE.md`
