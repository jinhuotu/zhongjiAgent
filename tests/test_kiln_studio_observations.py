from copy import deepcopy

from common.kiln_studio_engine import examples
from common.kiln_studio_evaluation import evaluation_detail
from common.kiln_studio_files import prepare_cycle
from common.kiln_studio_observations import clean_rows, zone_observation_trend
from common.kiln_studio_schema import ExperimentRequest


def test_pressure_sheet_preview_and_process_frames_restore_archived_channels():
    from common.kiln_studio_files import source_sheet_preview, standard_rows
    from common.kiln_studio_observations import process_observations

    timestamp = "2024-04-28 01:04:00"
    pressure = [
        [
            None,
            None,
            "窑内压力",
            None,
            None,
            "燃气压力",
            None,
            None,
            "助燃风压力",
            None,
            None,
            "燃气流量",
            None,
            "助燃风流量",
            None,
            "氧含量",
        ],
        [
            "查询时间",
            "温度设定值",
            "设定",
            "实测",
            "输出",
            "设定",
            "实测",
            "输出",
            "设定",
            "测量值（KPa）",
            "输出",
            "瞬时",
            "累计",
            "瞬时",
            "累计",
            "设定",
            "测量",
        ],
        [
            timestamp,
            1025.4,
            2,
            1,
            0,
            20,
            -0.1,
            0,
            3.26,
            3.25,
            44.6,
            -0.1,
            364.9,
            633.6,
            7422.3,
            21,
            18.8,
        ],
    ]
    temp = [
        [None, None, None, "一区温度", None, None, None, "换热器温度"],
        ["查询时间", "设定", "配比", "测量", "燃气", "风量", "风阀", "测量值(℃)"],
        [timestamp, 1025.4, 1, 1000, 1, 2, 30, 470],
    ]
    archive = {
        "sheets": [("温度", temp), ("压力", pressure)],
        "standard": [{"ts": timestamp.replace(" ", "T"), "z1_temp": 1000, "o2": 18.8, "gas": -0.1}],
    }
    preview = source_sheet_preview(archive)[1]
    assert preview["columns"][9] == "助燃风压力 / 测量值（KPa）"
    assert preview["rows"][0][9] == 3.25
    points, _ = prepare_cycle(standard_rows(archive), "c")
    frame = process_observations(points, 1)["frames"][0]["values"]
    assert (frame["air_p_sp"], frame["air_p_meas"], frame["air_p_out"]) == (3.26, 3.25, 44.6)
    assert frame["air_total"] == 7422.3 and frame["gas"] == -0.1
    assert frame["exchanger_temp"] == 470 and frame["z1_air_valve"] == 30
    assert "air_p_meas" not in archive["standard"][0]


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


def test_counter_health_detects_reset_but_ignores_small_jitter():
    from common.kiln_studio_observations import counter_health

    # 第4帧燃气表大幅重置（100 -> 5），助燃风表正常递增；起点为 0
    rows = [
        {"time": 0, "gas_total": 0, "air_total": 0},
        {"time": 1, "gas_total": 50, "air_total": 500},
        {"time": 2, "gas_total": 100, "air_total": 1000},
        {"time": 3, "gas_total": 5, "air_total": 1005},
        {"time": 4, "gas_total": 10, "air_total": 1010},
    ]
    health = counter_health(rows)
    assert health["gas_total"]["resetCount"] == 1
    assert health["air_total"]["resetCount"] == 0
    assert health["gas_total"]["nonzeroStart"] is False
    # 浮点抖动（-0.01）不判重置
    jitter = [
        {"time": 0, "gas_total": 100.0},
        {"time": 1, "gas_total": 99.99},
        {"time": 2, "gas_total": 100.5},
    ]
    assert counter_health(jitter)["gas_total"]["resetCount"] == 0


def test_nonzero_start_uses_start_to_end_ratio():
    from common.kiln_studio_observations import counter_health

    # 窑次06式：起点已带大量累计（首/末 > 30%）
    unzeroed = [
        {"time": 0, "gas_total": 2744},
        {"time": 1, "gas_total": 4000},
        {"time": 2, "gas_total": 5342},
    ]
    assert counter_health(unzeroed)["gas_total"]["nonzeroStart"] is True
    # 正常窑次：起点几 Nm³、末值约 3000，不判未清零
    normal = [
        {"time": 0, "gas_total": 5},
        {"time": 1, "gas_total": 1500},
        {"time": 2, "gas_total": 3000},
    ]
    assert counter_health(normal)["gas_total"]["nonzeroStart"] is False


def test_freeze_segments_detects_whole_frame_freeze():
    from common.kiln_studio_observations import freeze_segments

    rows = [
        {"time": 0, "ts": "t0", "temp": 100, "gas": 10, "air": 100, "z1_temp": 100},
        {"time": 1, "ts": "t1", "temp": 100, "gas": 10, "air": 100, "z1_temp": 100},
        {"time": 2, "ts": "t2", "temp": 100, "gas": 10, "air": 100, "z1_temp": 100},
        {"time": 3, "ts": "t3", "temp": 105, "gas": 11, "air": 110, "z1_temp": 105},
        {"time": 4, "ts": "t4", "temp": 110, "gas": 12, "air": 120, "z1_temp": 110},
    ]
    result = freeze_segments(rows, 1)
    assert result["frozenRows"] == 3
    assert result["longestMinutes"] == 2
    assert len(result["segments"]) == 1
