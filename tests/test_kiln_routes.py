"""验证 HTTP 请求约束、所选模型贯穿计算与 AI 失败时的可用计算结果。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_kiln_analysis import workspace

from api.deps import get_current_user
from api.routers.kiln_cycles import router
from api.services import kiln_reports
from common.errors import AppError
from common.kiln_analysis import AnalysisRequest
from db.session import get_db


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(router)

    async def mock_workspace(*args, **kwargs):
        return workspace()

    async def mock_db():
        yield SimpleNamespace()

    async def mock_user():
        return SimpleNamespace(id=1)

    @app.exception_handler(AppError)
    async def handle_error(request, exc):
        return JSONResponse(
            {"code": exc.code, "msg": exc.msg, "data": None}, status_code=exc.status_code
        )

    monkeypatch.setattr(kiln_reports, "cycle_workspace", mock_workspace)
    app.dependency_overrides[get_db] = mock_db
    app.dependency_overrides[get_current_user] = mock_user
    return TestClient(app)


@pytest.mark.parametrize("model", ["similar", "data", "physics", "fusion"])
def test_http_analysis_uses_user_selection(client, model):
    response = client.post("/kiln-cycles/2/analyze", json={"selectedModel": model, "cursorHour": 8})
    assert response.status_code == 200
    assert response.json()["data"]["analysis"]["selectedModel"]["key"] == model


def test_http_rejects_bad_parameters_and_unsupported_model(client):
    for body in (
        {"selectedModel": "unknown", "cursorHour": 8},
        {"selectedModel": "data", "cursorHour": -1},
        {"selectedModel": "physics", "cursorHour": 8, "parameters": {"temperatureOffsetC": 500}},
        {"selectedModel": "similar", "cursorHour": 8, "parameters": {"temperatureOffsetC": 20}},
    ):
        assert client.post("/kiln-cycles/2/analyze", json=body).status_code == 422


def test_llm_unavailable_does_not_prevent_numerical_analysis(client, monkeypatch):
    async def no_model(*args, **kwargs):
        raise AppError(50000, "LLM not configured", 503)

    monkeypatch.setattr(kiln_reports, "build_llm_client", no_model)
    body = {"selectedModel": "physics", "cursorHour": 8}
    assert client.post("/kiln-cycles/2/analyze", json=body).status_code == 200
    failure = client.post("/kiln-cycles/2/report", json=body)
    assert failure.status_code == 503
    assert failure.json()["data"] is None


@pytest.mark.asyncio
async def test_ai_success_keeps_selected_model_and_saves_snapshot(monkeypatch):
    import json

    from test_kiln_analysis import workspace

    ws = workspace()
    async_workspace = AsyncMock(return_value=ws)
    monkeypatch.setattr(kiln_reports, "cycle_workspace", async_workspace)
    payload = {
        "title": "六温区用气分析",
        "summary": "按用户所选模型进行计划试算。",
        "kpis": ["remainingGas", "wholeGas", "deltaGas"],
        "panels": [
            {"kind": "gas", "title": "用气预测", "explanation": "所选模型计算"},
            {"kind": "temperature", "title": "温度计划", "explanation": "未来为设定计划"},
        ],
        "insights": ["查看调整前后差异"],
        "limitations": ["尚未验证在线精度"],
    }
    llm = SimpleNamespace(
        fixed_model="test-llm", complete=AsyncMock(return_value=json.dumps(payload))
    )
    monkeypatch.setattr(kiln_reports, "build_llm_client", AsyncMock(return_value=llm))
    saved = []
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock(), add=saved.append)
    request = AnalysisRequest(
        selectedModel="data",
        cursorHour=8,
        parameters={"temperatureOffsetC": 20, "remainingDurationScale": 1.1},
    )
    result = await kiln_reports.generate_report(db, 2, 7, request)
    assert result["analysis"]["selectedModel"]["key"] == "data"
    assert result["request"] == request.model_dump()
    assert result["generatedBy"] == "ai"
    assert saved[0].user_id == 7
    assert saved[0].request == request.model_dump()
    assert saved[0].snapshot["analysis"]["metrics"] == result["analysis"]["metrics"]
    assert {"process", "gas", "comparison"}.issubset(
        {p["kind"] for p in result["dashboard"]["panels"]}
    )
    facts = json.loads(llm.complete.call_args.args[0][1]["content"])["facts"]
    assert facts["selectedModel"]["key"] == "data"
    assert facts["metrics"] == result["analysis"]["metrics"]
    assert "未来炉温预测" in llm.complete.call_args.args[0][0]["content"]


@pytest.mark.asyncio
async def test_insufficient_balance_is_actionable_and_does_not_save_fake_ai(monkeypatch):
    monkeypatch.setattr(kiln_reports, "cycle_workspace", AsyncMock(return_value=workspace()))
    llm = SimpleNamespace(
        fixed_model="test-llm",
        complete=AsyncMock(side_effect=AppError(50000, "LLM request failed (402)", 502)),
    )
    monkeypatch.setattr(kiln_reports, "build_llm_client", AsyncMock(return_value=llm))
    saved = []
    db = SimpleNamespace(commit=AsyncMock(), add=saved.append)
    with pytest.raises(AppError, match="余额不足"):
        await kiln_reports.generate_report(
            db, 2, 7, AnalysisRequest(selectedModel="physics", cursorHour=8)
        )
    assert saved == []
