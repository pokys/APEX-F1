from __future__ import annotations


from pipeline import import_fia_documents as fia
from pipeline.prediction_targeting import grid_penalties_from_signals

# Excerpts of real stewards' documents (2026 Azerbaijan / Italian / Dutch GP).
PU_DOC = """2026 AZERBAIJAN GRAND PRIX
From The Stewards
Document 20
Date 24 September 2026
Time 15:59
No / Driver 14 - Fernando Alonso
Competitor Aston Martin Aramco F1 Team
Time 12:34
Session Free Practice 1
Fact The following Power Unit elements have been used:
5th Engine (ICE)
Infringement Breaches of Articles B8.2.2 (read with B8.2.3) of the FIA F1 Regulations.
Decision Drop of 25 grid positions for the next Race in which the driver participates
Reason The penalty is imposed in accordance with Article B8.2.8 of the FIA F1 Regulations.
"""
PIT_LANE_SPRINT_DOC = """Date 30 August 2026
Time 17:10
No / Driver 27 - Nico Hülkenberg
Session Sprint Qualifying
Decision Required to start the Sprint from the pit lane.
Reason Parc ferme changes.
"""
SPRINT_OR_RACE_DOC = """Date 25 September 2026
Time 18:00
No / Driver 55 - Carlos Sainz
Session Qualifying
Decision Drop of 5 grid positions for the next Sprint/Race in which the driver
participates. 2 penalty points (total of 4 for the 12 month period).
Reason Yellow flags.
"""
CONVERTED_DOC = """Date 26 September 2026
Time 16:40
No / Driver 43 - Franco Colapinto
Session Race
Decision 10 second time penalty converted to a drop of 5 grid positions at the next race in
which the driver participates.
Reason Collision with Car 10.
"""
REPRIMAND_DOC = """Date 25 September 2026
No / Driver 41 - Arvid Lindblad
Session Free Practice 3
Decision Driver: Reprimand (Driving). This is the driver's 1st reprimand of the season.
Reason Leaving the track.
"""


def test_parse_decision_reads_pu_grid_drop() -> None:
    parsed = fia.parse_decision(PU_DOC)
    assert parsed is not None
    assert parsed["driver_number"] == 14
    assert parsed["places"] == 25
    assert parsed["session"] == "FP1"
    assert parsed["target"] == "race"
    assert parsed["timestamp"] == "2026-09-24T15:59:00"


def test_parse_decision_pit_lane_and_targets() -> None:
    pit = fia.parse_decision(PIT_LANE_SPRINT_DOC)
    assert pit["pit_lane"] is True and "places" not in pit
    assert pit["session"] == "SQ" and pit["target"] == "sprint"
    multi_line = fia.parse_decision(SPRINT_OR_RACE_DOC)
    assert multi_line["places"] == 5 and multi_line["target"] == "sprint_or_race"
    assert fia.parse_decision(CONVERTED_DOC)["places"] == 5


def test_parse_decision_ignores_non_grid_sanctions() -> None:
    assert fia.parse_decision(REPRIMAND_DOC) is None
    assert fia.parse_decision("Session Race\nDecision No further action.\nReason x\n") is None


def test_applies_to_current_and_previous_event() -> None:
    pu = fia.parse_decision(PU_DOC)
    assert fia.applies_to(pu, sprint_weekend=False, previous_event=False) == "race"
    # A PU penalty from the previous GP's practice was served there.
    assert fia.applies_to(pu, sprint_weekend=False, previous_event=True) is None
    sprint_or_race = fia.parse_decision(SPRINT_OR_RACE_DOC)
    # Qualifying comes after the sprint on a sprint weekend -> race.
    assert fia.applies_to(sprint_or_race, sprint_weekend=True, previous_event=False) == "race"
    sprint_or_race["session"] = "SQ"
    assert fia.applies_to(sprint_or_race, sprint_weekend=True, previous_event=False) == "sprint"
    converted = fia.parse_decision(CONVERTED_DOC)
    assert fia.applies_to(converted, sprint_weekend=False, previous_event=True) == "race"
    # A race decision of the current GP cannot affect its own grid.
    assert fia.applies_to(converted, sprint_weekend=False, previous_event=False) is None
    pit = fia.parse_decision(PIT_LANE_SPRINT_DOC)
    assert fia.applies_to(pit, sprint_weekend=False, previous_event=False) is None


def test_resolve_driver_by_name_without_accents_then_number() -> None:
    by_number = {27: "HUL", 14: "ALO"}
    by_name = {fia.name_key("Nico Hulkenberg"): "HUL"}
    assert fia.resolve_driver(fia.parse_decision(PIT_LANE_SPRINT_DOC), {}, by_name) == "HUL"
    assert fia.resolve_driver(fia.parse_decision(PU_DOC), by_number, {}) == "ALO"
    assert fia.resolve_driver(fia.parse_decision(PU_DOC), {}, {}) is None


