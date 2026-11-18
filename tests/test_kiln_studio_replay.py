from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from common.kiln_studio_engine import FEATURES, make_features, predict_model, run_experiment
from common.kiln_studio_replay import build_replay_package
from common.kiln_studio_schema import (
    ExperimentRequest,
    PhysicsParameters,
    ReplayExcludedRange,
    ReplayPackage,
    ReplayRequest,
)


def example(algorithms=("baseline", "ridge", "physics", "fusion"), horizon=60, task="temperature"):
    start = datetime(2026, 1, 1)
    points = [
        {
            "time": minute,
            "ts": (start + timedelta(minutes=minute)).isoformat(),
            "cycle": "cycle",
            "temp": 100 + minute,
            "temp_sp": 160 + minute * 0.25,
            "z1_temp": 100 + minute,
            "gas": 60,
            "air": 660,
            "afr": 11,
            "o2": 2.1,
            "pressure": 3,
        }
        for minute in range(121)
    ]
    config = ExperimentRequest(datasetId="d", mechanismId="m", task=task, historyMinutes=5)
    weights = [0, 1, *([0] * (len(FEATURES) - 1))]
    artifacts = {
        key: {
            "algorithm": key,
            "config": config.model_dump(),
            "parameters": PhysicsParameters().model_dump(),
            "preprocessor": {"mean": [0] * len(FEATURES), "scale": [1] * len(FEATURES)},
            "model": [2, *([0] * len(FEATURES))] if key == "fusion" else weights,
        }
        for key in algorithms
    }
    request = ReplayRequest(
        experimentId="e",
        cycleId="cycle",
        horizonMinutes=horizon,
        selectedAlgorithms=list(algorithms),
    )
    return [{"id": "cycle", "name": "cycle.csv", "points": points}], artifacts, request


def test_replay_future_observations_do_not_change_predicted_paths():
    cycles, artifacts, request = example()
    changed = deepcopy(cycles)
    for point in changed[0]["points"][21:]:
        point.update(temp=500, z1_temp=550, gas=30, air=220, o2=6, pressure=-10)
    first = build_replay_package(cycles, artifacts, request, 1, origin_after=19).frames[0]
    second = build_replay_package(changed, artifacts, request, 1, origin_after=19).frames[0]
    assert first.originMinute == 20
    assert first.future == second.future
    assert first.fusionReference == second.fusionReference
    assert first.actualFuture != second.actualFuture
    assert first.snapshot == second.snapshot
    assert first.snapshot.zones[0].airValve is None
    assert [p.minuteOffset for p in first.future["ridge"] if p.value is not None] == [30, 60]
    assert first.future["ridge"][59].value == 120
    assert all(point.value is not None for point in first.future["physics"])
    assert first.fusionReference is not None
    assert first.future["fusion"][29].value == pytest.approx(first.fusionReference[29].value + 2)


@pytest.mark.parametrize("window", [5, 10, 30])
def test_temperature_persistence_outputs_every_minute_independent_of_training_horizon(window):
    cycles, artifacts, request = example(("baseline",), window)
    original = build_replay_package(cycles, artifacts, request, 1, origin_after=19)
    assert original.algorithms[0].temperatureTrajectory == "persistence"
    frame = original.frames[0]
    assert [(point.minuteOffset, point.value) for point in frame.future["baseline"]] == [
        (minute, 120) for minute in range(1, window + 1)
    ]
    for row in cycles[0]["points"][21:]:
        row.update(temp=900, z1_temp=920, temp_sp=1000)
    revised = build_replay_package(cycles, artifacts, request, 1, origin_after=19).frames[0]
    assert revised.future == frame.future
    assert revised.actualFuture != frame.actualFuture


def test_temperature_persistence_matches_the_zone_and_stops_before_record_gap():
    cycles, artifacts, request = example(("baseline",), 10)
    artifacts["baseline"]["config"]["zone"] = 1
    cycles[0]["points"][20].update(temp=300, z1_temp=401)
    del cycles[0]["points"][24:29]
    frame = build_replay_package(cycles, artifacts, request, 1, origin_after=19).frames[0]
    assert [point.value for point in frame.future["baseline"]] == [401] * 3 + [None] * 7


