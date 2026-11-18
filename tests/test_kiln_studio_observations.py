from copy import deepcopy

from common.kiln_studio_engine import examples
from common.kiln_studio_evaluation import evaluation_detail
from common.kiln_studio_files import prepare_cycle
from common.kiln_studio_observations import clean_rows, zone_observation_trend
from common.kiln_studio_schema import ExperimentRequest


def test_sentinels_are_missing_for_training_and_measured_targets():
    points = [
        {
            "time": index,
            "ts": f"2026-01-01T00:{index:02}:00",
            "cycle": "c",
            "temp": 500,
            "temp_sp": 510,
            "z1_temp": 500,
        }
        for index in range(60)
    ]
    points[30].update(temp=-9999, z1_temp=-9999, temp_sp=-9999)
    before = deepcopy(points)
    request = ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="temperature",
        horizonMinutes=5,
        historyMinutes=5,
    )
    samples = examples([{"id": "c", "points": points}], request, 0.5)
    assert samples
    assert all(sample["y"] != -9999 and sample["y0"] != -9999 for sample in samples)
    assert all(value != -9999 for sample in samples for value in sample["x"] if value is not None)
    assert points == before


def test_zero_temperature_mask_requires_the_recorded_rule_and_all_zones_and_setpoint():
    points = [
        {
            "time": 1,
            "ts": "2026-01-01T00:01:00",
            "cycle": "c",
            "temp": 0,
            "temp_sp": 0,
            "z1_temp": 0,
        },
        {
            "time": 2,
            "ts": "2026-01-01T00:02:00",
            "cycle": "c",
            "temp": 0,
            "temp_sp": 20,
            "z1_temp": 0,
        },
    ]
    assert clean_rows(points)[0]["temp"] == 0
    filtered = clean_rows(points, exclude_zero_temperature=True)
    assert filtered[0]["temp"] is filtered[0]["temp_sp"] is None
    assert filtered[1]["temp"] == 0


def test_temperature_observations_show_each_zone_without_filling_missing_readings():
    points = [
        {"time": 0, "z1_temp": 600, "z2_temp": 580},
        {"time": 1, "z1_temp": 601, "z2_temp": -9999},
        {"time": 2, "z1_temp": 602, "z2_temp": 581},
        {"time": 10, "z1_temp": 630, "z2_temp": 610},
    ]
    trend = zone_observation_trend(points, 3)
    assert trend["zones"] == [1, 2]
    assert trend["rowCount"] == trend["displayCount"] == 4
    assert [row["temperatures"] for row in trend["points"]] == [
        [600, 580, None],
        [601, None, None],
        [602, 581, None],
        [None, None, None],
        [630, 610, None],
    ]


def test_backtest_retains_the_actual_channel_and_origin_of_total_temperature():
    raw = [
        {
            "ts": f"2026-01-01T00:{index:02}:00",
            "temp": 700 if index % 2 else None,
            "z1_temp": 400,
            "z2_temp": 600,
        }
        for index in range(60)
    ]
    points, _ = prepare_cycle(raw, "c")
    config = ExperimentRequest(
        datasetId="d", mechanismId="m", task="temperature", horizonMinutes=5, historyMinutes=5
    )
    cycles = [{"id": "c", "points": points}]
    samples = examples(cycles, config, 0.5)
    assert {(row["y"], row["actualTemperatureSource"]) for row in samples} == {
        (500, "zoneAverage"),
        (700, "provided"),
    }
    zone = examples(cycles, config.model_copy(update={"zone": 2}), 0.5)
    assert {row["y"] for row in zone} == {600}
    row = samples[0]
    result = evaluation_detail(
        [{**row, "split": "test", "actual": row["y"], "baseline": 500}],
        "baseline",
        None,
        "test",
        "c",
        1,
    )
    assert result["points"][0]["actualTemperatureSource"] == row["actualTemperatureSource"]