def fake_site(documents: dict[str, str]):
    championship = '<option value="/documents/x/season/season-2026-2072">SEASON 2026</option>'
    season = (
        '<option value="/documents/x/season/season-2026-2072/event/Italian%20Grand%20Prix">Italian Grand Prix</option>'
        '<option value="/documents/x/season/season-2026-2072/event/Dutch%20Grand%20Prix">Dutch Grand Prix</option>'
    )

    def event_page(prefix: str) -> str:
        return "".join(
            f'<a href="/system/files/decision-document/{name}.pdf">x</a>' for name in documents if name.startswith(prefix)
        ) + '<a href="/system/files/decision-document/2026_italian_grand_prix_-_summons_-_car_1_-_x.pdf">s</a>'

    pages = {
        fia.FIA_BASE + fia.CHAMPIONSHIP_PATH: championship,
        fia.FIA_BASE + "/documents/x/season/season-2026-2072": season,
        fia.FIA_BASE + "/documents/x/season/season-2026-2072/event/Italian%20Grand%20Prix": event_page("2026_italian"),
        fia.FIA_BASE + "/documents/x/season/season-2026-2072/event/Dutch%20Grand%20Prix": event_page("2026_dutch"),
    }
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        if url in pages:
            return pages[url].encode()
        name = url.rsplit("/", 1)[-1][:-4]
        return documents[name].encode()

    return fetch, fetched


def test_collect_reads_new_documents_only(monkeypatch) -> None:
    monkeypatch.setattr(fia, "pdf_text", lambda data: data.decode())
    documents = {
        "2026_italian_grand_prix_-_infringement_-_car_14_-_pu_elements": PU_DOC,
        "2026_italian_grand_prix_-_infringement_-_car_41_-_leaving_track": REPRIMAND_DOC,
        "2026_dutch_grand_prix_-_infringement_-_car_43_-_collision": CONVERTED_DOC,
        "2026_dutch_grand_prix_-_infringement_-_car_14_-_pu_elements": PU_DOC,
    }
    fetch, fetched = fake_site(documents)
    by_number = {14: "ALO", 43: "COL", 41: "LIN"}
    signals, done = fia.collect(fetch, 2026, "Italian Grand Prix", False, "Dutch Grand Prix", set(), by_number, {})
    assert sorted((s["driver"], s["places"], s["applies_to"]) for s in signals) == [("ALO", 25, "race"), ("COL", 5, "race")]
    assert all(s["event"] == "Italian Grand Prix" and s["source_name"] == fia.SOURCE_NAME for s in signals)
    assert len(done) == 4
    assert not any("summons" in url for url in fetched)

    fetch2, fetched2 = fake_site(documents)
    again, done2 = fia.collect(fetch2, 2026, "Italian Grand Prix", False, "Dutch Grand Prix", done, by_number, {})
    assert again == [] and done2 == set()
    assert not any(url.endswith(".pdf") for url in fetched2)


def test_fia_signals_pass_validation() -> None:
    from pipeline.validate_signals import validate_signal

    signal = fia.penalty_signal(fia.parse_decision(PU_DOC), "ALO", 2026, "Italian Grand Prix", "race", "https://www.fia.com/x.pdf")
    assert validate_signal(signal, "penalties_2026_fia.json", 0) == []


def test_race_control_copy_is_not_counted_twice() -> None:
    race_control = {"type": "grid_penalty", "driver": "PIA", "places": 3, "applies_to": "race", "source_name": "openf1_race_control"}
    fia_doc = {"type": "grid_penalty", "driver": "PIA", "places": 3, "applies_to": "race", "source_name": fia.SOURCE_NAME}
    other = {"type": "grid_penalty", "driver": "NOR", "places": 5, "applies_to": "race", "source_name": "openf1_race_control"}
    manual = {"type": "grid_penalty", "driver": "PIA", "places": 10, "applies_to": "race", "source_name": "manual"}
    penalties = {p["driver"]: p["places"] for p in grid_penalties_from_signals([race_control, fia_doc, other, manual])}
    assert penalties == {"PIA": 13, "NOR": 5}


def test_fia_site_is_checked_only_during_race_weekend_and_rarely() -> None:
    from datetime import datetime, timezone

    schedule = {"FP1": "2026-10-09T09:30:00+00:00", "Q": "2026-10-10T13:00:00+00:00", "R": "2026-10-11T12:00:00+00:00"}

    def at(text: str) -> datetime:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)

    assert fia.should_check(schedule, None, at("2026-10-06T12:00:00"))[0] is False  # days before
    assert fia.should_check(schedule, None, at("2026-10-08T12:00:00"))[0] is True  # day before FP1
    assert fia.should_check(schedule, "2026-10-10T11:00:00+00:00", at("2026-10-10T12:00:00"))[0] is False
    assert fia.should_check(schedule, "2026-10-10T09:59:00+00:00", at("2026-10-10T12:00:00"))[0] is True
    assert fia.should_check(schedule, None, at("2026-10-11T13:00:00"))[0] is False  # after race start
    assert fia.should_check({}, None, at("2026-10-10T12:00:00"))[0] is False
