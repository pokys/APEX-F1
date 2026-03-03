# APEX-F1

APEX-F1 je deterministický datový pipeline + Monte Carlo simulace pro predikce závodů Formule 1.

Důležité principy:
- žádné LLM predikce výsledků,
- žádný skrytý stav mimo repozitář,
- výstup je pravděpodobnost, ne "jisté" pořadí.

## Veřejný web predikce

- Sdílený link: **https://pokys.github.io/APEX-F1/**
- Publikuje se z workflow `deploy-pages.yml`.
- Jednorázové nastavení v GitHub repozitáři: `Settings -> Pages -> Source: GitHub Actions`.

## Co je hotové

Implementované pipeline skripty:
1. `pipeline/collect_articles.py`
2. `pipeline/ingest_fastf1.py`
3. `pipeline/select_next_gp.py`
4. `pipeline/validate_signals.py`
5. `pipeline/build_features.py`
6. `pipeline/update_ratings.py`
7. `pipeline/backtest_simulation.py`
8. `pipeline/apply_backtest_calibration.py`
9. `pipeline/simulate_race.py`
10. `pipeline/publish_prediction.py`
11. `pipeline/render_prediction_page.py`
12. `pipeline/simulate_weather_scenarios.py`
13. `pipeline/validate_outputs.py`
14. `pipeline/archive_old_signals.py`

## Aktuální chování (důležité)

- Další GP se vybírá automaticky podle data (`select_next_gp.py`).
- Na startu nové sezony (např. 2026), když ještě nejsou odjeté závody, `build_features.py` a `update_ratings.py` fallbacknou na poslední kompletní sezonu (aktuálně 2025).
- Simulace je deterministická (seed + minimálně 5000 simulací).
- Backtest umí dopočítat kalibraci `win_temperature`, která se před simulací automaticky aplikuje.

## Jak pipeline teče

1. `collect_articles.py`
- Načte RSS feedy z `knowledge/feeds.yaml`.
- Přidá nové články do `knowledge/inbox/articles.md`.
- Dedup: `sha256(normalized_title + normalized_url)`.

2. `ingest_fastf1.py`
- Načte tvrdá data z FastF1.
- Zapíše snapshot do `data/raw/fastf1/season_<year>.json`.

3. `select_next_gp.py`
- Vybere nejbližší GP podle kalendáře.
- Aplikuje profil trati z `config/track_profiles.json`.
- Zapíše `config/race_config.json`.

4. Human + AI extrakce signálů
- Člověk projde inbox článků.
- Externí AI/člověk vytvoří strukturované signály.
- Uloží do `knowledge/processed/signals_YYYY-MM-DD.json`.

5. `validate_signals.py`
- Ověří schéma a rozsahy hodnot.

6. `build_features.py`
- Spojí hard data + signály.
- Zapíše `data/processed/features_season_<year>.json`.

7. `update_ratings.py`
- Přepočítá modely jezdců/týmů/strategie/spolehlivosti.
- Zapíše JSON do `models/`.

8. `backtest_simulation.py` (periodicky)
- Ověří kvalitu modelu na historických datech.
- Zapíše `outputs/backtest/backtest_season_<year>.json`.

9. `apply_backtest_calibration.py`
- Přečte doporučenou kalibraci z backtestu.
- Zapíše ji do `config/race_config.json`.

10. `simulate_race.py`
- Spustí deterministickou Monte Carlo simulaci pro jeden scénář.

11. `simulate_weather_scenarios.py`
- Spustí simulaci pro `dry` i `wet` scénář.
- Zapíše:
  - `outputs/prediction_dry.json`
  - `outputs/prediction_wet.json`
  - `outputs/prediction.json` (kompatibilní alias na dry scénář)

12. `publish_prediction.py`
- Znormalizuje finální JSON tvar výstupu.

13. `render_prediction_page.py`
- Vygeneruje HTML přehled predikce s přepínačem `Dry/Wet`.
- Zapíše `outputs/prediction_report.html`.

14. `validate_outputs.py`
- Ověří konzistenci výstupů (sumy pravděpodobností, sezona, formát).

