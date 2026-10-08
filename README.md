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
- do feature engineeringu přidává časové rozdíly: kvalifikační gap na nejlepší čas (vždy Q1 vs Q1, Q2 vs Q2…), gap na rychlejšího týmového kolegu, sprint kvalifikační gap a závodní gap na vítěze,
- tréninky a sprint kvalifikace mají skutečné pořadí a časy (FastF1 lap data, záložně OpenF1),
- komponenty ratingů se počítají jen z reálných vstupů; chybějící vstup se z váženého průměru vynechá místo konstanty,
- spolehlivost je přímo pravděpodobnost DNF z pozorovaného podílu odstoupení se shrinkage k průměru pole,
- sprint se simuluje jako sprint (třetinové riziko DNF, bez strategie zastávek), mokrý scénář používá `wet_rating` jezdců,
- známé penalizace na roštu (FIA/race control, ruční signály) se aplikují na startovní grid,
- používá recency weighting s efektivním počtem startů, takže novější závody váží víc, ale starší data nezmizí úplně,
- backtest běží walk-forward stylem stejným kódem jako produkce a porovnává model s baseline modely,
- kalibrace ladí škálu šumu simulace (ne až výsledné pravděpodobnosti), takže všechny výstupy zůstávají konzistentní.

## Data

### Hard data

- FastF1 session snapshoty v `data/raw/fastf1/`,
- kalendářové cache v `data/raw/calendars/`,
- tyre compound data v `data/raw/tyres/`,
- odvozené features v `data/processed/`,
- modelové ratingy v `models/`.

Výchozí ingest pracuje se session `FP1`, `FP2`, `FP3`, `SQ`, `S`, `Q` a `R`.

