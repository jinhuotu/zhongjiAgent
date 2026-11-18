import json
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from common.kiln_studio_acceptance import (
    energy_periods,
    interval_evaluation,
    overtemperature_evaluation,
)
from common.kiln_studio_comparison import compare_temperature_plans
from common.kiln_studio_engine import (
    algorithm_library,
    examples,
    predict_many,
    predict_model,
    run_experiment,
)
from common.kiln_studio_replay import build_replay_package
from common.kiln_studio_schema import (
    ExperimentRequest,
    PhysicsParameters,
    ReplayRequest,
    TemperatureForecastRequest,
)


def records(count=61, *, actual=500, prediction=500, step=1):
    start = datetime(2026, 1, 1)
    return [
        {
            "cycle": "c",
            "split": "test",
            "ts": (start + timedelta(minutes=index * step)).isoformat(),
            "origin": (start + timedelta(minutes=index * step - 10)).isoformat(),
            "actual": actual,
            "model": prediction,
        }
        for index in range(count)
    ]


def test_hourly_and_daily_integrate_actual_time_and_apply_distinct_limits():
    rows = records(1441, actual=60, prediction=66)
    score = energy_periods(rows, "model")
    assert score["hourly"]["completeCount"] == 24
    assert score["hourly"]["metrics"]["mape"] == pytest.approx(10)
    assert score["hourly"]["meetsTarget"] is True
    assert score["daily"]["completeCount"] == 1
    assert score["daily"]["metrics"]["mape"] == pytest.approx(10)
    assert score["daily"]["meetsTarget"] is False
    assert score["daily"]["periods"][0]["actual"] == pytest.approx(1440)
    assert score["daily"]["periods"][0]["start"].startswith("2026-01-01T00:00:00")


def test_energy_exposes_gaps_overlaps_and_zero_denominators():
    rows = records(actual=60, prediction=60)
    gap = energy_periods(rows[:20] + rows[25:], "model")["hourly"]
    assert gap["completeCount"] == 0
    assert gap["meetsTarget"] is None
    assert gap["periods"][0]["coveredMinutes"] == 54
    overlapping = rows + [{**row, "cycle": "duplicate"} for row in rows]
    invalid = energy_periods(overlapping, "model")["hourly"]
    assert invalid["completeCount"] == 0
    assert invalid["periods"][0]["reason"] == "同时段记录重叠"
    zero = energy_periods(records(actual=0, prediction=5), "model")["hourly"]
    assert zero["completeCount"] == 1
    assert zero["metrics"]["mape"] is None and zero["meetsTarget"] is None
    sparse = energy_periods(rows[::10], "model", {"c": 1.5})["hourly"]
    assert sparse["completeCount"] == 0


def test_alarm_matches_event_start_and_uses_prediction_issue_time():
    rows = records()
    for index in range(20, 31):
        rows[index]["actual"] = rows[index]["model"] = 1200
    for index in range(50, 56):
        rows[index]["model"] = 1200
    score = overtemperature_evaluation(rows, "model", 1100)
    assert score["actualCount"] == score["timelyCount"] == 1
    assert score["events"][0]["leadMinutes"] == 10
    assert score["hitRate"] == 1
    assert score["falseAlertCount"] == 1
    assert score["falseAlertRate"] == 0.5
    for row in rows[20:31]:
        row["origin"] = (datetime.fromisoformat(row["ts"]) - timedelta(minutes=2)).isoformat()
    late = overtemperature_evaluation(rows, "model", 1100)
    assert late["timelyCount"] == 0 and late["lateCount"] == 1
    assert late["hitRate"] == 0


def test_no_events_and_cutoff_events_never_claim_a_perfect_hit_rate():
    assert overtemperature_evaluation(records(), "model", None) is None
    none = overtemperature_evaluation(records(), "model", 1100)
    assert none["hitRate"] is none["falseAlertRate"] is None
    censored = records(actual=1200, prediction=1200)
    score = overtemperature_evaluation(censored, "model", 1100)
    assert score["actualCount"] == 0 and score["pendingActualCount"] == 1
    assert score["pendingAlertCount"] == 1
    assert score["falseAlertRate"] is None


