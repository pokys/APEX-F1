# APEX-F1 Runbook

Praktický provozní návod pro automatický provoz.

## 1. Co systém dělá bez zásahu člověka

Pipeline běží v GitHub Actions a sama:
- stáhne nové články,
- načte nové FastF1 session snapshoty,
- vybere nejbližší relevantní GP,
- určí, co se právě má predikovat,
- přepočítá features a ratingy,
- vygeneruje suchý i mokrý scénář,
- vyrenderuje web,
- nasadí veřejnou stránku přes GitHub Pages.

Kalendářová vrstva má vlastní fallback:
- live schedule fetch,
- verzovaný cache v `data/raw/calendars/`,
- snapshot calendar ve FastF1 raw datech.

Veřejná stránka:
- [https://pokys.github.io/APEX-F1/](https://pokys.github.io/APEX-F1/)

## 2. Automatické rozhodnutí: co se právě predikuje

Rozhodovací logika je v [`pipeline/select_prediction_target.py`](pipeline/select_prediction_target.py).

### Standard weekend

- před `Q` => `Qualifying`
- po `Q` a před `R` => `Race`

### Sprint weekend

- před `SQ` => `Sprint Qualifying`
- po `SQ` a před `S` => `Sprint`
- po `S` a před `Q` => `Qualifying`
- po `Q` a před `R` => `Race`

Skript zapisuje do [`config/race_config.json`](config/race_config.json):
- `prediction_target`
- `prediction_target_label`
- `target_session_code`
- `target_output_type`
- `available_sessions`
- `weekend_format`
- `inputs_used`
- `fixed_grid` a `grid_source`, pokud už existuje relevantní grid

Pokud FastF1 raw snapshot ještě neobsahuje konkrétní event, skript si bere `event_format` z calendar cache. Tím pádem sprint víkendy fungují správně ještě předtím, než dorazí kompletní raw event data.

## 3. Co zobrazuje web

Web report je kontrolní panel. Nahoře má být vždy jasně vidět:
- co se právě predikuje,
- jaký je formát víkendu,
- které session už jsou online,
- odkud přišel grid,
- jaké vstupy a jaké váhy byly použity,
- kolik signálů bylo započítáno.

### Typy výstupu

Pokud je target:
- `Qualifying` nebo `Sprint Qualifying`, web ukazuje:
  - `Pole`
  - `Front Row`
  - `Top 10`
  - `Expected Position`

Pokud je target:
- `Sprint` nebo `Race`, web ukazuje:
  - `Win`
  - `Podium`
  - `Expected Finish`

Web má zároveň přepínač `Dry/Wet`.

## 4. Odkud se berou váhy

Váhy zdrojů jsou v:
- [`config/session_weights.json`](config/session_weights.json)

Používají se pro sestavení `inputs_used`, které se zobrazují i na webu.

Typické zdroje:
- `history_qualifying`
- `history_race`
- `fp1`
- `fp2`
- `fp3`
- `sprint_history`
- `sq_result`
- `qualifying_result`
- `signals`

## 5. FastF1 ingest

Výchozí ingest načítá:
- `FP1`
- `FP2`
- `FP3`
- `SQ`
- `S`
- `Q`
- `R`

To je důležité pro automatické přepínání cíle predikce i pro víkendové úpravy simulace.

- `FP1`–`FP3` a `SQ` se načítají s lap daty (jinak nemají pozice). Když F1 live timing data nemá, ingest použije OpenF1; vypnout lze `--no-openf1`.
- Session se jen seznamem jezdců (bez pozic a časů) se do snapshotu neuloží.
- Hotové session starší než 3 dny se přebírají z minulého snapshotu; FastF1 cache se v Actions ukládá přes `actions/cache`.
- U `Q`, `R`, `S`, `SQ` se z OpenF1 (směsi pneumatik, déšť) zapisuje příznak `wet`, ze kterého vzniká `wet_rating`.

## 6. Human-in-the-loop signály

Signály z článků nejsou generované v GitHub Actions. GitHub Actions je jen konzumují.

Postup:
1. RSS collector přidá články do [`knowledge/inbox/articles.md`](knowledge/inbox/articles.md).
2. Člověk nebo externí AI z nich vytvoří strukturovaný JSON.
3. JSON se uloží do `knowledge/processed/`.
4. Pipeline signály zvaliduje a započítá.

Signal count se propisuje i do `race_config` a webu. Když žádné signály nejsou, web to uvádí a jejich váha se přerozdělí.

Inbox se nemaže: sekce starší než 14 dní přesouvá `collect_articles.py` do `knowledge/inbox/archive/articles_YYYY-MM.md` i se zaškrtnutím.

### Penalizace, výměny PU, tresty, náhradníci

- Automaticky: krok `Import grid penalties from race control` (`pipeline/import_penalties.py`) zapisuje `knowledge/processed/penalties_<sezona>_auto.json`.
- Ručně: výměny prvků PU a penalizace z dokumentů FIA zapsat do `knowledge/processed/penalties_<sezona>.json` podle sekce 10 v `AI_EXTRACTION_GUIDE.md` a zvalidovat `python pipeline/validate_signals.py`.
- Náhradník za jezdce: signál `driver_substitution`; nový jezdec dostane výchozí rating (úroveň týmového kolegy posunutá o rozdíl 25. percentilu a mediánu pole).
- `select_prediction_target.py` zapíše do `race_config` `race_grid_penalties`, `sprint_grid_penalties` a pro aktuální cíl `grid_penalties`; pevný grid pak má `grid_source` `qualifying+penalties`.

## 7. Běžné workflow

### Full Prediction Pipeline

Soubor:
- [`.github/workflows/full-pipeline.yml`](.github/workflows/full-pipeline.yml)

Spouští:
- cron
- ruční dispatch
- push relevantních souborů

Dělá kompletní end-to-end běh.

Mezi kroky jsou i `Archive and score predictions` (track record v `outputs/track_record.json`) a import penalizací.

### Backtest Simulation

Soubor:
- [`.github/workflows/backtest.yml`](.github/workflows/backtest.yml)

Běží pro aktuální sezonu, report commitne a teprve potom spustí brány kvality. Červený běh znamená, že kalibrovaný model nepřekonal nejlepší baseline; report je i tak uložený a kalibrace se z něj použije.

### Simulate Prediction

Soubor:
- [`.github/workflows/simulate-race.yml`](.github/workflows/simulate-race.yml)

Slouží pro rychlý refresh predikčních výstupů bez plného ingestu článků a feature rebuild řetězce.

### GitHub Pages

Pokud by Pages deploy selhal:
- `Settings -> Pages -> Source: GitHub Actions`

## 8. Lokální příkazy

Plný lokální běh:

```bash
python pipeline/collect_articles.py --log-level INFO
python pipeline/ingest_fastf1.py --log-level INFO
python pipeline/select_next_gp.py --race-config config/race_config.json --log-level INFO
python pipeline/import_penalties.py --race-config config/race_config.json --log-level INFO
python pipeline/select_prediction_target.py --race-config config/race_config.json --raw-dir data/raw/fastf1 --session-weights config/session_weights.json --signals-dir knowledge/processed --log-level INFO
python pipeline/build_features.py --guardrails-config config/signal_guardrails.json --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --guardrails-config config/signal_guardrails.json --allow-missing-features --log-level INFO
python pipeline/apply_backtest_calibration.py --race-config config/race_config.json --allow-missing-report --log-level INFO
python pipeline/simulate_weather_scenarios.py --raw-dir data/raw/fastf1 --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/track_record.py --log-level INFO
python pipeline/render_prediction_page.py --prediction outputs/prediction.json --prediction-dry outputs/prediction_dry.json --prediction-wet outputs/prediction_wet.json --race-config config/race_config.json --output outputs/prediction_report.html --allow-missing-input --log-level INFO
python pipeline/validate_outputs.py --log-level INFO
```

## 9. Fallbacky a očekávané chování

### Začátek sezony

Když aktuální sezona ještě nemá odjetá data:
- features můžou fallbacknout na poslední použitelnou sezonu,
- ratings můžou fallbacknout na poslední použitelná features.

To je očekávané chování.

### Kalendář nebo session ještě nejsou online

Když raw snapshot ještě neobsahuje cílový závod:
- `select_prediction_target.py` nespadne,
- použije konfiguraci závodu a zvolí target bez session-specific vstupů,
- web stále ukáže, že session zatím nejsou k dispozici.

### GitHub push 500

Pokud workflow failne na `git push` HTTP 500:
- obvykle jde o dočasný GitHub problém,
- workflow už má retry logiku,
- případně stačí rerun.

## 10. Co ještě chybí

Další kroky:
- long-run tempo z tréninků (medián čistých kol podle směsi a stáří pneumatik z OpenF1 laps/stints) jako vstup závodní predikce,
- nahradit ručně laděné koeficienty ratingů fitovaným modelem (Bradley–Terry / Plackett–Luce),
- automatický parser dokumentů FIA (PDF) pro výměny prvků pohonné jednotky,
- vyhodnotit sprint a sprint kvalifikaci v backtestu zvlášť.
