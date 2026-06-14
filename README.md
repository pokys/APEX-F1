# APEX-F1

APEX-F1 je deterministicky reprodukovatelná F1 prediction pipeline běžící čistě v GitHubu. Nesází na LLM tipování výsledků. LLM může pomoct jen se strukturovanou extrakcí signálů z článků; samotné predikce vznikají z datové pipeline, ratingů, kalibrace a Monte Carlo simulace.

## Veřejný dashboard

- [https://pokys.github.io/APEX-F1/](https://pokys.github.io/APEX-F1/)

Dashboard ukazuje aktuální cíl predikce, dostupné session, použité vstupy, váhy modelu, datovou čerstvost a suchý i mokrý scénář.

## Co se predikuje

Pipeline si cíl vybírá automaticky podle kalendáře a dostupných session dat:

- standardní víkend:
  - před kvalifikací predikuje `Qualifying`,
  - po kvalifikaci a před závodem predikuje `Race`,
- sprintový víkend:
  - před sprint kvalifikací predikuje `Sprint Qualifying`,
  - po sprint kvalifikaci a před sprintem predikuje `Sprint`,
  - po sprintu a před kvalifikací predikuje `Qualifying`,
  - po kvalifikaci a před závodem predikuje `Race`.

Vybraný cíl se zapisuje do [`config/race_config.json`](config/race_config.json) a používá ho simulace i HTML dashboard.

## Co zlepšuje kvalitu predikcí

Aktuální model se snaží méně hádat z pořadí v kalendáři a více pracovat s tím, co se opravdu odjelo:

- eventy řadí podle `event_date`, ne jen podle `round`, takže zrušené nebo neodjeté závody nerozhodí chronologii sezony,
- klasifikované výsledky typu `Lapped` nebo `+1 Lap` bere jako dokončený závod, ne jako DNF,
- počítá oddělené ratingy pro kvalifikaci a závod: `qualifying_rating`, `race_rating`, `qualifying_team_rating`, `race_team_rating`,
- do feature engineeringu přidává časové rozdíly: kvalifikační gap na nejlepší čas, gap na týmového kolegu, sprint kvalifikační gap a závodní gap na vítěze,
- umí volitelně přidat clean-lap pace metriky z FastF1 přes `--include-lap-metrics`,
- používá recency weighting s efektivním počtem startů, takže novější závody váží víc, ale starší data nezmizí úplně,
- backtest běží walk-forward stylem: každá historická predikce používá jen data dostupná před daným eventem,
- kalibrace nastavuje zvlášť teplotu pro race winner predikce a kvalifikační predikce.

## Data

### Hard data

- FastF1 session snapshoty v `data/raw/fastf1/`,
- kalendářové cache v `data/raw/calendars/`,
- tyre compound data v `data/raw/tyres/`,
- odvozené features v `data/processed/`,
- modelové ratingy v `models/`.

Výchozí ingest pracuje se session `FP1`, `FP2`, `FP3`, `SQ`, `S`, `Q` a `R`.

Lap metrics jsou záměrně vypnuté ve výchozím běhu, protože jsou pomalejší a dražší na načtení. Pro cílený refresh je lze zapnout ručně ve workflow inputu `include_lap_metrics` nebo lokálně přes `--include-lap-metrics`.

### Soft data

- RSS zdroje v [`knowledge/feeds.yaml`](knowledge/feeds.yaml),
- zpracované signály v `knowledge/processed/*.json`.

Soft signály mají omezený vliv přes guardrails v [`config/signal_guardrails.json`](config/signal_guardrails.json). Nemají přebíjet tvrdá timing data.

## Výstupy

### `Qualifying` / `Sprint Qualifying`

JSON výstup obsahuje hlavně:

- `pole_probability`,
- `front_row_probability`,
- `top10_probability`,
- `expected_position`.

### `Race` / `Sprint`

JSON výstup obsahuje hlavně:

- `win_probability`,
- `podium_probability`,
- `expected_finish`.

Kanonické výstupy jsou:

- [`outputs/prediction.json`](outputs/prediction.json),
- [`outputs/prediction_dry.json`](outputs/prediction_dry.json),
- [`outputs/prediction_wet.json`](outputs/prediction_wet.json),
- [`outputs/prediction_report.html`](outputs/prediction_report.html).

## Hlavní pipeline

1. [`pipeline/collect_articles.py`](pipeline/collect_articles.py) načte F1 články do inboxu.
2. [`pipeline/ingest_fastf1.py`](pipeline/ingest_fastf1.py) vytvoří raw FastF1 snapshot sezony.
3. [`pipeline/select_next_gp.py`](pipeline/select_next_gp.py) vybere další GP a traťový profil.
4. [`pipeline/select_prediction_target.py`](pipeline/select_prediction_target.py) vybere `SQ`, `Sprint`, `Qualifying` nebo `Race`.
5. [`pipeline/collect_tyre_compounds.py`](pipeline/collect_tyre_compounds.py) doplní Pirelli compound data, když jsou dostupná.
6. [`pipeline/validate_signals.py`](pipeline/validate_signals.py) ověří strukturu soft signálů.
7. [`pipeline/build_features.py`](pipeline/build_features.py) vytvoří driver/team features z hard dat a guardrailovaných signálů.
8. [`pipeline/update_ratings.py`](pipeline/update_ratings.py) přepočítá ratingy jezdců, týmů, strategie a reliability.
9. [`pipeline/apply_backtest_calibration.py`](pipeline/apply_backtest_calibration.py) aplikuje kalibraci z backtestu.
10. [`pipeline/simulate_weather_scenarios.py`](pipeline/simulate_weather_scenarios.py) spustí suchý a mokrý scénář.
11. [`pipeline/publish_prediction.py`](pipeline/publish_prediction.py) zapíše finální kanonický JSON.
12. [`pipeline/render_prediction_page.py`](pipeline/render_prediction_page.py) vygeneruje HTML dashboard.
13. [`pipeline/validate_outputs.py`](pipeline/validate_outputs.py) ověří matematickou konzistenci výstupů.

## Backtest a kalibrace

Backtest je v [`pipeline/backtest_simulation.py`](pipeline/backtest_simulation.py). Pro každý historický event:

- seřadí eventy podle skutečného `event_date`,
- sestaví features jen z předchozích eventů,
- vyrobí in-memory ratingy,
- simuluje kvalifikaci i závod,
- vyhodnotí pole/winner kvalitu,
- doporučí `recommended_win_temperature` a `recommended_qualifying_temperature`.

Kalibrace se následně aplikuje do [`config/race_config.json`](config/race_config.json). Díky tomu se pravděpodobnosti dají držet realističtější, místo aby model přehnaně věřil favoritům.

## GitHub Actions

Hlavní automatický běh je [`Full Prediction Pipeline`](.github/workflows/full-pipeline.yml). Běží plánovaně, ručně přes `workflow_dispatch` i po relevantních změnách pipeline nebo konfigurace.

Další důležité workflow:

- [`Pipeline Tests`](.github/workflows/tests.yml),
- [`Ingest FastF1 Data`](.github/workflows/ingest-fastf1.yml),
- [`Build Features`](.github/workflows/build-features.yml),
- [`Update Ratings`](.github/workflows/update-ratings.yml),
- [`Simulate Prediction`](.github/workflows/simulate-race.yml),
- [`Backtest Simulation`](.github/workflows/backtest.yml),
- [`Deploy Prediction Page`](.github/workflows/deploy-pages.yml).

Workflow, která zapisují generované výstupy zpět do `main`, sdílí frontu `bot-outputs`, aby si navzájem nepřepisovala artefakty. Deploy stránky běží přes GitHub Pages a používá aktuální `outputs/prediction_report.html`.

## Lokální spuštění

Instalace:

```bash
python -m pip install --upgrade pip
pip install -r requirements.lock
```

Rychlá kontrola:

```bash
python -m pytest -q
python pipeline/validate_outputs.py --log-level INFO
```

Plný lokální přepočet:

```bash
python pipeline/collect_articles.py --log-level INFO
python pipeline/ingest_fastf1.py --log-level INFO
python pipeline/select_next_gp.py --race-config config/race_config.json --log-level INFO
python pipeline/select_prediction_target.py --race-config config/race_config.json --raw-dir data/raw/fastf1 --calendar-cache-dir data/raw/calendars --session-weights config/session_weights.json --signals-dir knowledge/processed --log-level INFO
python pipeline/collect_tyre_compounds.py --calendar-cache-dir data/raw/calendars --source-config config/tyre_sources.json --output-dir data/raw/tyres --log-level INFO
python pipeline/validate_signals.py --signals-dir knowledge/processed --allow-empty --log-level INFO
python pipeline/build_features.py --guardrails-config config/signal_guardrails.json --recency-config config/recency.json --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --guardrails-config config/signal_guardrails.json --allow-missing-features --log-level INFO
python pipeline/apply_backtest_calibration.py --race-config config/race_config.json --allow-missing-report --log-level INFO
python pipeline/simulate_weather_scenarios.py --raw-dir data/raw/fastf1 --recency-config config/recency.json --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/render_prediction_page.py --prediction outputs/prediction.json --prediction-dry outputs/prediction_dry.json --prediction-wet outputs/prediction_wet.json --race-config config/race_config.json --tyres-input data/raw/tyres --output outputs/prediction_report.html --allow-missing-input --log-level INFO
python pipeline/validate_outputs.py --log-level INFO
```

Lap metrics refresh:

```bash
python pipeline/ingest_fastf1.py --include-lap-metrics --log-level INFO
```

Backtest:

```bash
python pipeline/backtest_simulation.py --season 2025 --simulations 2000 --log-level INFO
python pipeline/apply_backtest_calibration.py --season 2026 --race-config config/race_config.json --allow-missing-report --log-level INFO
```

## Známé limity

- Lap metrics nejsou ve výchozím GitHub běhu zapnuté kvůli rychlosti a stabilitě.
- Soft signály jsou pomocný vstup, ne náhrada za timing data.
- Kvalita predikce bude pořád kolísat u nových jezdců, změn týmů a víkendů s málo odjetými session.
- Automatizace je navržená tak, aby po výpadku dat nebo zrušeném závodě pokračovala z dalšího reálně dostupného eventu.