def test_excluded_replay_keeps_wall_clock_gaps_and_uses_only_retained_inputs():
    cycles, artifacts, request = example(("ridge",), 30)
    start = datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))).timestamp() / 60
    for point in cycles[0]["points"]:
        point["time"] += start
    original = deepcopy(cycles)
    artifacts["ridge"]["config"]["horizonMinutes"] = 1
    request = request.model_copy(update={"includeMechanism": True, "frameStepMinutes": 1})
    parameters = artifacts["ridge"]["parameters"]
    baseline = build_replay_package(cycles, artifacts, request, 1, mechanism_parameters=parameters)
    excluded_request = request.model_copy(
        update={
            "excludedRange": ReplayExcludedRange(start="2026-01-01T00:30", end="2026-01-01T00:50")
        }
    )
    filtered = build_replay_package(
        cycles, artifacts, excluded_request, 1, mechanism_parameters=parameters
    )
    frames = {frame.originMinute: frame for frame in filtered.frames}
    assert filtered.excludedCount == 20
    assert filtered.baseSeries[30].minute == 30
    assert filtered.baseSeries[30].actual is filtered.baseSeries[30].setpoint is None
    assert filtered.baseSeries[50].actual == baseline.baseSeries[50].actual
    assert 30 not in frames and 49 not in frames
    assert frames[28].future["ridge"][0] == baseline.frames[28].future["ridge"][0]
    assert frames[28].future["ridge"][1].value is None
    assert frames[28].actualFuture[1].value is None
    assert frames[28].mechanismFuture[1].value is None
    assert frames[50].future["ridge"][0].value is None
    assert frames[55].future["ridge"][0] == baseline.frames[55].future["ridge"][0]
    assert cycles == original


@pytest.mark.parametrize("task", ["temperature", "energy", "afr"])
def test_independent_mechanism_and_short_algorithm_share_rolling_windows(task):
    cycles, artifacts, request = example(("ridge",), 30, task=task)
    artifacts["ridge"]["config"]["horizonMinutes"] = 1
    request = request.model_copy(update={"includeMechanism": True, "frameStepMinutes": 1})
    parameters = artifacts["ridge"]["parameters"]
    replay = build_replay_package(
        cycles, artifacts, request, 1, origin_after=19, mechanism_parameters=parameters
    )
    before, after = replay.frames[:2]
    assert (before.originMinute, after.originMinute) == (20, 21)
    assert all(before.future["ridge"][minute - 1].value is not None for minute in (1, 5, 30))
    assert before.mechanismFuture is not None
    assert all(before.mechanismFuture[minute - 1].value is not None for minute in (1, 5, 30))
    assert before.mechanismIssue is None
    changed = deepcopy(cycles)
    for point in changed[0]["points"][21:]:
        point.update(temp=600, z1_temp=620, gas=90, air=690)
    new_replay = build_replay_package(
        changed, artifacts, request, 1, origin_after=19, mechanism_parameters=parameters
    )
    assert new_replay.frames[0].actualFuture != before.actualFuture
    assert new_replay.frames[0].future == before.future
    assert new_replay.frames[0].mechanismFuture == before.mechanismFuture
    if task == "afr":
        assert before.mechanismFuture[4].value == pytest.approx(
            parameters["theoreticalAfr"] * parameters["excessAir"]
        )


def test_rolling_replay_reports_disabled_and_gap_blocked_mechanism():
    cycles, artifacts, request = example(("baseline",), 30, task="energy")
    request = request.model_copy(update={"includeMechanism": True})
    default = build_replay_package(cycles, artifacts, request, 1, origin_after=19).frames[0]
    assert default.mechanismFuture is None
    assert "未启用" in default.mechanismIssue
    del cycles[0]["points"][22:32]
    frame = build_replay_package(
        cycles,
        artifacts,
        request,
        1,
        origin_after=19,
        mechanism_parameters=artifacts["baseline"]["parameters"],
    ).frames[0]
    assert [p.minuteOffset for p in frame.mechanismFuture if p.value is not None] == [1]
    assert "断档" in frame.mechanismIssue


def test_new_one_minute_experiment_yields_a_real_five_minute_trajectory():
    cycles, _, request = example(("ridge",), 30)
    config = ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="temperature",
        historyMinutes=5,
        horizonMinutes=1,
        selectedAlgorithmKeys=["ridge"],
    )
    parameters = PhysicsParameters().model_dump()
    result = run_experiment(cycles, config, parameters, ["thermal"])
    start = datetime.fromisoformat(cycles[0]["points"][0]["ts"])
    training_end = datetime.fromisoformat(result["split"][0]["to"])
    replay = build_replay_package(
        cycles,
        result["artifacts"],
        request.model_copy(update={"includeMechanism": True}),
        1,
        origin_after=(training_end - start).total_seconds() / 60,
        mechanism_parameters=result["parameters"],
    )
    first = replay.frames[0]
    assert first.originMinute > (training_end - start).total_seconds() / 60
    assert [
        point.minuteOffset for point in first.future["ridge"][:5] if point.value is not None
    ] == [
        1,
        2,
        3,
        4,
        5,
    ]


