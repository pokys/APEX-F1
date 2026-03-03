# APEX-F1 Runbook

Operational guide for daily use and race-week execution.

## 1. Daily Operating Mode

Normal automated operation:
- `Collect F1 Articles` runs every 6 hours.
- `Ingest FastF1 Data` runs daily.
- `Build Features` runs daily.
- `Update Ratings` runs daily.
- `Simulate Race` runs daily.
- `Backtest Simulation` runs weekly.

`Full Prediction Pipeline` can be used as one-shot end-to-end run.

## 2. Human-in-the-Loop Signals

The system intentionally requires human curation before soft-news signals affect predictions.

### Workflow

1. Open `knowledge/inbox/articles.md`.
2. Pick relevant F1 articles (skip irrelevant content).
3. Use external tooling/human analysis to produce structured signals JSON.
4. Save as `knowledge/processed/signals_YYYY-MM-DD.json`.
5. Commit and push.

On push:
- `Validate Processed Signals` runs automatically.
- Full pipeline can be triggered manually or by configured triggers.

### Signal Quality Rules

- Prefer technical/reporting content over gossip.
- Use `source_confidence` conservatively for rumor-like sources.
- Attach every signal to `article_hash` (hash of normalized title+url).
- Keep values within allowed ranges (see schema doc).

Schema details: `knowledge/processed/README.md`.
AI extraction prompt and formatting guide: `knowledge/processed/AI_EXTRACTION_GUIDE.md`.

## 3. Race-Week Procedure

Recommended sequence before GP:

1. Ensure latest hard data:
```bash
python pipeline/ingest_fastf1.py --season 2026 --sessions Q,R --cutoff-date YYYY-MM-DD --log-level INFO
```

2. Ensure next GP is selected:
```bash
python pipeline/select_next_gp.py --season 2026 --as-of-date YYYY-MM-DD --race-config config/race_config.json --log-level INFO
```

3. Validate and apply latest signals:
```bash
python pipeline/validate_signals.py --signals-dir knowledge/processed --log-level INFO
python pipeline/build_features.py --season 2026 --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --season 2026 --allow-missing-features --log-level INFO
```

4. Apply backtest calibration:
```bash
python pipeline/apply_backtest_calibration.py --season 2026 --allow-missing-report --race-config config/race_config.json --log-level INFO
```

5. Simulate + publish:
```bash
python pipeline/simulate_race.py --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/validate_outputs.py --log-level INFO
```

## 4. Season Start Behavior (Important)

At the beginning of a new season, data may be incomplete.

Current fallback logic:
- If requested season snapshot exists but has no completed sessions, `build_features.py` falls back to prior season snapshot with completed results.
- `update_ratings.py` then falls back to the latest feature file with non-empty driver rows.

This is expected behavior until enough current-season race data exists.

## 5. Weather Handling

Current state:
- Simulation uses `weather` + `weather_modifier` from race config/profile.
- No automatic local forecast ingestion is implemented yet.

How to adjust manually:
- Edit `config/track_profiles.json` for event/country defaults.
- Or override in `config/race_config.json` before simulation run.

## 6. Article Handling and Source Credibility

Current state:
- RSS links are ingested automatically.
- Semantic extraction into signals is manual.
- Source credibility map is hardcoded in `pipeline/build_features.py`:
  - `the-race`: 0.90
  - `racefans`: 0.86
  - `motorsport`: 0.86
  - `autosport`: 0.85
  - default unknown source: 0.70

Implication:
- Soft data has bounded impact through weighting and aggregation.
- Low-quality sources should receive low `source_confidence` in signal JSON.

## 7. Troubleshooting

### A) Workflow failed with `git push` HTTP 500

Meaning:
- Usually transient GitHub-side error.
- Pipeline logic already succeeded before final push.

Current mitigation:
- Retry logic is implemented in write-heavy workflows (`full-pipeline`, `simulate-race`, `backtest`).

Action:
- Re-run the failed workflow if needed.

### B) `select_next_gp` failed to fetch schedule

Meaning:
- FastF1 backend/API temporary issue.

Behavior:
- Script keeps existing `config/race_config.json` when available.

Action:
- Re-run later, or run with explicit `--season` and `--as-of-date`.

### C) Empty/invalid processed signals

Symptoms:
- `validate_signals.py` errors.

Action:
- Fix schema/ranges in the failing file.
- Re-run validation.

### D) Tests failing in GitHub due import path

Mitigation is already in place:
- `pipeline/__init__.py`
- `tests/conftest.py`
- `PYTHONPATH` set in `tests.yml`

## 8. What Needs Human Decisions

- Which articles are relevant for signal extraction.
- How strong each extracted claim is (`source_confidence`, concern/change values).
- Whether to tune track/weather assumptions before race weekend.

## 9. Pre-Run Checklist

Before major full run:
- `knowledge/feeds.yaml` valid and reachable feeds.
- `knowledge/processed/*.json` passes validation.
- `config/race_config.json` points to intended race.
- Backtest report exists (optional, but preferred for calibration).
- `outputs/prediction.json` validated after run.
