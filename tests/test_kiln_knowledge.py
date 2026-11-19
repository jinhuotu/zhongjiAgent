"""Project documents preserve history and extract mechanisms without datasets."""

import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from fastapi import FastAPI, UploadFile
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from test_kiln_mechanism_catalog import studio_db as _studio_db
from test_kiln_mechanism_catalog import upload_manual

from api.deps import get_current_user, get_db
from api.routers.kiln_studio import router
from api.services import kiln_knowledge as knowledge
from api.services import kiln_studio as studio
from common.errors import AppError
from common.kiln_studio_schema import DocumentRecognitionRequest

studio_db = _studio_db


def upload(name, text):
    return UploadFile(BytesIO(text.encode()), filename=name)


async def initial_project(studio_db):
    async with studio_db() as db:
        project_id, job = await upload_manual(db, "热惯性：时间常数45分钟。\ndT/dt = (T_sp-T)/τ")
    await studio.execute_job(job["id"], 7)
    return project_id


async def test_append_delete_reparse_preserves_history_and_no_resurrection(studio_db):
    project_id = await initial_project(studio_db)
    async with studio_db() as db:
        before = await studio.project_detail(db, 7, project_id)
        original = before["project"]["files"][0]
        evidence = (await studio.mechanism_catalog(db, 7))["items"]
        candidate = next(item for item in evidence if item["status"] == "pending")
        history = await studio.create_asset(
            db, 7, project_id, "dataset", "旧数据版本", {"fileIds": [original["id"]]}
        )
        history_id = history.public_id
        await db.commit()
        job = await knowledge.append_files(
            db,
            7,
            project_id,
            [upload("supplement.txt", "机理：排烟循环：回流影响炉内气氛稳定，需要分阶段确认。")],
        )
        with pytest.raises(AppError, match="任务运行"):
            await knowledge.delete_file(db, 7, project_id, original["id"])
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        detail = await studio.project_detail(db, 7, project_id)
        assert len(detail["project"]["files"]) == 2
        supplement = next(
            file for file in detail["project"]["files"] if file["path"] == "supplement.txt"
        )
        assert supplement["uploadedAt"] and supplement["parsedAt"]
        await knowledge.delete_file(db, 7, project_id, original["id"])
        assert (await studio.get_asset(db, 7, history_id)).payload["fileIds"] == [original["id"]]
        catalog = (await studio.mechanism_catalog(db, 7))["items"]
        assert (
            next(item for item in catalog if item["id"] == candidate["id"])["evidence"]
            == candidate["evidence"]
        )
        job = await studio.reparse_project(db, 7, project_id)
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        detail = await studio.project_detail(db, 7, project_id)
        assert [file["id"] for file in detail["project"]["files"]] == [supplement["id"]]
        job = await knowledge.queue_reparse(db, 7, project_id, supplement["id"])
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        assert (await studio.get_asset(db, 7, job["id"], "job")).payload["status"] == "succeeded"


async def test_rules_recognition_is_dataset_independent_and_idempotent(studio_db):
    project_id = await initial_project(studio_db)
    async with studio_db() as db:
        job = await knowledge.queue_recognition(db, 7, project_id, DocumentRecognitionRequest())
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        result = (await studio.get_asset(db, 7, job["id"], "job")).payload
        assert result["status"] == "succeeded"
        assert result["recognition"]["matched"] > 0
        assert result["recognition"]["created"] == 0
        assert not await studio.list_assets(db, 7, "dataset", project_id)


async def test_document_mutations_check_ownership_and_stale_selection(studio_db):
    project_id = await initial_project(studio_db)
    async with studio_db() as db:
        file = (await studio.project_detail(db, 7, project_id))["project"]["files"][0]
        for owner, file_id in [(8, file["id"]), (7, "missing")]:
            with pytest.raises(AppError):
                await knowledge.delete_file(db, owner, project_id, file_id)
        with pytest.raises(AppError):
            await knowledge.queue_recognition(db, 8, project_id, DocumentRecognitionRequest())
        with pytest.raises(AppError):
            await knowledge.queue_recognition(
                db, 7, project_id, DocumentRecognitionRequest(fileIds=["missing"])
            )
        with pytest.raises(AppError):
            await knowledge.append_files(db, 8, project_id, [upload("other.txt", "资料")])
        with pytest.raises(AppError, match="路径已存在"):
            await knowledge.append_files(db, 7, project_id, [upload("manual.txt", "新内容")])
        assert len((await studio.project_detail(db, 7, project_id))["project"]["files"]) == 1


async def test_archive_leaf_deletion_survives_reparse(studio_db):
    blob = BytesIO()
    with ZipFile(blob, "w") as archive:
        archive.writestr("a.txt", "热惯性：时间常数30分钟")
        archive.writestr("b.txt", "热平衡：燃气输入与损失")
    async with studio_db() as db:
        project_id, job = await studio.upload_project(
            db, 7, [UploadFile(BytesIO(blob.getvalue()), filename="docs.zip")], "文档"
        )
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        files = (await studio.project_detail(db, 7, project_id))["project"]["files"]
        await knowledge.delete_file(db, 7, project_id, files[0]["id"])
        job = await studio.reparse_project(db, 7, project_id)
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        files_after = (await studio.project_detail(db, 7, project_id))["project"]["files"]
        assert len(files_after) == 1 and files_after[0]["id"] == files[1]["id"]


