"""Regress real citation failure modes without accepting rewritten scientific facts."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.kiln_studio_grounding import (
    dataset_context,
    ground_evidence,
    source_quote,
    source_references,
)
from common.kiln_studio_schema import AIInterpretation

SOURCE = {
    "id": "a" * 24 + ":26",
    "fileId": "a" * 24,
    "file": "manual.docx",
    "location": "段落26",
    "text": "燃气低位热值为36 MJ/Nm³；燃烧效率为0.75。热平衡需考虑散热。",
}


def answer(evidence):
    return {
        "summary": "根据资料提出集总机理，参数仍需确认。",
        "mechanisms": ["energy"],
        "evidence": evidence,
        "warnings": [],
    }


def test_short_reference_is_resolved_to_canonical_id_and_actual_source_text():
    references, lookup = source_references([SOURCE])
    assert references[0]["sourceId"] == "S1"
    assert SOURCE["id"] not in json.dumps(references)
    parsed = AIInterpretation.model_validate(answer([{"sourceId": "S1", "reason": "与热平衡有关"}]))
    verified, rejected = ground_evidence(parsed, lookup)
    assert not rejected
    assert verified[0]["sourceId"] == SOURCE["id"]
    assert verified[0]["quote"] == SOURCE["text"]
    assert verified[0]["verification"] == "source_reference"


def test_layout_only_changes_return_original_text():
    original = "燃气热值为36\n MJ / Nm³；\n效率为0.75。"
    quote, method = source_quote(original, "燃气热值为36 MJ/Nm³；效率为0.75。")
    assert quote == original
    assert method == "layout_normalized"


@pytest.mark.parametrize("proposed", ["燃气低位热值为38 MJ/Nm³", "燃烧效率为0.85", "温度最高1500℃"])
def test_changed_numbers_and_invented_claims_are_rejected(proposed):
    assert source_quote(SOURCE["text"], proposed) == (None, None)


def test_digit_cells_and_mathematical_operators_are_not_normalized_into_different_facts():
    assert source_quote("表中数据：3 6", "数据：36") == (None, None)
    assert source_quote("O₂≤3%", "O₂<3%") == (None, None)
    assert source_quote("Nm³/h", "Nm3/h") == (None, None)


def test_one_bad_citation_does_not_discard_other_verified_sources():
    _, lookup = source_references([SOURCE])
    parsed = AIInterpretation.model_validate(
        answer(
            [
                {"sourceId": "S1", "reason": "热平衡关系"},
                {"sourceId": "S999", "reason": "不存在的来源"},
                {"sourceId": "S1", "quote": "热值为100 MJ/Nm³", "reason": "错误引文"},
            ]
        )
    )
    verified, rejected = ground_evidence(parsed, lookup)
    assert len(verified) == 1 and len(rejected) == 2
    assert "100" not in verified[0]["quote"]


def test_data_context_aggregates_all_files_while_bounding_examples():
    summaries = [
        {
            "file": f"{i}.xls",
            "from": "2026-01-01",
            "to": "2026-01-02",
            "count": 10,
            "fields": [{"key": "gas", "present": 8, "total": 10}] if i < 19 else [],
        }
        for i in range(20)
    ]
    ctx = dataset_context({"summary": summaries, "rowCount": 200})
    assert ctx["fileCount"] == 20 and ctx["exampleFileCount"] == 12
    assert ctx["fields"]["gas"] == {"present": 152, "total": 200}


@pytest.fixture
def generation(monkeypatch):
    from api.services import kiln_studio as service

    dataset = SimpleNamespace(
        public_id="dataset",
        project_id="project",
        payload={"summary": [], "rowCount": 10, "config": {"zoneCount": 6}},
    )
    project = SimpleNamespace(payload={"files": [{"sources": [SOURCE]}]})

    async def asset(db, owner, identifier, kind):
        return dataset if kind == "dataset" else project

    monkeypatch.setattr(service, "get_asset", asset)
    updates = AsyncMock()
    save = AsyncMock(return_value=SimpleNamespace(public_id="saved-draft"))
    monkeypatch.setattr(service, "_job_update", updates)
    monkeypatch.setattr(service, "create_asset", save)
    db = SimpleNamespace(commit=AsyncMock())
    job = SimpleNamespace(
        project_id="project", payload={"body": {"datasetId": "dataset", "mode": "ai"}}
    )
    return service, db, job, save, updates


@pytest.mark.asyncio
async def test_generation_automatically_repairs_unverifiable_references_once(
    generation, monkeypatch
):
    service, db, job, save, updates = generation
    llm = SimpleNamespace(
        fixed_model="test-model",
        complete=AsyncMock(
            side_effect=[
                json.dumps(answer([{"sourceId": "wrong", "reason": "找不到来源"}])),
                json.dumps(answer([{"sourceId": "S1", "reason": "来源中的热平衡"}])),
            ]
        ),
    )
    monkeypatch.setattr(service, "build_llm_client", AsyncMock(return_value=llm))
    assert await service._generate_draft(db, 1, job) == "saved-draft"
    assert llm.complete.await_count == 2
    payload = save.call_args.args[5]
    assert payload["citationAudit"]["attempts"] == 2
    assert payload["aiEvidence"][0]["quote"] == SOURCE["text"]
    assert payload["mode"] == "ai"


@pytest.mark.asyncio
async def test_repeated_invalid_evidence_never_saves_a_draft(generation, monkeypatch):
    service, db, job, save, updates = generation
    invalid = json.dumps(answer([{"sourceId": "S999", "reason": "不存在"}]))
    llm = SimpleNamespace(fixed_model="test-model", complete=AsyncMock(return_value=invalid))
    monkeypatch.setattr(service, "build_llm_client", AsyncMock(return_value=llm))
    with pytest.raises(ValueError, match="无法核验"):
        await service._generate_draft(db, 1, job)
    assert llm.complete.await_count == 2
    save.assert_not_called()
    assert updates.call_args_list[-1].kwargs["citationDiagnostics"]["verifiedCount"] == 0


@pytest.mark.asyncio
async def test_balance_or_transport_errors_do_not_trigger_retries_or_fake_template_success(
    generation, monkeypatch
):
    from common.errors import AppError

    service, db, job, save, updates = generation
    llm = SimpleNamespace(
        fixed_model="test-model",
        complete=AsyncMock(side_effect=AppError(50000, "balance unavailable", 503)),
    )
    monkeypatch.setattr(service, "build_llm_client", AsyncMock(return_value=llm))
    with pytest.raises(AppError):
        await service._generate_draft(db, 1, job)
    assert llm.complete.await_count == 1
    save.assert_not_called()
