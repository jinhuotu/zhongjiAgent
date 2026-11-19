from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from common.kiln_studio_comparison import compare_temperature_plans
from common.kiln_studio_engine import run_experiment
from common.kiln_studio_replay import build_replay_package
from common.kiln_studio_schema import (
    ExperimentRequest,
    PhysicsParameters,
    ReplayRequest,
    TemperatureForecastRequest,
)


@pytest.fixture(scope="module")
def trained():
    date = datetime(2026, 1, 1)
    cycle = {
        "id": "c1",
        "name": "minute.csv",
        "points": [
            {
                "time": minute,
                "ts": (date + timedelta(minutes=minute)).isoformat(),
                "cycle": "c1",
                "temp": 400 + minute * 0.2,
                "temp_sp": 500 + minute * 0.1,
                "gas": 20,
                "air": 200,
                "o2": 2,
                "pressure": 3,
            }
            for minute in range(600)
        ],
    }
    config = ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="temperature",
        historyMinutes=5,
        horizonMinutes=30,
        calibrate=False,
        selectedAlgorithmKeys=["ridge", "fusion"],
    )
    result = run_experiment([cycle], config, PhysicsParameters().model_dump(), ["thermal"])
    return cycle, result


@pytest.mark.parametrize("algorithm", ["ridge", "fusion"])
def test_trained_model_has_all_minute_heads_and_replay_values(trained, algorithm):
    cycle, result = trained
    card = next(item for item in result["algorithms"] if item["key"] == algorithm)
    assert card["predictionOffsets"] == list(range(1, 31))
    artifact = result["artifacts"][algorithm]
    assert set(artifact["temperatureHeads"]) == {str(i) for i in range(1, 30)}
    request = ReplayRequest(
        experimentId="e", cycleId="c1", horizonMinutes=60, selectedAlgorithms=[algorithm]
    )
    package = build_replay_package([cycle], {algorithm: artifact}, request, 0, origin_after=250)
    assert package.algorithms[0].temperatureTrajectory == "multiOutput"
    assert (
        len([point for point in package.frames[0].future[algorithm] if point.value is not None])
        == 60
    )


@pytest.mark.parametrize("algorithm", ["ridge", "fusion"])
def test_comparison_covers_every_minute_without_using_future_observations(trained, algorithm):
    cycle, trained_result = trained
    artifact = trained_result["artifacts"][algorithm]
    request = TemperatureForecastRequest(cycleId="c1", cursorFraction=0.5, durationMinutes=30)
    original = compare_temperature_plans(cycle, artifact, request, 0, thermal_enabled=True)
    assert original["comparison"]["algorithmPoints"] == 30
    changed = deepcopy(cycle)
    for row in changed["points"]:
        if row["ts"] > original["originAt"]:
            row.update(temp=1500, gas=300, air=500, pressure=40, o2=10)
    updated = compare_temperature_plans(changed, artifact, request, 0, thermal_enabled=True)
    def values(result):
        return [(point["minute"], point["algorithmForecast"]) for point in result["points"]]
    assert values(updated) == values(original)


def test_coarse_records_do_not_invent_minute_training_labels():
    date = datetime(2026, 1, 1)
    cycle = {
        "id": "c",
        "name": "coarse.csv",
        "points": [
            {
                "time": t,
                "ts": (date + timedelta(minutes=t)).isoformat(),
                "cycle": "c",
                "temp": 400 + t,
                "temp_sp": 600,
                "gas": 20,
                "air": 200,
                "o2": 2,
                "pressure": 3,
            }
            for t in range(0, 2000, 5)
        ],
    }
    config = ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="temperature",
        historyMinutes=5,
        horizonMinutes=10,
        calibrate=False,
        selectedAlgorithmKeys=["ridge"],
    )
    result = run_experiment([cycle], config, PhysicsParameters().model_dump(), ["thermal"])
    assert result["algorithms"][0]["predictionOffsets"] == [10]
