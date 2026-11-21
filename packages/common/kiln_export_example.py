"""从已绑定的数据生成真实离线输入，不使用随机或手工填写的演示特征。"""

from bisect import bisect_left

from common.errors import AppError, ErrorCode
from common.kiln_studio_engine import make_features, model_uses_sequence, predict_model
from common.kiln_studio_observations import clean_rows
from common.kiln_studio_schema import ExperimentRequest


def model_example(model, cycles, zone_count):
    config = ExperimentRequest.model_validate(model["config"])
    sequence = model_uses_sequence(model)
    for cycle in cycles:
        rows = clean_rows(
            cycle["points"],
            zone_count,
            afr_floor=model["parameters"]["minGas"] if config.task == "afr" else None,
            exclude_zero_temperature=config.excludeZeroTemperatureRecords,
        )
        times = [row["time"] for row in rows]
        for index, row in enumerate(rows):
            if row["time"] - times[0] < config.historyMinutes:
                continue
            future = bisect_left(times, row["time"] + config.horizonMinutes)
            if future == len(rows):
                break
            features = make_features(
                rows[: index + 1],
                rows[future].get("temp_sp"),
                config,
                include_sequence=sequence,
            )
            if features is None:
                continue
            value = predict_model(model, features)
            if value is not None:
                example = {
                    "features": dict(zip(model["features"], features["x"], strict=True)),
                    "origin": features["origin"],
                    "cycleId": cycle["id"],
                    "expected": value,
                }
                if sequence:
                    example["sequence"] = features["sequence"]
                return example
    raise AppError(
        ErrorCode.VALIDATION,
        f"{model['algorithm']} 的绑定数据中没有可用历史窗口，无法生成真实输入示例",
        422,
    )


def build_export_example(data, cycles):
    zone_count = data["datasetConfig"].get("zoneCount", 0)
    return {
        "bundleId": data["bundle"]["id"],
        "inputs": [
            {"modelIndex": index, **model_example(model, cycles, zone_count)}
            for index, model in enumerate(data["models"])
        ],
    }
