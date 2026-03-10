# APEX-F1

APEX-F1 je deterministicky reprodukovatelný F1 prediction pipeline běžící čistě v GitHubu. Nesází na LLM tipování výsledků. LLM slouží jen jako pomocník pro strukturovanou extrakci signálů z článků. Samotné predikce vznikají pouze z datové pipeline a Monte Carlo simulace.

## Veřejný web

- Dashboard: [https://pokys.github.io/APEX-F1/](https://pokys.github.io/APEX-F1/)

Na webu je vždy vidět:
- co se právě predikuje,
- pro jaký víkendový formát,
- které session už jsou k dispozici,
- jaké vstupy a váhy model právě používá,
- suchý a mokrý scénář.

## Co systém právě predikuje

Pipeline se rozhoduje automaticky podle kalendáře a dostupných session dat:

- Standard weekend:
  - před `Q` predikuje `Qualifying`
  - po `Q` a před `R` predikuje `Race`
- Sprint weekend:
  - před `SQ` predikuje `Sprint Qualifying`
  - po `SQ` a před `S` predikuje `Sprint`
  - po `S` a před `Q` predikuje `Qualifying`
  - po `Q` a před `R` predikuje `Race`

Tato volba se zapisuje do [`config/race_config.json`](config/race_config.json) a dál ji používá simulace i web.

## Jaká data systém používá

### Hard data

- `FastF1`
- historické výsledky a session snapshoty
- verzovaný season calendar cache v `data/raw/calendars/`
- průběžně načtené session:
  - `FP1`
  - `FP2`
  - `FP3`
  - `SQ`
  - `S`
  - `Q`
  - `R`

Výchozí ingest běží právě přes tuto sadu session, aby měl systém dost dat pro automatické přepínání cíle predikce.

### Soft data

- RSS články z `knowledge/feeds.yaml`
- ručně zpracované AI/Human signály v `knowledge/processed/*.json`

Soft data mají omezený a kontrolovaný dopad přes guardrails. Nepřebíjí tvrdá timing data.

## Výstupy podle typu predikce

### Qualifying / Sprint Qualifying

Výstup obsahuje:
- `pole_probability`
- `front_row_probability`
- `top10_probability`
- `expected_position`

### Sprint / Race

Výstup obsahuje:
- `win_probability`
- `podium_probability`
- `expected_finish`

Web i JSON vždy ukazují správný typ výstupu pro aktuální `prediction_target`.

## Vstupy a váhy

Zdrojové váhy jsou verzované v [`config/session_weights.json`](config/session_weights.json).

Pipeline z nich pro každou situaci sestaví manifest vstupů:
- historie kvalifikací,
- historie závodů,
- tréninky,
- `SQ`,
- sprint,
- hotová kvalifikace,
- aktivní signály.

Tento manifest se zapisuje do výstupu jako `inputs_used` a web ho zobrazuje přímo uživateli.

## Jak teče pipeline

1. [`pipeline/collect_articles.py`](pipeline/collect_articles.py)
   Sbírá F1 články do inboxu.
2. [`pipeline/ingest_fastf1.py`](pipeline/ingest_fastf1.py)
   Načte session data a vytvoří raw snapshot sezony.
3. [`pipeline/select_next_gp.py`](pipeline/select_next_gp.py)
   Vybere následující GP a traťový profil. Kalendář bere prioritně z live schedule, při výpadku z verzovaného cache v `data/raw/calendars/`.
4. [`pipeline/select_prediction_target.py`](pipeline/select_prediction_target.py)
   Automaticky určí, zda se má predikovat `SQ`, `Sprint`, `Qualifying` nebo `Race`.
5. [`pipeline/build_features.py`](pipeline/build_features.py)
   Postaví features z tvrdých dat a soft signálů.
6. [`pipeline/update_ratings.py`](pipeline/update_ratings.py)
   Přepočítá ratingy jezdců, týmů, strategie a reliability.
7. [`pipeline/simulate_weather_scenarios.py`](pipeline/simulate_weather_scenarios.py)
   Spustí suchý i mokrý scénář nad správným typem predikce.
8. [`pipeline/publish_prediction.py`](pipeline/publish_prediction.py)
   Zapíše finální kanonický JSON výstup.
9. [`pipeline/render_prediction_page.py`](pipeline/render_prediction_page.py)
   Vygeneruje HTML dashboard.
10. [`pipeline/validate_outputs.py`](pipeline/validate_outputs.py)
    Zkontroluje, že pravděpodobnosti dávají matematicky smysl.

## GitHub Actions

Hlavní automatický běh je:
- [`Full Prediction Pipeline`](.github/workflows/full-pipeline.yml)

Tento workflow:
- běží na cron schedule,
- běží ručně přes `workflow_dispatch`,
- po změně relevantních dat a pipeline souborů přepočítá výstupy,
- po úspěchu commituje nové artefakty do repa.

Predikční refresh běží i přes:
- [`Simulate Prediction`](.github/workflows/simulate-race.yml)

Deploy veřejného webu zajišťuje GitHub Pages workflow.

## Lokální spuštění

Instalace:

```bash
python -m pip install --upgrade pip
pip install -r requirements.lock
```

Plný lokální běh:

```bash
python pipeline/collect_articles.py --log-level INFO
python pipeline/ingest_fastf1.py --log-level INFO
python pipeline/select_next_gp.py --race-config config/race_config.json --log-level INFO
python pipeline/select_prediction_target.py --race-config config/race_config.json --raw-dir data/raw/fastf1 --session-weights config/session_weights.json --signals-dir knowledge/processed --log-level INFO
python pipeline/build_features.py --guardrails-config config/signal_guardrails.json --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --guardrails-config config/signal_guardrails.json --allow-missing-features --log-level INFO
python pipeline/apply_backtest_calibration.py --race-config config/race_config.json --allow-missing-report --log-level INFO
python pipeline/simulate_weather_scenarios.py --raw-dir data/raw/fastf1 --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/render_prediction_page.py --prediction outputs/prediction.json --prediction-dry outputs/prediction_dry.json --prediction-wet outputs/prediction_wet.json --race-config config/race_config.json --output outputs/prediction_report.html --allow-missing-input --log-level INFO
python pipeline/validate_outputs.py --log-level INFO
```

## Co ještě není dotažené

Repo už umí automaticky rozlišit, co se má predikovat. Další velký krok je dostat `FP1/FP2/FP3`, `SQ` a segmenty `Q1/Q2/Q3` hlouběji do feature engineeringu, nejen do target selection a víkendových úprav v simulaci.
