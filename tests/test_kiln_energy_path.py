from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from common.kiln_studio_engine import run_experiment
from common.kiln_studio_replay import build_replay_package
from common.kiln_studio_schema import ExperimentRequest, PhysicsParameters, ReplayRequest


@pytest.fixture(scope="module")
def energy_model():
    date = datetime(2026, 1, 1)
    cycle = {
        "id": "gas",
        "name": "gas.csv",
        "points": [
            {
                "time": minute,
                "ts": (date + timedelta(minutes=minute)).isoformat(),
                "cycle": "gas",
                "temp": 400 + minute * 0.2,
                "temp_sp": 500 + minute * 0.1,
                "gas": 30 + minute * 0.03 + (minute % 11) * 0.002,
                "air": 600,
                "o2": 2,
                "pressure": 3,
            }
            for minute in range(600)
        ],
    }
    config = ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="energy",
        historyMinutes=5,
        horizonMinutes=30,
        calibrate=False,
        selectedAlgorithmKeys=["ridge", "fusion", "physics", "baseline"],
    )
    result = run_experiment([cycle], config, PhysicsParameters().model_dump(), ["energy"])
    return cycle, result


@pytest.mark.parametrize("algorithm", ["ridge", "fusion"])
def test_energy_model_outputs_sixty_real_minute_predictions(energy_model, algorithm):
    cycle, trained = energy_model
    artifact = trained["artifacts"][algorithm]
    card = next(item for item in trained["algorithms"] if item["key"] == algorithm)
    assert card["predictionOffsets"] == list(range(1, 31))
    assert set(artifact["minuteHeads"]) == {str(i) for i in range(1, 30)}
    request = ReplayRequest(
        experimentId="e", cycleId="gas", horizonMinutes=60, selectedAlgorithms=[algorithm]
    )
    package = build_replay_package([cycle], {algorithm: artifact}, request, 0, origin_after=250)
    assert package.task == "energy" and package.unit == "Nm³/h"
    assert package.algorithms[0].forecastTrajectory == "multiOutput"
    assert package.algorithms[0].temperatureTrajectory is None
    points = package.frames[0].future[algorithm]
    assert [point.minuteOffset for point in points if point.value is not None] == list(range(1, 61))
    assert all(point.value >= 0 for point in points)


@pytest.mark.parametrize("algorithm", ["ridge", "fusion"])
def test_energy_forecast_does_not_read_future_gas_or_temperature(energy_model, algorithm):
    cycle, trained = energy_model
    artifact = trained["artifacts"][algorithm]
    request = ReplayRequest(
        experimentId="e", cycleId="gas", horizonMinutes=30, selectedAlgorithms=[algorithm]
    )
    original = build_replay_package([cycle], {algorithm: artifact}, request, 0, origin_after=250)
    changed = deepcopy(cycle)
    origin = original.frames[0].originTs
    for point in changed["points"]:
        if point["ts"] > origin:
            point.update(gas=999, temp=1600, air=2000, o2=20, pressure=50)
    replay = build_replay_package([changed], {algorithm: artifact}, request, 0, origin_after=250)
    assert replay.frames[0].future == original.frames[0].future
    assert replay.frames[0].actualFuture != original.frames[0].actualFuture


@pytest.mark.parametrize(
    "algorithm,method", [("baseline", "persistence"), ("physics", "mechanism")]
)
def test_energy_reference_algorithms_have_minute_coverage(energy_model, algorithm, method):
    cycle, trained = energy_model
    artifact = trained["artifacts"][algorithm]
    request = ReplayRequest(
        experimentId="e", cycleId="gas", horizonMinutes=30, selectedAlgorithms=[algorithm]
    )
    package = build_replay_package([cycle], {algorithm: artifact}, request, 0, origin_after=250)
    assert package.algorithms[0].forecastTrajectory == method
    assert sum(point.value is not None for point in package.frames[0].future[algorithm]) == 30


def test_legacy_gas_artifact_keeps_its_sparse_horizon(energy_model):
    cycle, trained = energy_model
    artifact = {
        key: value for key, value in trained["artifacts"]["ridge"].items() if key != "minuteHeads"
    }
    request = ReplayRequest(
        experimentId="e", cycleId="gas", horizonMinutes=30, selectedAlgorithms=["ridge"]
    )
    package = build_replay_package([cycle], {"ridge": artifact}, request, 0, origin_after=250)
    assert package.algorithms[0].forecastTrajectory == "fixedHorizon"
    assert [
        point.minuteOffset for point in package.frames[0].future["ridge"] if point.value is not None
    ] == [30]
