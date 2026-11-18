from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.kiln_studio_engine import run_experiment
from common.kiln_studio_evaluation import error_statistics, evaluation_detail
from common.kiln_studio_schema import (
    ExperimentRequest,
    PhysicsParameters,
    PredictionEvaluationRequest,
)


def test_percentage_uses_eligible_actuals_and_keeps_them_in_temperature_error():
    score = error_statistics([1200, 50, 0, 0.5, 400], [1215, 65, 15, 15.5, None])
    assert score["rmse"] == score["mae"] == 15
    assert score["mape"] == pytest.approx((1.25 + 30) / 2)
    assert score["mapeCount"] == 2
    assert score["mapeExcludedCount"] == 2
    assert score["count"] == 4 and score["totalCount"] == 5
    assert score["coverage"] == 0.8
    assert error_statistics([0], [15])["mape"] is None
    assert error_statistics([500], [None])["rmse"] is None


def test_quality_uses_all_records_and_keeps_hidden_outliers_in_the_chart():
    rows = [
        {
            "ts": f"time-{index}",
            "origin": f"origin-{index}",
            "cycle": "c",
            "split": "test",
            "actual": 100,
            "ridge": 100,
        }
        for index in range(2001)
    ]
    rows[7]["ridge"] = 250
    rows.append({**rows[0], "cycle": "other", "ridge": 800})
    detail = evaluation_detail(rows, "ridge", None, "test", "c", 1)
    assert detail["metrics"]["count"] == 2001
    assert detail["metrics"]["rmse"] == pytest.approx(150 / 2001**0.5)
    assert detail["worstPoints"][0]["ts"] == "time-7"
    assert detail["worstPoints"][0] in detail["points"]
    assert len(detail["points"]) < 1000


@pytest.mark.asyncio
async def test_older_experiments_evaluate_saved_weights_and_match_full_test_metrics(monkeypatch):
    from api.services import kiln_studio as studio
    from common.errors import AppError

    start = datetime(2026, 1, 1)
    cycles = [
        {
            "id": "c",
            "name": "kiln.csv",
            "points": [
                {
                    "time": index,
                    "ts": (start + timedelta(minutes=index)).isoformat(),
                    "cycle": "c",
                    "temp": 500 + index / 10,
                    "temp_sp": 600,
                    "gas": 50,
                    "air": 500,
                    "afr": 10,
                    "pressure": 3,
                    "o2": 2,
                }
                for index in range(601)
            ],
        }
    ]
    config = ExperimentRequest(
        datasetId="d",
        mechanismId="m",
        task="temperature",
        horizonMinutes=5,
        selectedAlgorithmKeys=["baseline", "physics"],
        calibrate=False,
    )
    result = run_experiment(cycles, config, PhysicsParameters().model_dump(), ["thermal"])
    payload = {
        **result,
        "datasetId": "d",
        "dataHash": "hash",
        "artifactKey": "models",
    }
    payload.pop("evaluationRows")
    assets = {
        "e": SimpleNamespace(project_id="p", payload=payload),
        "d": SimpleNamespace(
            project_id="p",
            payload={
                "dataHash": "hash",
                "dataKey": "cycles",
                "summary": [{"fileId": "c", "file": "kiln.csv"}],
            },
        ),
    }
    storage = {"models": result["artifacts"], "cycles": cycles}
    original = deepcopy(storage)
    get_asset = AsyncMock(side_effect=lambda db, owner, public_id, kind: assets[public_id])
    monkeypatch.setattr(studio, "get_asset", get_asset)
    monkeypatch.setattr(studio, "read_json", lambda owner, key: storage[key])
    body = PredictionEvaluationRequest(algorithm="baseline")
    detail = await studio.prediction_evaluation(None, 7, "e", body)
    expected = next(item["test"] for item in result["algorithms"] if item["key"] == "baseline")
    assert detail["metrics"] == expected
    assert detail["referenceName"] is not None
    assert detail["cycles"] == [{"id": "c", "name": "kiln.csv"}]
    assert storage == original
    assert get_asset.await_args_list[0].args == (None, 7, "e", "experiment")
    assert get_asset.await_args_list[1].args == (None, 7, "d", "dataset")

    payload["evaluationKey"] = "full"
    storage["full"] = result["evaluationRows"]
    saved = await studio.prediction_evaluation(None, 7, "e", body)
    assert saved == detail

    assets["d"].payload["dataHash"] = "changed"
    with pytest.raises(AppError, match="版本不匹配"):
        await studio.prediction_evaluation(None, 7, "e", body)
