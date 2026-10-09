from __future__ import annotations

import math

from pipeline import render_accuracy_page as acc


def backtest() -> dict:
    def race(round_number, actual, predicted, p_actual, baseline_p, baseline_fav):
        return {
            "round": round_number,
            "race": f"GP {round_number}",
            "actual_winner": actual,
            "predicted_winner": predicted,
            "winner_hit": actual == predicted,
            "win_probabilities": {actual: p_actual, "ZZZ": 0.1},
            "baseline_log_loss": {"championship_order": -math.log(baseline_p)},
            "baseline_favourite": baseline_fav,
            "baseline_hit": baseline_fav == actual,
        }

    return {
        "season": 2026,
        "races": [race(3, "VER", "VER", 0.4, 0.2, "NOR"), race(2, "NOR", "VER", 0.1, 0.2, "NOR")],
        "qualifying": [],
    }


def test_rows_use_probability_of_actual_result_and_baseline() -> None:
    rows = acc.kind_rows(backtest(), "win")
    assert [r["round"] for r in rows] == [2, 3]
    assert rows[1]["model_p"] == 0.4 and abs(rows[1]["baseline_p"] - 0.2) < 1e-9
    assert rows[0]["baseline_hit"] is True


def test_typical_probability_and_verdict() -> None:
    assert abs(acc.typical_probability([0.4, 0.1]) - 0.2) < 1e-9
    assert acc.verdict(0.2, 0.2) == "on par with the championship-order guess"
    assert acc.verdict(0.3, 0.2) == "50% better than the championship-order guess"
    assert acc.verdict(0.1, 0.2) == "50% worse than the championship-order guess"
    assert acc.verdict(0.2, None) == ""


def test_page_compares_model_with_championship_order() -> None:
    page = acc.render(backtest(), None, 2026)
    assert "1/2<small> favourite correct" in page
    assert "Championship leader as favourite: 1/2" in page
    assert "on par with the championship-order guess" in page
    assert page.count("<svg") == 2  # wide + narrow chart for the race winner; no qualifying rows
    assert "No session has been scored yet" in page


def test_page_without_baseline_fields_or_backtest() -> None:
    data = backtest()
    for row in data["races"]:
        del row["baseline_favourite"], row["baseline_hit"]
    assert "Championship leader as favourite" not in acc.render(data, None, 2026)
    assert "No evaluated event yet" in acc.render(None, None, 2026)


def test_main_never_fails_the_pipeline(tmp_path, monkeypatch) -> None:
    import sys

    (tmp_path / "race_config.json").write_text('{"season": 2026}')
    (tmp_path / "backtest_season_2026.json").write_text("not json")
    monkeypatch.setattr(sys, "argv", ["x", "--race-config", str(tmp_path / "race_config.json"), "--backtest-dir", str(tmp_path),
                                      "--output", str(tmp_path / "accuracy.html")])
    assert acc.main() == 0


def test_sprints_get_their_own_card_and_chart() -> None:
    data = backtest()
    data["sprints"] = [dict(data["races"][0], race="Sprint GP")]
    page = acc.render(data, None, 2026)
    assert "Sprint winner" in page and page.count("<svg") == 4
    assert "Sprint winner" not in acc.render(backtest(), None, 2026)
