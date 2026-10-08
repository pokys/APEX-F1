# Code review APEX-F1: shrnutí implementace

Implementace všech 28 bodů z code review. Práce šla přímo do `main` po fázích; každá fáze byla pushnutá samostatně, až když prošly testy a lokální běh pipeline (`build_features` → `validate_outputs`). Testy (`python -m pytest -q`) mají 148 položek a všechny procházejí. Kroky z README (`build_features` → `validate_outputs`) prošly i přímo v repozitáři nad commitnutými daty (vygenerované soubory se pak vrátily do commitnutého stavu). Testy uvedené níže na původním kódu selžou.

## Commity

| Fáze | Commit | Obsah |
|---|---|---|
| 1 | `61a2048` | ingest FP/SQ s pozicemi, znovupoužití session, cache, gapy v kvalifikaci |
| 1 (oprava) | `e9c8d8b` | pád ingestu, když FastF1 nemá timing data |
| 1 (doplněk) | `f18c9a6` | OpenF1 jako záložní zdroj pro FP a SQ |
| 1 (doplněk) | `8f65c42` | názvy týmů rezervních jezdců z OpenF1 |
| 2 | `6d32346` | přesné časy session, normalizace zemí, reset parametrů trati |
| 3 | `dab8251` | ratingy jen z reálných vstupů, zisk pozic, pravděpodobnost DNF |
| 4 | `aeda6c5` | sprint, mokrý scénář s `wet_rating`, konzistentní kalibrace |
| 5 | `14d64a0` | kalibrace šumu, baseline modely, sdílený kód, track record |
| 6 | `5e686fb` | penalizace, výměny PU, tresty a náhradníci |
| 7 | `afea076` | inbox se nemaže, informace o chybějících signálech |
| docs | commit s tímto souborem | README, RUNBOOK, tento přehled |

## Body 1–28