15. `archive_old_signals.py`
- Po odjetých závodech přesune staré signály do `knowledge/processed/archive/`.

## Lokální spuštění

Instalace závislostí:

```bash
python -m pip install --upgrade pip
pip install -r requirements.lock
```

Plný běh ručně:

```bash
python pipeline/collect_articles.py --log-level INFO
python pipeline/ingest_fastf1.py --season 2026 --sessions Q,R --cutoff-date 2026-03-03 --log-level INFO
python pipeline/select_next_gp.py --season 2026 --as-of-date 2026-03-03 --race-config config/race_config.json --log-level INFO
python pipeline/validate_signals.py --signals-dir knowledge/processed --allow-empty --log-level INFO
python pipeline/build_features.py --season 2026 --guardrails-config config/signal_guardrails.json --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --season 2026 --guardrails-config config/signal_guardrails.json --allow-missing-features --log-level INFO
python pipeline/apply_backtest_calibration.py --season 2026 --allow-missing-report --race-config config/race_config.json --log-level INFO
python pipeline/simulate_weather_scenarios.py --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --input outputs/prediction_dry.json --output outputs/prediction_dry.json --allow-missing-input --log-level INFO
python pipeline/publish_prediction.py --input outputs/prediction_wet.json --output outputs/prediction_wet.json --allow-missing-input --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/render_prediction_page.py --prediction outputs/prediction.json --prediction-dry outputs/prediction_dry.json --prediction-wet outputs/prediction_wet.json --race-config config/race_config.json --output outputs/prediction_report.html --allow-missing-input --log-level INFO
python pipeline/validate_outputs.py --log-level INFO
```

## GitHub Actions workflowe

Hlavní workflowe:
- `collect-articles.yml`: sběr článků z RSS
- `ingest-fastf1.yml`: ingest hard dat
- `build-features.yml`: tvorba features
- `update-ratings.yml`: přepočet modelů
- `simulate-race.yml`: výběr GP + simulace + publish
- `archive-signals.yml`: archivace starých signálů
- `backtest.yml`: backtest a kalibrace
- `full-pipeline.yml`: end-to-end běh
- `validate-signals.yml`: validace signal JSON
- `tests.yml`: unit testy
- `deploy-pages.yml`: publikace HTML reportu na GitHub Pages

## Výstupy

Predikce:
- `outputs/prediction.json`
- `outputs/prediction_dry.json`
- `outputs/prediction_wet.json`
- Pole na jezdce: `name`, `win_probability`, `podium_probability`, `expected_finish`
- Validace: `sum(win_probability) ~= 1.0`, `sum(podium_probability) ~= 3.0`

HTML report:
- `outputs/prediction_report.html`
- Obsahuje přepínač scénářů `Dry` / `Wet`.
- Obsahuje přepínač zobrazení `Top 10` / `All Drivers` (na mobilu default `Top 10`).
- Obsahuje přehledové karty (lídr na výhru, lídr na podium, a při `Dry/Wet` i největší weather swing).
- Veřejný URL: **https://pokys.github.io/APEX-F1/**

Konfigurace závodu:
- `config/race_config.json`
- Obsahuje vybraný závod + parametry simulace.

Guardrails pro signály:
- `config/signal_guardrails.json`
- Řídí důvěryhodnost zdrojů, confidence floor, echo-decay a capy dopadu měkkých signálů.

## Determinismus

- Stabilní řazení feedů/událostí/jezdců.
- Fixní seed v simulaci.
- Min. počet simulací vynucen (`>= 5000`).
- Idempotentní sběr článků.
- Veškerý stav je v repozitáři (JSON + workflowy).
- CI instaluje z `requirements.lock`.

## Omezení

- Semantická extrakce článků není automatická v CI (záměrně human-in-the-loop).
- Není zapojené live počasí API.
- RSS může obsahovat i méně relevantní obsah, filtr je při ruční extrakci signálů.

## Další dokumentace

- Provozní postup: `RUNBOOK.md`
- Schéma signálů: `knowledge/processed/README.md`
- Prompt/kontrakt pro externí AI: `knowledge/processed/AI_EXTRACTION_GUIDE.md`