def test_prediction_interval_scores_held_out_observations():
    rows = records(10, actual=100, prediction=100)
    rows[3]["actual"] = 130
    interval = interval_evaluation(
        rows,
        "model",
        {
            "radius": 15,
            "level": 0.9,
            "method": "split-conformal",
            "calibrationCount": 20,
        },
    )
    assert interval["coverage"] == 0.9
    assert interval["meetsTarget"] is True


@pytest.mark.parametrize("horizon", [10, 30])
def test_real_lstm_artifact_trains_reloads_and_replays_each_minute(horizon):
    start = datetime(2026, 1, 1)
    cycle = {
        "id": "c",
        "name": "cycle.csv",
        "points": [
            {
                "time": index,
                "ts": (start + timedelta(minutes=index)).isoformat(),
                "cycle": "c",
                "temp": 500 + index * 0.1,
                "temp_sp": 600,
                "gas": 60,
                "air": 600,
                "pressure": 3,
                "o2": 2,
            }
            for index in range(601)
        ],
    }
    config = ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="temperature",
        horizonMinutes=horizon,
        selectedAlgorithmKeys=None,
        calibrate=False,
    )
    definitions = [item for item in algorithm_library() if item.key in {"baseline", "lstm"}]
    result = run_experiment(
        [cycle], config, PhysicsParameters().model_dump(), ["thermal"], definitions
    )
    cards = {card["key"]: card for card in result["algorithms"]}
    assert cards["lstm"]["test"]["mae"] < cards["baseline"]["test"]["mae"]
    assert cards["lstm"]["trainingSummary"]["epochs"] == 24
    assert cards["lstm"]["predictionOffsets"] == list(range(1, horizon + 1))
    artifact = json.loads(json.dumps(result["artifacts"]["lstm"]))
    sample = examples([cycle], config, 0.5, include_sequence=True)[-1]
    assert predict_model(artifact, sample) == predict_model(result["artifacts"]["lstm"], sample)
    batch = examples([cycle], config, 0.5, include_sequence=True)[-270:]
    assert predict_many(artifact, batch) == pytest.approx(
        [predict_model(artifact, row) for row in batch], abs=1e-8
    )
    assert predict_many(artifact, []) == []
    energy_artifact = deepcopy(artifact)
    energy_artifact["config"]["task"] = "energy"
    energy_artifact["model"]["targetMean"] = -10000
    assert predict_many(energy_artifact, batch[:2]) == [0, 0]
    assert artifact["predictionInterval"]["calibrationCount"] > 0
    replay = build_replay_package(
        [cycle],
        {"lstm": artifact},
        ReplayRequest(
            experimentId="e",
            cycleId="c",
            horizonMinutes=horizon,
            selectedAlgorithms=["lstm"],
        ),
        0,
        origin_after=450,
    )
    frame = replay.frames[0]
    assert [point.minuteOffset for point in frame.future["lstm"]] == list(range(1, horizon + 1))
    assert all(point.value is not None for point in frame.future["lstm"])
    short = build_replay_package(
        [cycle],
        {"lstm": artifact},
        replay.request.model_copy(update={"horizonMinutes": 5}),
        0,
        origin_after=450,
    )
    assert short.frames[0].future["lstm"] == frame.future["lstm"][:5]
    altered = deepcopy(cycle)
    for point in altered["points"]:
        if point["time"] > frame.originMinute:
            point.update(temp=900, gas=120)
    changed = build_replay_package(
        [altered], {"lstm": artifact}, replay.request, 0, origin_after=450
    )
    assert changed.frames[0].future == frame.future
    assert changed.frames[0].actualFuture != frame.actualFuture
    comparison = compare_temperature_plans(
        cycle,
        artifact,
        TemperatureForecastRequest(
            cycleId="c",
            cursorFraction=frame.originMinute / 600,
            durationMinutes=horizon,
            comparePlans=True,
        ),
        0,
        thermal_enabled=True,
    )
    assert comparison["comparison"]["algorithmPoints"] == horizon
    assert [
        row["algorithmForecast"]
        for row in comparison["points"]
        if row["algorithmForecast"] is not None
    ] == pytest.approx([point.value for point in frame.future["lstm"]])