def ai_content(source_id="S1"):
    return {
        "name": "换热回流关系",
        "explanation": "回流质量与总质量的比值。",
        "formula": "R_mix = m_return / m_total",
        "variables": ["m_return: kg", "m_total: kg"],
        "parameterNotes": "",
        "parameterValues": {},
        "check": "需核对回流测量",
        "scope": {},
        "templateKey": None,
        "sourceIds": [source_id],
    }


@pytest.mark.parametrize("invalid", [False, True])
async def test_ai_candidates_validate_sources_and_deduplicate(studio_db, monkeypatch, invalid):
    project_id = await initial_project(studio_db)
    response = {"mechanisms": [ai_content(), ai_content("unknown")] if invalid else [ai_content()]}
    complete = AsyncMock(return_value=json.dumps(response))
    monkeypatch.setattr(
        studio,
        "build_llm_client",
        AsyncMock(return_value=SimpleNamespace(fixed_model="test", complete=complete)),
    )
    for _ in range(2):
        async with studio_db() as db:
            job = await knowledge.queue_recognition(
                db, 7, project_id, DocumentRecognitionRequest(mode="ai")
            )
        await studio.execute_job(job["id"], 7)
        async with studio_db() as db:
            result = (await studio.get_asset(db, 7, job["id"], "job")).payload
            entries = [
                item
                for item in (await studio.mechanism_catalog(db, 7))["items"]
                if item["name"] == "换热回流关系"
            ]
            if invalid:
                assert result["status"] == "failed" and not entries
            else:
                assert result["status"] == "succeeded" and len(entries) == 1
                assert result["recognition"]["created"] == (1 if _ == 0 else 0)
                assert entries[0]["status"] == "pending" and not entries[0]["solverSupported"]
                assert entries[0]["evidence"][0]["file"] == "manual.txt"


async def test_ai_processes_all_batches(studio_db, monkeypatch):
    project_id = await initial_project(studio_db)
    complete = AsyncMock(return_value=json.dumps({"mechanisms": []}))
    monkeypatch.setattr(
        studio,
        "build_llm_client",
        AsyncMock(return_value=SimpleNamespace(fixed_model="test", complete=complete)),
    )
    async with studio_db() as db:
        project = await studio.get_asset(db, 7, project_id, "project")
        file = project.payload["files"][0]
        file = {
            **file,
            "sources": [
                {
                    "id": f"source-{i}",
                    "fileId": file["id"],
                    "file": file["path"],
                    "location": f"段落{i}",
                    "text": "温控" * 900,
                }
                for i in range(40)
            ],
        }
        project.payload = {**project.payload, "files": [file]}
        job = await knowledge.queue_recognition(
            db, 7, project_id, DocumentRecognitionRequest(mode="ai")
        )
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        result = (await studio.get_asset(db, 7, job["id"], "job")).payload
        assert result["status"] == "succeeded"
        assert result["recognition"]["sourceCount"] == 40
        assert complete.await_count == 3


async def test_http_document_workflow_and_download_authorization(studio_db):
    project_id = await initial_project(studio_db)
    app = FastAPI()
    app.include_router(router)
    user = SimpleNamespace(id=7)

    async def database():
        async with studio_db() as db:
            yield db

    async def current_user():
        return user

    @app.exception_handler(AppError)
    async def error_handler(request, exc):
        return JSONResponse({"code": exc.code, "msg": exc.msg}, status_code=exc.status_code)

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = current_user
    base = f"/kiln-studio/projects/{project_id}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        detail = (await client.get(base)).json()["data"]
        file = detail["project"]["files"][0]
        response = await client.get(f"{base}/files/{file['id']}/download")
        assert response.status_code == 200 and "热惯性" in response.content.decode()
        user.id = 8
        assert (await client.get(f"{base}/files/{file['id']}/download")).status_code == 404
        user.id = 7
        assert (
            await client.post(f"{base}/knowledge/recognize", json={"mode": "invalid"})
        ).status_code == 422
        response = await client.post(f"{base}/knowledge/recognize", json={"mode": "rules"})
        assert response.status_code == 200
        job_id = response.json()["data"]["id"]
        assert (await client.get(f"/kiln-studio/assets/{job_id}")).json()["data"][
            "status"
        ] == "succeeded"
        response = await client.post(
            f"{base}/files",
            files=[("files", ("new.txt", "窑压：压力与气量有关".encode(), "text/plain"))],
        )
        assert response.status_code == 200
        detail = (await client.get(base)).json()["data"]
        assert len(detail["project"]["files"]) == 2
        new_file = next(item for item in detail["project"]["files"] if item["path"] == "new.txt")
        assert (await client.post(f"{base}/files/{new_file['id']}/reparse")).status_code == 200
        assert (await client.delete(f"{base}/files/{new_file['id']}")).status_code == 200
        assert (await client.get(f"{base}/files/{new_file['id']}/download")).status_code == 404