def test_short_window_and_missing_signals_keep_nulls():
    cycles, artifacts, request = example(("ridge", "physics"), 5)
    cycles[0]["points"][20].update(temp=-9999, z1_temp=-9999, gas=-9999)
    result = build_replay_package(cycles, artifacts, request, 1, origin_after=19)
    first, following = result.frames[:2]
    assert first.snapshot.temp is None and first.snapshot.afr is None
    assert first.snapshot.zones[0].temp is None
    assert all(p.value is None for points in first.future.values() for p in points)
    assert all(p.value is None for p in following.future["ridge"])
    assert all(p.value is not None for p in following.future["physics"])


def test_mechanism_does_not_bridge_plan_gaps():
    cycles, artifacts, request = example(("physics",), 30)
    del cycles[0]["points"][22:32]
    frame = build_replay_package(cycles, artifacts, request, 1, origin_after=19).frames[0]
    assert [p.minuteOffset for p in frame.future["physics"] if p.value is not None] == [1]
    assert any(p.minuteOffset == 12 for p in frame.actualFuture)


def test_replay_rejects_unknown_artifacts_and_pretraining_origins():
    cycles, artifacts, request = example(("lasso",))
    with pytest.raises(ValueError, match="训练产物"):
        build_replay_package(cycles, {}, request, 1)
    with pytest.raises(ValueError, match="可回放"):
        build_replay_package(cycles, artifacts, request, 1, origin_after=120)
    with pytest.raises(ValueError, match="重复"):
        ReplayRequest.model_validate(
            {**request.model_dump(), "selectedAlgorithms": ["lasso", "lasso"]}
        )


@pytest.mark.parametrize(
    ("task", "unit", "actual"), [("energy", "Nm³/h", 60), ("afr", "Nm³/Nm³", 11)]
)
def test_flow_and_ratio_use_saved_models_at_native_horizons(task, unit, actual):
    cycles, artifacts, request = example(task=task)
    changed = deepcopy(cycles)
    for row in changed[0]["points"][21:]:
        row.update(temp=999, gas=0.01, air=150, afr=88, o2=5, pressure=-20)
    package = build_replay_package(cycles, artifacts, request, 1, origin_after=19)
    updated = build_replay_package(changed, artifacts, request, 1, origin_after=19)
    frame = package.frames[0]
    assert (package.task, package.unit, package.baseSeries[20].actual) == (task, unit, actual)
    assert frame.future == updated.frames[0].future
    assert frame.actualFuture != updated.frames[0].actualFuture
    assert frame.fusionReference is None
    points = cycles[0]["points"]
    for algorithm in ("physics", "fusion", "ridge", "baseline"):
        artifact = artifacts[algorithm]
        config = ExperimentRequest.model_validate(artifact["config"])
        example_features = make_features(points[15:21], points[50]["temp_sp"], config)
        assert frame.future[algorithm][29].value == pytest.approx(
            predict_model(artifact, example_features)
        )
        offsets = [
            point.minuteOffset for point in frame.future[algorithm] if point.value is not None
        ]
        assert offsets == [30, 60]


def test_afr_replay_uses_model_flow_floor_for_observations_and_origin():
    cycles, artifacts, request = example(("physics", "ridge"), task="afr")
    for artifact in artifacts.values():
        artifact["parameters"]["minGas"] = 10
    points = cycles[0]["points"]
    points[20].update(gas=5, air=200, afr=40)
    points[21].update(gas=10, air=130, afr=999)
    points[22].update(gas=12, air=-9999, afr=99)
    replay = build_replay_package(cycles, artifacts, request, 1, origin_after=19)
    first = replay.frames[0]
    assert first.snapshot.afr is None
    assert all(point.value is None for series in first.future.values() for point in series)
    assert [point.value for point in first.actualFuture[:2]] == [13, None]


def test_previous_temperature_packages_default_to_temperature():
    cycles, artifacts, request = example(("physics",))
    stored = build_replay_package(cycles, artifacts, request, 1).model_dump(
        exclude={"task", "unit"}
    )
    replay = ReplayPackage.model_validate(stored)
    assert (replay.task, replay.unit) == ("temperature", "℃")