| # | Bod | Commit | Testy |
|---|---|---|---|
| 1 | FP1–FP3 a SQ s pozicemi a časy | `61a2048`, `e9c8d8b`, `f18c9a6`, `8f65c42` | `test_ingest_fastf1.py::test_load_session_classifies_practice_from_laps`, `::test_load_session_skips_entry_list_without_classification`, `::test_load_session_survives_missing_timing_data`, `test_openf1_client.py::test_sprint_qualifying_results_keep_segment_times`, `::test_practice_without_results_is_ranked_by_fastest_lap`, `::test_align_team_names_maps_reserve_drivers_via_teammates` |
| 2 | Cache FastF1 a znovupoužití hotových session | `61a2048` | `test_ingest_fastf1.py::test_reusable_session_requires_final_classified_data`, `::test_ingest_workflows_restore_fastf1_cache_before_ingest` |
| 3 | Session je dostupná jen s pozicí nebo časem | `61a2048` | `test_prediction_targeting.py::test_entry_list_without_positions_is_not_an_available_session`, `test_select_next_gp.py::test_get_available_sessions_ignores_entry_lists` |
| 4 | Gap na týmového kolegu z vlastního týmu, ≥ 0 | `61a2048` | `test_build_features.py::test_teammate_gap_is_measured_against_own_team` |
| 5 | Kvalifikační gap po segmentech | `61a2048` | `test_build_features.py::test_qualifying_gap_compares_same_segments_only`, `::test_build_features_collects_timing_gap_metrics` |
| 6 | Přesné UTC časy session | `6d32346` | `test_select_next_gp.py::test_live_calendar_prefers_fastf1_backend_with_exact_times`, `::test_merge_calendars_keeps_exact_times_over_date_only_values`, `test_ingest_fastf1.py::test_fetch_schedule_retries_within_backend_before_falling_back` |
| 7 | Normalizace zemí, chybějící profily tratí | `6d32346` | `test_prediction_targeting.py::test_american_country_aliases_resolve_like_full_names`, `test_select_next_gp.py::test_every_2026_calendar_event_has_a_track_profile` |
| 8 | Reset parametrů trati při změně GP | `6d32346` | `test_select_next_gp.py::test_track_params_reset_between_events_and_country_alias` |
| 9 | Testy nočního sprintového víkendu a víkendu v USA | `6d32346` | `test_prediction_targeting.py::test_singapore_exact_times_follow_real_weekend`, `::test_singapore_date_only_fallback_never_switches_before_session_end`, `::test_american_country_aliases_resolve_like_full_names` |
| 10 | Znaménka `sector_dominance` a `pit_stop_perf` → `race_position_gain` | `dab8251` | `test_update_ratings.py::test_race_position_gain_rewards_gaining_places_against_field_trend` |
| 11 | Bez konstantních placeholderů, rozsah 0–100 | `dab8251` | `test_update_ratings.py::test_rating_components_have_real_inputs_and_stay_in_range` |
| 12 | `sprint_execution` jen ze SQ vs S, jinak 50 | `dab8251` | `test_update_ratings.py::test_sprint_execution_is_neutral_without_sprint_qualifying_data` |
| 13 | Výchozí rating nového jezdce | `dab8251` | `test_update_ratings.py::test_new_driver_starts_below_teammate_and_field_median` |
| 14 | Pravděpodobnost DNF z `dnf_rate` se shrinkage | `dab8251` | `test_update_ratings.py::test_dnf_probability_follows_observed_rate_with_shrinkage`, `::test_simulation_uses_dnf_probability` |
| 15 | Samostatná simulace sprintu | `aeda6c5` | `test_simulate_race.py::test_sprint_has_a_third_of_race_dnf_exposure`, `::test_sprint_ignores_pit_strategy` |
| 16 | `wet_rating`; suchý scénář drží `weather_modifier` | `aeda6c5` | `test_simulate_race.py::test_wet_rating_only_matters_in_the_wet`, `::test_dry_scenario_keeps_track_weather_modifier`, `test_build_features.py::test_wet_sessions_produce_wet_position_delta`, `test_update_ratings.py::test_wet_rating_from_history_and_signals`, `test_openf1_client.py::test_session_is_wet_from_tyre_stints`, `test_ingest_fastf1.py::test_annotate_wet_flag_stores_openf1_result_once` |
| 17 | Stejné škálování všech pravděpodobností | `aeda6c5`, `14d64a0` | `test_simulate_race.py::test_qualifying_probabilities_are_scaled_consistently`, `::test_standings_blend_mixes_whole_outcomes_consistently` |
| 18 | Backtest používá produkční kód a aktuální sezonu | `14d64a0` | `test_backtest_simulation.py::test_event_config_does_not_inherit_live_gp_parameters`; sdílené `update_ratings.build_rating_models` a `blend_with_previous_season`; výchozí sezona workflow `$(date -u +%Y)` |
| 19 | Kalibrace škály šumu, rozšiřování mřížky | `14d64a0` | `test_backtest_simulation.py::test_noise_scale_search_extends_past_grid_boundary`, `test_apply_backtest_calibration.py::test_apply_calibration_prefers_noise_scales`, `::test_choose_backtest_file_needs_enough_races_in_current_season` |
| 20 | Baseline modely; brány „lepší než baseline“ | `14d64a0` | `test_backtest_simulation.py::test_baselines_are_distributions_and_use_only_prior_points`, `::test_choose_blend_reports_leave_one_out`, `test_validate_backtest_quality.py::test_quality_gate_requires_beating_best_baseline`, `::test_committed_backtest_report_passes_quality_gate` |
| 21 | Archiv predikcí a track record na dashboardu | `14d64a0` | `test_track_record.py::test_archive_is_written_before_and_frozen_after_session_start`, `::test_archived_prediction_is_scored_once_results_exist` |
| 22 | Historie penalizací; skutečný grid v backtestu | `5e686fb` | `test_penalties.py::test_historical_grid_penalty_rate_from_grid_vs_qualifying`, `::test_backtest_uses_actual_starting_grid` |
| 23 | Penalizace na pevném i simulovaném gridu | `5e686fb` | `test_penalties.py::test_apply_grid_penalties_places_back_of_grid_and_pit_lane`, `::test_select_prediction_target_applies_penalties_to_qualifying_grid`, `::test_known_penalty_moves_driver_back_on_simulated_grid` |
| 24 | Typované signály: schéma, validace, dokumentace | `5e686fb` | `test_penalties.py::test_validate_event_signals`, `::test_grid_penalties_from_signals_respects_session_and_sums_places`, `::test_event_signals_do_not_count_as_soft_signals`, `::test_roster_changes_for_substitution_and_ban` |
| 25 | Automatický import z race control | `5e686fb` | `test_penalties.py::test_parse_race_control_messages`, `::test_collect_penalties_from_race_control_produces_valid_signals` |
| 26 | Penalizace a zdroj na dashboardu | `5e686fb` | `test_penalties.py::test_dashboard_lists_penalties_with_source` |
| 27 | Inbox se archivuje, nemaže | `afea076` | `test_collect_articles.py::test_rotate_inbox_archives_old_sections_with_checkbox_state`, `::test_full_pipeline_does_not_wipe_the_inbox` |
| 28 | Bez signálů: informace na webu, váha přerozdělena | `afea076` | `test_render_prediction_page.py::test_page_states_when_no_article_signals_are_used` |

## Co neplatí úplně nebo se ověřilo jen v produkci

