from __future__ import annotations

import json
from pathlib import Path

from pipeline.publish_prediction import normalize_prediction, redistribute_to_target
from pipeline.validate_outputs import validate_prediction


def _qualifying_payload() -> dict:
    """A 20-car grid whose raw spot counts sum exactly to the validator targets
    (pole=1, front_row=2, top10=10). Pole is smoothed + temperature-scaled, so the
    16 tail drivers carry a 0.005 floor that sits above their raw front-row / top-10
    counts of zero, mimicking the production leak that broke the pipeline."""
    floor = 0.005
    pole = [0.45, 0.27, 0.12, 0.08] + [floor] * 16  # sums to 1.0
    front_raw = [0.88, 0.66, 0.31, 0.15] + [0.0] * 16  # sums to 2.0
    top10_raw = [1.0] * 10 + [0.0] * 10  # sums to 10.0
    drivers = []
    for i in range(20):
        drivers.append(
            {
                "name": f"D{i:02d}",
                "team": f"T{i // 2}",
                "pole_probability": pole[i],
                "front_row_probability": front_raw[i],
                "top10_probability": top10_raw[i],
                "expected_position": float(i + 1),
            }
        )
    return {
        "race": "Test GP",
        "generated_at": "2026-01-01T00:00:00Z",
        "target_output_type": "qualifying",
        "prediction_target": "qualifying",
        "drivers": drivers,
    }


def test_publish_restores_exact_spot_sums_for_qualifying(tmp_path: Path) -> None:
    out = normalize_prediction(_qualifying_payload())
    rows = out["drivers"]

    top10_target = float(min(10, len(rows)))
    assert abs(sum(r["pole_probability"] for r in rows) - 1.0) < 1e-6
    assert abs(sum(r["front_row_probability"] for r in rows) - 2.0) < 1e-3
    assert abs(sum(r["top10_probability"] for r in rows) - top10_target) < 1e-3

    # Monotonicity must survive the redistribution.
    for r in rows:
        assert r["pole_probability"] <= r["front_row_probability"] + 1e-9
        assert r["front_row_probability"] <= r["top10_probability"] + 1e-9
        assert r["top10_probability"] <= 1.0 + 1e-9

    # The smoothed pole floor still acts as a lower bound for a tail driver.
    tail = next(r for r in rows if r["name"] == "D19")
    assert tail["front_row_probability"] >= tail["pole_probability"] - 1e-9

    # And the published payload passes the output validator end to end.
    path = tmp_path / "prediction.json"
    path.write_text(json.dumps(out), encoding="utf-8")
    validate_prediction(path)


def _race_payload() -> dict:
    """A 10-car grid whose raw podium counts sum exactly to 3. The win metric is
    smoothed, so tail drivers carry a 0.005 floor above their raw podium count of zero."""
    floor = 0.005
    win = [0.5, 0.3, 0.165] + [floor] * 7  # sums to 1.0
    podium_raw = [0.95, 0.8, 0.6, 0.4, 0.25] + [0.0] * 5  # sums to 3.0
    drivers = []
    for i in range(10):
        drivers.append(
            {
                "name": f"D{i:02d}",
                "team": f"T{i // 2}",
                "win_probability": win[i],
                "podium_probability": podium_raw[i],
                "expected_finish": float(i + 1),
            }
        )
    return {
        "race": "Test GP",
        "generated_at": "2026-01-01T00:00:00Z",
        "target_output_type": "race",
        "prediction_target": "race",
        "drivers": drivers,
    }


def test_publish_restores_podium_sum_for_race(tmp_path: Path) -> None:
    out = normalize_prediction(_race_payload())
    rows = out["drivers"]
    assert abs(sum(r["win_probability"] for r in rows) - 1.0) < 1e-6
    assert abs(sum(r["podium_probability"] for r in rows) - 3.0) < 1e-3
    for r in rows:
        assert r["win_probability"] <= r["podium_probability"] + 1e-9
    path = tmp_path / "prediction.json"
    path.write_text(json.dumps(out), encoding="utf-8")
    validate_prediction(path)


def test_redistribute_to_target_is_noop_when_already_on_target() -> None:
    entries = [
        {"v": 0.8, "lo": 0.5},
        {"v": 0.7, "lo": 0.3},
        {"v": 0.5, "lo": 0.2},
    ]
    redistribute_to_target(entries, "v", "lo", sum(e["v"] for e in entries))
    assert [e["v"] for e in entries] == [0.8, 0.7, 0.5]


def test_redistribute_to_target_preserves_lower_bound() -> None:
    entries = [
        {"v": 0.90, "lo": 0.40},
        {"v": 0.70, "lo": 0.30},
        {"v": 0.20, "lo": 0.20},  # at its lower bound; must not drop below it
    ]
    redistribute_to_target(entries, "v", "lo", 1.5)
    assert abs(sum(e["v"] for e in entries) - 1.5) < 1e-9
    for e in entries:
        assert e["v"] >= e["lo"] - 1e-9
