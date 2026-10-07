# APEX-F1 – plán oprav a vylepšení (z code review)

Ke každé opravě patří test, který bez opravy selže. Fáze se dělají v tomto pořadí.

## FÁZE 1 – Data (nejvyšší priorita, kvůli sprintovému víkendu v Singapuru)
1. `ingest_fastf1`: pro FP1/FP2/FP3/SQ načítat timing/lap data tak, aby výsledky měly pozice a nejlepší časy. Pokud FastF1 pozice nedopočítá, dopočítat je z nejrychlejšího platného kola. SQ musí mít pozice a SQ1/SQ2/SQ3 časy.
2. Do workflow, která volají ingest, přidat `actions/cache` pro `data/raw/fastf1_cache` a nestahovat znovu session, které už jsou ve snapshotu kompletní.
3. `available_sessions_for_event` a `build_inputs_manifest`: session je „dostupná“ jen tehdy, když má aspoň jeden řádek s pozicí nebo časem. Pouhý seznam jezdců nestačí.
4. `build_features`: opravit únik proměnné `team_key` ve výpočtu `teammate_qualifying_gap_ms` (řádek ~772). Gap musí být vůči vlastnímu týmu a vždy >= 0.
5. Kvalifikační gap počítat po segmentech (Q1 vs Q1 apod.), ne q3/q2/q1 smíchaně.

## FÁZE 2 – Kalendář a cíl predikce
6. Ukládat skutečné UTC časy session (FastF1 `Session*DateUtc`) do kalendářové cache i do `sessions_schedule`. Tabulky `COUNTRY_UTC_OFFSET_HOURS` a `SESSION_END_LOCAL_HOUR` ponechat jen jako fallback.
7. Normalizovat názvy zemí (USA/UK/UAE/Malaysia ↔ klíče profilů) jednou sdílenou funkcí a doplnit chybějící profily (Malajsie, Las Vegas a Miami podle event name, dvě španělské tratě podle event name).
8. `select_next_gp`: při změně GP resetovat parametry trati na defaulty před aplikací profilu, aby nic neprosakovalo z předchozí GP. Pokud profil chybí, zalogovat warning.
9. Testy: noční sprintový víkend (Singapur) a americký víkend. Cíl se nesmí přepnout před skutečným koncem session.

## FÁZE 3 – Ratingy
10. Opravit nekonzistentní znaménka `sector_dominance` a `pit_stop_perf`. Přejmenovat komponenty podle toho, co skutečně měří.
11. Odstranit nebo nahradit konstantní placeholder komponenty (`upgrades_impact`, `weekend_pace_proxy`, `sprint_pace_proxy`, `safety_car_reactions`, `power_unit_reliability` a další bez reálného vstupu) a přenormovat váhy. Všechny komponenty clampovat na 0–100.
12. `sprint_execution` počítat jen ze skutečných SQ vs S dat, jinak neutrální hodnota.
13. Baseline pro nového jezdce: rating týmu + pod-průměrný driver offset (např. 25. percentil pole), ne fixních 68/66.
14. Spolehlivost: pravděpodobnost DNF odvodit přímo z `dnf_rate` se shrinkage k průměru pole.

## FÁZE 4 – Simulace
15. Sprint simulovat zvlášť: kratší vzdálenost, zhruba 1/3 rizika DNF, bez pit-stop strategie, nižší vliv degradace.
16. Mokrý scénář: zavést wet rating (z historických mokrých session a signálů), aby se mokro reálně projevilo. Suchý scénář nesmí přepisovat `weather_modifier` z profilu trati na 0 bez důvodu.
17. Konzistentní kalibrace: stejné škálování pro všechny pravděpodobnostní výstupy (pole/front row/top10, win/podium), ne jen pro headline metriku.

## FÁZE 5 – Backtest a kalibrace
18. Backtest musí používat stejný kód feature/rating/simulace jako produkce a běžet i na sezóně 2026 (walk-forward). Workflow backtestu má defaultně brát aktuální sezónu.
19. Kalibrovat `qualifying_noise` a `race_noise` (případně škálu ratingů) místo, nebo vedle, post-hoc teploty. Když optimum padne na hranici rozsahu hledání, rozsah rozšířit.
20. Přidat baseline modely (uniformní, poleman/vítěz minulé GP, pořadí v šampionátu). Quality gates přepsat na „model je lepší než nejlepší baseline“ v log-loss.
21. Ukládat archiv predikcí před každou session a po ní je vyhodnotit (track record), zobrazit na dashboardu.

## FÁZE 6 – Penalizace a pohonné jednotky (nová funkce)
22. Historie: z R výsledků (`grid_position` vs pozice v Q, start z boxů) odvodit uplatněné grid penalizace. Používat je jako feature (riziko penalizace týmu/PU) a v backtestu brát skutečný startovní grid.
23. Opravit `fixed_grid` pro závod: aplikovat známé penalizace (posun o N míst, konec gridu, pit lane) místo čistého pořadí z kvalifikace. Před kvalifikací aplikovat oznámené penalizace i na simulovaný grid.
24. Nový typ signálu v `knowledge/processed` (`grid_penalty`, `pu_element_change`, `race_ban`/`driver_substitution`) se schématem, validací ve `validate_signals` a dokumentací v `AI_EXTRACTION_GUIDE.md`. Pole: season, event, driver, typ, places / back_of_grid / pit_lane, source_url, timestamp.
25. Prozkoumat automatické zdroje: FastF1 `race_control_messages` (`messages=True`), OpenF1 race_control endpoint, dokumenty FIA. Implementovat best-effort automatický import, který generuje signály z bodu 24 (výstup musí projít stejnou validací), a sepsat zjištění o spolehlivosti do README.
26. Dashboard: zobrazit uplatněné penalizace a jejich zdroj.

## FÁZE 7 – Provoz a soft signály
27. Full pipeline nesmí mazat `knowledge/inbox/articles.md`. Inbox se má archivovat nebo rotovat, ne přepisovat, a stav zaškrtávacích políček zachovat.
28. Pokud nejsou signály, zobrazit to na dashboardu a nepočítat s jejich vahou (ověřit a otestovat).