- **Bod 1, ověření přes síť:** po povolení sítě proběhl ingest se živými daty do prázdného adresáře, tedy bez převzetí starého snapshotu. Všech 16 FP1, 11 FP2, 11 FP3 a 5 SQ sezony 2026 má pozice (v každé session aspoň 20 z 22 jezdců). SQ má i časy Q1–Q3. Tentokrát šlo vše přímo přes FastF1 live timing; v GitHub Actions tento zdroj pro 2026 data nevracel, proto existuje záložní OpenF1 (`f18c9a6`), ověřený v produkčním snapshotu. Živý běh odhalil chybu, kterou CI data neukázala: live timing a OpenF1 používají jiné názvy týmů než Ergast („Red Bull Racing“ vs „Red Bull“), takže by se týmy po změně zdroje rozdělily. Opravuje to commit „Canonical team names…“: kanonické názvy ve výsledcích i v lap metrikách, doplnění chybějícího týmu z rosteru a testy `test_ingest_fastf1.py::test_team_names_are_canonical_across_sources`, `::test_missing_team_is_filled_from_roster`, `::test_lap_metrics_use_canonical_team_names`. Pipeline nad čerstvým snapshotem pak prošla s 11 týmy a validate_outputs je OK.
- **Bod 2, `actions/cache`:** test kontroluje, že oba workflow s ingestem obnovují `data/raw/fastf1_cache` před spuštěním ingestu; na původních workflow selže. Samotné uložení a obnovení cache v Actions unit test nepokryje.
- **Bod 16, příznak `wet`:** plní ho ingest z OpenF1 (stinty INTERMEDIATE/WET nebo déšť). Po nasazení ho má 42 session sezony 2026, z toho 2 mokré. `wet_rating` proto zatím stojí na velmi malém vzorku a je silně stažený k neutrální hodnotě 50.
- **Bod 25, dokumenty FIA:** automatický import není. FIA zveřejňuje výměny prvků PU a jejich penalizace jen jako PDF na fia.com bez API a parser takových dokumentů by byl křehký. Tyto údaje se zadávají ručně jako signály `pu_element_change` (README, `AI_EXTRACTION_GUIDE.md` sekce 10). Import z OpenF1 race control je best effort: zachytí jen rozhodnutí, která race control zveřejní textem.
- **Migrace formátu:** `outputs/backtest/backtest_season_2025.json` a nový `backtest_season_2026.json` se v `14d64a0` přegenerovaly, protože se změnily kalibrační klíče (škály šumu místo teplot). Jiná vygenerovaná data se ručně necommitovala.

## Backtest před a po

Walk-forward backtest, 2000 simulací na event. „Před“ je kód před začátkem prací (`731a533`), „po“ je finální kód. Log-loss: nižší je lepší; náhodný tip ≈ 3,0.

| Metrika | 2025 před | 2025 po | 2026 před | 2026 po |
|---|---|---|---|---|
| Vyhodnocené závody / kvalifikace | 23 / 23 | 23 / 23 | 15 / 15 | 15 / 15 |
| Log-loss pole, nekalibrovaný | 3,357 | 1,932 | 2,278 | 2,008 |
| Log-loss pole, kalibrovaný (publikovaný) | 2,537 | **1,803** | 1,957 | **1,953** |
| Log-loss vítěze, nekalibrovaný | 1,527 | 1,612 | 1,687 | 1,716 |
| Log-loss vítěze, kalibrovaný (publikovaný) | **1,512** | 1,689 | **1,654** | 1,731 |
| Přesnost pole | 26 % | 17 % | 20 % | 27 % |
| Přesnost vítěze | 30 % | 22 % | 33 % | 40 % |
| Shoda pódia (z 3) | 1,91 | 1,83 | 1,67 | 1,47 |
| ECE pole (kalibrace jistoty) | 0,340 | **0,129** | 0,371 | **0,091** |
| ECE vítěze | 0,170 | **0,103** | 0,177 | **0,096** |
| Nejlepší baseline (pole / vítěz) | – | 1,854 / 1,810 | – | 1,971 / 1,771 |

Jak čísla číst:

- **Kvalifikace se výrazně zlepšila.** Nekalibrovaný model předtím dával skutečnému polemanovi téměř nulovou pravděpodobnost (log-loss 3,36 je horší než náhodný tip). Teď je kalibrovaný log-loss pod nejlepší baseline v obou sezonách. Velký podíl na tom mají reálná data z tréninků.
- **Kalibrace jistoty se zlepšila 2–4×** (ECE). Dříve model tvrdil třeba 99,7 % na top 10 u osmi jezdců najednou.
- **Log-loss vítěze je horší než dřív, a to i po započtení metodiky.** Část rozdílu je metodická: dřívější „kalibrovaný“ log-loss se ladil teplotou na stejných datech, na kterých se hodnotil, a s vyhlazenými pravděpodobnostmi. Nové číslo počítá s pevnou, předem zvolenou váhou směsi a se skutečným startovním gridem. Model ale i bez směsi zůstává za dřívějším číslem (2026: 1,710 vs 1,654). Fáze 3 totiž vyřadila „body na start“ ze strategického skóre jako dvojí započtení tempa; pokus vrátit je do závodního ratingu přinesl jen smíšený výsledek (2026 −0,007, 2025 +0,013), proto se nezavedl.
- **Proti baseline:** samotný model na 2026 nepřekonal pořadí v šampionátu (pole 2,15 vs 1,97). Proto je publikovaná predikce směs 50/50 se šampionátním modelem. Ta překonává nejlepší baseline v obou sezonách a obou cílech, což brány kvality nyní vynucují.
