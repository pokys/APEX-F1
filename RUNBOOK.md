# APEX-F1 Runbook

Praktický provozní návod pro každodenní běh a závodní víkend.

## 1. Denní režim

Běžná automatika:
- `Collect F1 Articles`: každých 6 hodin
- `Ingest FastF1 Data`: denně
- `Build Features`: denně
- `Update Ratings`: denně
- `Simulate Race`: denně
- `Backtest Simulation`: týdně
- `Deploy Prediction Page`: při změně `outputs/prediction_report.html` na `main`

`Full Prediction Pipeline` je one-shot end-to-end běh.

Veřejná stránka predikce:
- **https://pokys.github.io/APEX-F1/**

Pokud by deploy padal s Pages chybou, jednorázově nastav:
- `Settings -> Pages -> Source: GitHub Actions`

## 2. Human-in-the-loop signály

Signály z článků jsou záměrně pod lidskou kontrolou.

Postup:
1. Otevři `knowledge/inbox/articles.md`.
2. Vyber relevantní F1 články.
3. V externím AI nástroji udělej strukturovanou extrakci.
4. Ulož JSON do `knowledge/processed/signals_YYYY-MM-DD.json`.
5. Commit + push.

Po pushi:
- automaticky běží validace signálů,
- následně může běžet plná pipeline.

Životní cyklus:
- aktivní signály jsou v `knowledge/processed/`,
- po odjetí závodů se staré soubory přesouvají do `knowledge/processed/archive/`.

## 3. Doporučený race-week postup

1. Aktualizuj hard data:

```bash
python pipeline/ingest_fastf1.py --season 2026 --sessions Q,R --cutoff-date YYYY-MM-DD --log-level INFO
```

2. Vyber další GP:

```bash
python pipeline/select_next_gp.py --season 2026 --as-of-date YYYY-MM-DD --race-config config/race_config.json --log-level INFO
```

3. Zvaliduj a aplikuj signály:

```bash
python pipeline/validate_signals.py --signals-dir knowledge/processed --log-level INFO
python pipeline/build_features.py --season 2026 --guardrails-config config/signal_guardrails.json --allow-missing-fastf1 --log-level INFO
python pipeline/update_ratings.py --season 2026 --guardrails-config config/signal_guardrails.json --allow-missing-features --log-level INFO
```

4. Aplikuj backtest kalibraci:

```bash
python pipeline/apply_backtest_calibration.py --season 2026 --allow-missing-report --race-config config/race_config.json --log-level INFO
```

5. Simulace + publikace + validace:

```bash
python pipeline/simulate_weather_scenarios.py --allow-missing-models --log-level INFO
python pipeline/publish_prediction.py --input outputs/prediction_dry.json --output outputs/prediction_dry.json --allow-missing-input --log-level INFO
python pipeline/publish_prediction.py --input outputs/prediction_wet.json --output outputs/prediction_wet.json --allow-missing-input --log-level INFO
python pipeline/publish_prediction.py --allow-missing-input --log-level INFO
python pipeline/render_prediction_page.py --prediction outputs/prediction.json --prediction-dry outputs/prediction_dry.json --prediction-wet outputs/prediction_wet.json --race-config config/race_config.json --output outputs/prediction_report.html --allow-missing-input --log-level INFO
python pipeline/validate_outputs.py --log-level INFO
```

Poznámka:
- Web report teď má přepínač scénářů `Dry/Wet`.
- `prediction.json` zůstává kvůli kompatibilitě (alias dry scénáře).

## 4. Chování na startu nové sezony

Na začátku sezony bývají data nekompletní.

Aktuální fallback logika:
- `build_features.py`: když sezona nemá odjeté session, vezme poslední kompletní sezonu.
- `update_ratings.py`: když nejsou použitelné features pro aktuální sezonu, vezme poslední s daty.

To je očekávané chování, dokud se nenajedou aktuální závody.

## 5. Počasí

Aktuálně:
- simulace používá `weather` + `weather_modifier` z konfigurace,
- live weather API zatím není zapojené.

Ruční ladění:
- `config/track_profiles.json` (defaulty podle tratě/země),
- nebo přímo `config/race_config.json` před během.

## 6. Články, důvěryhodnost a guardrails

Aktuálně:
- RSS sběr je automatický,
- semantická extrakce článků je ruční/external AI,
- dopad signálů je řízen v `config/signal_guardrails.json`:
  - mapa důvěryhodnosti zdrojů,
  - minimální confidence,
  - echo-decay (opakované stejné tvrzení),
  - capy dopadu soft signálů.

Výsledek:
- soft data mají omezený, kontrolovaný vliv,
- bulvár/hype neprojde naplno do modelu.

## 7. Troubleshooting

### A) `git push` HTTP 500 ve workflow

Význam:
- většinou dočasný problém GitHubu.

Co dělat:
- rerun workflow (workflowe mají retry logiku).

### B) `select_next_gp` selže při načtení kalendáře

Význam:
- dočasný výpadek FastF1 backendu/API.

Chování:
- skript zachová stávající `config/race_config.json`, pokud existuje.

Co dělat:
- spustit znovu později,
- nebo explicitně vyplnit `--season` a `--as-of-date`.

### C) Chyby ve zpracovaných signálech

Symptomy:
- padá `validate_signals.py`.

Co dělat:
- opravit JSON podle schématu,
- znovu pustit validaci.

### D) Testy v GitHubu hlásí import path

Mitigace už je zapojená:
- `pipeline/__init__.py`
- `tests/conftest.py`
- `PYTHONPATH` v `tests.yml`

## 8. Co je pořád na člověku

- výběr relevantních článků,
- síla signálů (`source_confidence`, concern/change),
- případné ruční doladění před víkendem (trať/počasí).

## 9. Před-run checklist

Před větším během ověř:
- `knowledge/feeds.yaml` je validní,
- `knowledge/processed/*.json` projde validací,
- `config/race_config.json` míří na správný závod,
- backtest report je dostupný (doporučeno),
- výsledný `outputs/prediction.json` projde validací,
- veřejná stránka je dostupná: **https://pokys.github.io/APEX-F1/**