Pro `FP1`–`FP3` a `SQ` ingest vždy načítá lap/timing data: Ergast/Jolpica pro ně klasifikaci nemá, FastF1 ji umí spočítat jen z timing dat (SQ) a u tréninků ji pipeline dopočítá z nejrychlejšího platného kola. Pokud F1 live timing pro session data nevrací (stav sezony 2026 v GitHub Actions), použije se [OpenF1](https://openf1.org) (`pipeline/openf1_client.py`): výsledky session, případně nejrychlejší kola. Názvy týmů se sjednotí s FastF1. Session, která má jen seznam jezdců bez pozic a časů, se nepovažuje za dostupnou.

Hotové session starší než 3 dny se z minulého snapshotu přebírají bez stahování a FastF1 HTTP cache se v Actions ukládá přes `actions/cache`.

Kalendář bere přesné UTC začátky session z FastF1 backendu `fastf1`; tabulky časových pásem v `prediction_targeting.py` jsou jen záloha pro kalendář bez časů.

Lap metrics pro `R`/`S` jsou ve výchozím běhu vypnuté. Pro cílený refresh je lze zapnout ve workflow inputu `include_lap_metrics` nebo lokálně přes `--include-lap-metrics`.

### Soft data

- RSS zdroje v [`knowledge/feeds.yaml`](knowledge/feeds.yaml),
- zpracované signály v `knowledge/processed/*.json`.

Soft signály mají omezený vliv přes guardrails v [`config/signal_guardrails.json`](config/signal_guardrails.json). Nemají přebíjet tvrdá timing data. Pokud žádné nejsou, jejich váha se přerozdělí na timing data a dashboard to uvádí.

Inbox článků se nemaže: sekce starší než 14 dní se i se stavem zaškrtávacích políček přesouvají do `knowledge/inbox/archive/`.

### Penalizace, výměny pohonné jednotky, tresty a náhradníci

Fakta o konkrétní GP se zapisují jako typované signály (`grid_penalty`, `pu_element_change`, `race_ban`, `driver_substitution`), schéma je v [`AI_EXTRACTION_GUIDE.md`](knowledge/processed/AI_EXTRACTION_GUIDE.md). Penalizace se aplikují na startovní grid závodu/sprintu (posun o N míst, konec roštu, start z boxů) i na simulované gridy před kvalifikací; náhradníci a tresty mění seznam jezdců dané GP. Dashboard ukazuje každou penalizaci se zdrojem.

Zdroje a jejich spolehlivost:

- **Historie (spolehlivé):** z rozdílu startovní pozice a pozice v kvalifikaci (≥ 3 místa nebo start z boxů) se počítá `grid_penalty_rate` týmu; backtest používá skutečný startovní grid.
- **OpenF1 race control (best effort, automaticky):** `pipeline/import_penalties.py` čte zprávy race control aktuální GP a přenesené tresty z minulé GP a zapisuje `knowledge/processed/penalties_<sezona>_auto.json`. Zachytí jen rozhodnutí, která race control zveřejní textem („3 PLACE GRID PENALTY FOR CAR 23 (ALB)“, „WILL START FROM THE PIT LANE“); formulace se mezi sezonami mění.
- **Dokumenty FIA (nejúplnější, automaticky):** `pipeline/import_fia_documents.py` stahuje z fia.com PDF rozhodnutí stewardů („Infringement“/„Decision“) aktuální GP a závodní rozhodnutí minulé GP (přenesené tresty) a čte z nich posuny na roštu, start z boxů a konec roštu – hlavně penalizace za výměny prvků pohonné jednotky a změny v parc fermé, které race control nehlásí. Čistý textový parser (pypdf), žádná AI; už přečtené dokumenty si pamatuje, takže každou hodinu stahuje jen nové. Zapisuje `knowledge/processed/penalties_<sezona>_fia.json`. Když stejný trest zachytí i race control, počítá se jen verze z FIA dokumentu.
- **Ruční signály:** cokoli, co automatika nezachytí (zákaz startu, náhradník), patří do `knowledge/processed/penalties_<sezona>.json`.

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
5. [`pipeline/collect_weather.py`](pipeline/collect_weather.py) stáhne předpověď deště pro každou session z Open-Meteo (souřadnice tratí v [`config/circuits.json`](config/circuits.json)); best effort.
6. [`pipeline/collect_tyre_compounds.py`](pipeline/collect_tyre_compounds.py) doplní Pirelli compound data, když jsou dostupná.
7. [`pipeline/validate_signals.py`](pipeline/validate_signals.py) ověří strukturu soft signálů.
8. [`pipeline/build_features.py`](pipeline/build_features.py) vytvoří driver/team features z hard dat a guardrailovaných signálů.
9. [`pipeline/update_ratings.py`](pipeline/update_ratings.py) přepočítá ratingy jezdců, týmů, strategie a reliability.
10. [`pipeline/apply_backtest_calibration.py`](pipeline/apply_backtest_calibration.py) aplikuje kalibraci z backtestu.
11. [`pipeline/simulate_weather_scenarios.py`](pipeline/simulate_weather_scenarios.py) spustí suchý a mokrý scénář.
12. [`pipeline/publish_prediction.py`](pipeline/publish_prediction.py) zapíše finální kanonický JSON.
13. [`pipeline/prediction_history.py`](pipeline/prediction_history.py) uloží headline pravděpodobnosti aktuální fáze víkendu (cíl + odjeté session) do `outputs/prediction_history.json`.
14. [`pipeline/render_prediction_page.py`](pipeline/render_prediction_page.py) vygeneruje HTML dashboard (pruhy pravděpodobností, timeline víkendu v lokálním čase s odpočtem, ▲▼ změny proti stavu před poslední session, riziko deště a doporučený suchý/mokrý scénář).
15. [`pipeline/validate_outputs.py`](pipeline/validate_outputs.py) ověří matematickou konzistenci výstupů.

## Backtest a kalibrace

Backtest je v [`pipeline/backtest_simulation.py`](pipeline/backtest_simulation.py). Pro každý historický event:

- seřadí eventy podle skutečného `event_date`,
- sestaví features jen z předchozích eventů,
- vyrobí in-memory ratingy,
- vyrobí ratingy stejným kódem jako produkce (`update_ratings.build_rating_models`, včetně blendingu s minulou sezonou),
- simuluje kvalifikaci i závod (závod ze skutečného startovního gridu),
- najde škálu šumu simulace (`recommended_qualifying_noise_scale`, `recommended_race_noise_scale`) mřížkovým hledáním, které mřížku rozšíří, když optimum leží na jejím okraji,
- porovná model s baseline modely: rovnoměrné rozdělení, poleman/vítěz minulé GP, pořadí v šampionátu.

Samotný model na sezoně 2026 baseline „pořadí v šampionátu“ nepřekonal. Publikovaná predikce je proto pevná směs 50/50 s modelem pořadí šampionátu, losovaná v každé simulaci (všechny výstupy zůstávají konzistentní). Váha je zvolená předem, ne laděná: laděná váha v leave-one-out neobstála.

Kalibrace se aplikuje do [`config/race_config.json`](config/race_config.json) z reportu aktuální sezony, pokud má aspoň 8 závodů, jinak z minulé sezony. Brány kvality ([`config/backtest_quality_gates.json`](config/backtest_quality_gates.json)) vyžadují, aby kalibrovaný log-loss byl nižší než u nejlepší baseline.

### Track record

`pipeline/track_record.py` archivuje poslední predikci před začátkem každé session do `outputs/archive/<sezona>/<kolo>_<session>.json` a po zveřejnění výsledků ji vyhodnotí do `outputs/track_record.json`. Dashboard ukazuje úspěšnost favorita a pravděpodobnost, kterou model dal skutečnému vítězi.

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
python pipeline/collect_weather.py --race-config config/race_config.json --log-level INFO
python pipeline/import_penalties.py --race-config config/race_config.json --log-level INFO
python pipeline/import_fia_documents.py --race-config config/race_config.json --log-level INFO
python pipeline/select_prediction_target.py --race-config config/race_config.json --raw-dir data/raw/fastf1 --calendar-cache-dir data/raw/calendars --session-weights config/session_weights.json --signals-dir knowledge/processed --log-level INFO
python pipeline/collect_tyre_compounds.py --calendar-cache-dir data/raw/calendars --source-config config/tyre_sources.json --output-dir data/raw/tyres --log-level INFO
python pipeline/validate_signals.py --signals-dir knowledge/processed --allow-empty --log-level INFO
python pipeline/build_features.py --guardrails-config config/signal_guardrails.json --recency-config config/recency.json --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --guardrails-config config/signal_guardrails.json --allow-missing-features --log-level INFO
python pipeline/apply_backtest_calibration.py --race-config config/race_config.json --allow-missing-report --log-level INFO
python pipeline/simulate_weather_scenarios.py --raw-dir data/raw/fastf1 --recency-config config/recency.json --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/track_record.py --log-level INFO
python pipeline/prediction_history.py --log-level INFO
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

- F1 live timing pro sezonu 2026 v GitHub Actions nevrací data; tréninky a SQ proto závisí na OpenF1.
- Výměny prvků pohonné jednotky z dokumentů FIA je nutné zadávat ručně.
- Na sezoně 2026 je přínos modelu oproti pořadí v šampionátu malý (log-loss 1,95 vs 1,97 pro pole, 1,73 vs 1,77 pro vítěze); backtest má jen 15 závodů.
- Soft signály jsou pomocný vstup, ne náhrada za timing data.
- Kvalita predikce bude pořád kolísat u nových jezdců, změn týmů a víkendů s málo odjetými session.
- Automatizace je navržená tak, aby po výpadku dat nebo zrušeném závodě pokračovala z dalšího reálně dostupného eventu.
