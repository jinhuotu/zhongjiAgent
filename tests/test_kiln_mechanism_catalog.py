"""Catalog workflow against the studio's persisted asset model."""

from contextlib import asynccontextmanager
from io import BytesIO

import pytest
from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from api.services import kiln_studio as studio
from common.errors import AppError
from common.kiln_studio_schema import (
    AlgorithmPresetRequest,
    CatalogRevisionRequest,
    SaveMechanismRequest,
)


class TestSession:
    __test__ = False

    def __init__(self, session):
        self.session = session

    def add(self, row):
        self.session.add(row)

    async def execute(self, query):
        return self.session.execute(query)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    @asynccontextmanager
    async def begin_nested(self):
        with self.session.begin_nested():
            yield


@pytest.fixture
def studio_db(monkeypatch, tmp_path):
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE kiln_studio_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id VARCHAR(32) UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                project_id VARCHAR(32) NOT NULL,
                kind VARCHAR(24) NOT NULL,
                title VARCHAR(180) NOT NULL,
                payload JSON NOT NULL,
                created_at DATETIME
            )
        """)

    @asynccontextmanager
    async def session():
        with Session(engine, expire_on_commit=False) as sync_session:
            yield TestSession(sync_session)

    monkeypatch.setattr(studio, "AsyncSessionLocal", session)
    monkeypatch.setattr(studio, "_root", lambda: tmp_path)
    yield session
    engine.dispose()


def revision(item, **changes):
    fields = {key: item[key] for key in CatalogRevisionRequest.model_fields}
    return CatalogRevisionRequest.model_validate({**fields, **changes})


async def upload_manual(db, text):
    return await studio.upload_project(
        db,
        7,
        [UploadFile(BytesIO(text.encode()), filename="manual.txt")],
        "现场窑炉资料",
    )


async def test_algorithm_presets_are_versioned_owned_and_filterable(studio_db):
    async with studio_db() as db:
        first = await studio.save_algorithm_preset(
            db,
            7,
            AlgorithmPresetRequest(
                name="岭回归低正则", baseKey="ridge", tuning={"ridgeAlpha": 0.1}
            ),
        )
        next_version = await studio.save_algorithm_preset(
            db,
            7,
            AlgorithmPresetRequest(
                name="岭回归高正则",
                baseKey="ridge",
                tuning={"ridgeAlpha": 10},
                parentKey=first["key"],
            ),
        )
        keys = [next_version["key"], "baseline", first["key"]]
        selected = await studio.selected_algorithm_definitions(db, 7, keys)
        assert [item.key for item in selected] == keys
        assert selected[0].tuning.ridgeAlpha == 10
        assert selected[2].tuning.ridgeAlpha == 0.1
        assert first["key"] not in {
            item.key for item in await studio.selected_algorithm_definitions(db, 7, None)
        }
        assert first["key"] not in {
            item["key"] for item in await studio.list_algorithm_catalog(db, 8)
        }
        with pytest.raises(AppError, match="无权访问"):
            await studio.selected_algorithm_definitions(db, 8, [first["key"]])
        await studio.set_algorithm_availability(db, 7, first["key"], False)
        with pytest.raises(AppError, match="不可加入新回测"):
            await studio.selected_algorithm_definitions(db, 7, [first["key"]])
        history = await studio.selected_algorithm_definitions(
            db, 7, [first["key"]], historical=True
        )
        assert history[0].tuning.ridgeAlpha == 0.1


async def test_saved_plan_exposes_and_retains_a_single_reusable_mechanism(studio_db):
    source = {
        "id": "manual:1",
        "file": "窑炉手册.pdf",
        "fileId": "manual",
        "location": "第 8 页",
        "text": "闭环热惯性时间常数 43 分钟。",
    }
    config = {"zoneCount": 6, "burnerCount": 6, "kilnCode": "K01", "flowBasis": "normal"}
    dataset_data = {
        "config": config,
        "sourceKind": "uploaded",
        "summary": [{"fields": [{"key": key, "present": 10} for key in ("temp", "temp_sp")]}],
    }
    async with studio_db() as db:
        old = await studio.create_asset(
            db, 7, "", "project", "历史项目", {"files": [{"sources": [source]}]}
        )
        old_data = await studio.create_asset(
            db, 7, old.public_id, "dataset", "老数据", dataset_data
        )
        saved = await studio.save_mechanism(
            db,
            7,
            old.public_id,
            SaveMechanismRequest(
                datasetId=old_data.public_id,
                title="老窑炉方案",
                templateKeys=["thermal"],
                parameters={"tauMinutes": 43},
                provenance={"tauMinutes": source["id"]},
                notes="核对自动温控工况",
                acceptAssumptions=True,
            ),
        )
        new = await studio.create_asset(db, 7, "", "project", "本次项目", {"files": []})
        current = await studio.create_asset(db, 7, new.public_id, "dataset", "新数据", dataset_data)
        await db.commit()
        items = (await studio.mechanism_catalog(db, 7, current.public_id))["items"]
        history = next(
            item for item in items if item.get("sourcePlan", {}).get("id") == saved["id"]
        )
        assert history["projected"]
        assert history["parameterValues"] == {"tauMinutes": 43}
        assert history["parameterSources"] == {"tauMinutes": "窑炉手册.pdf · 第 8 页"}
        assert history["evidence"] == [source]
        assert history["solverSupported"] and not history["compatibilityIssues"]
        assert all(
            "sourcePlan" not in item for item in (await studio.mechanism_catalog(db, 8))["items"]
        )

        replaced = await studio.materialize_history_catalog(
            db, 7, saved["id"], "thermal", current.public_id
        )
        assert replaced["id"] == history["id"] and not replaced.get("projected")
        again = await studio.materialize_history_catalog(db, 7, saved["id"], "thermal")
        assert again["id"] == replaced["id"]
        with pytest.raises(AppError, match="无权访问"):
            await studio.materialize_history_catalog(db, 8, saved["id"], "thermal")
        new_plan = await studio.save_mechanism(
            db,
            7,
            new.public_id,
            SaveMechanismRequest(
                datasetId=current.public_id,
                title="本次项目替换后",
                templateKeys=["thermal", "energy"],
                catalogSelections={"thermal": replaced["id"]},
                parameters={"tauMinutes": 43, "capacityKJ": 25000},
                provenance={"tauMinutes": f"catalog:{replaced['id']}:tauMinutes"},
                acceptAssumptions=True,
            ),
        )
        assert new_plan["catalogSelections"] == {"thermal": replaced["id"]}
        assert new_plan["parameters"]["capacityKJ"] == 25000
        assert new_plan["provenance"]["tauMinutes"].endswith(":tauMinutes")
        original = await studio.get_asset(db, 7, saved["id"], "mechanism")
        assert original.payload["catalogSelections"] == {}


async def test_catalog_import_review_reuse_and_immutable_references(studio_db):
    text = (
        "闭环热惯性：时间常数 45 分钟。\n"
        "dT/dt = (T_sp-T)/τ\n"
        "换热回流系数: R_mix = m_return / m_total\n"
        "机理：排烟再循环：回流影响炉内气氛的稳定程度，需分阶段确认。"
    )
    async with studio_db() as db:
        project_id, job = await upload_manual(db, text)
    await studio.execute_job(job["id"], 7)
    async with studio_db() as db:
        parsed_job = await studio.get_asset(db, 7, job["id"], "job")
        assert parsed_job.payload["status"] == "succeeded"
        items = (await studio.mechanism_catalog(db, 7))["items"]
        pending = [item for item in items if item["status"] == "pending"]
        thermal = next(item for item in pending if item["key"] == "thermal")
        custom = next(item for item in pending if item["name"] == "换热回流系数")
        assert any(item["name"] == "排烟再循环" for item in pending)
        assert thermal["solverSupported"]
        assert thermal["parameterValues"] == {"tauMinutes": 45}
        assert thermal["parseJobId"] == job["id"]
        assert thermal["evidence"][0]["file"] == "manual.txt"

        retry_project, retry = await upload_manual(db, text)
        assert retry_project != project_id
        reparsed = await studio.reparse_project(db, 7, project_id)
    await studio.execute_job(retry["id"], 7)
    await studio.execute_job(reparsed["id"], 7)
    async with studio_db() as db:
        items = (await studio.mechanism_catalog(db, 7))["items"]
        assert {item["id"] for item in items if item["status"] == "pending"} == {
            item["id"] for item in pending
        }
        assert all(
            item["status"] == "builtin" for item in (await studio.mechanism_catalog(db, 8))["items"]
        )
        with pytest.raises(AppError, match="资源不存在或无权访问"):
            await studio.revise_catalog(db, 8, thermal["id"], revision(thermal))

        approved = await studio.revise_catalog(
            db, 7, thermal["id"], revision(thermal, check=thermal["check"] + " 停火后单独核验。")
        )
        assert approved["evidence"] == thermal["evidence"]
        assert approved["parentId"] == thermal["id"]
        assert approved["status"] == "approved"
        reference = await studio.revise_catalog(db, 7, custom["id"], revision(custom))
        dataset = await studio.create_asset(
            db,
            7,
            project_id,
            "dataset",
            "现场数据",
            {
                "config": {
                    "zoneCount": 6,
                    "burnerCount": 6,
                    "kilnCode": "K01",
                    "flowBasis": "normal",
                },
                "summary": [{"fields": [{"key": k, "present": 5} for k in ("z1_temp", "temp_sp")]}],
                "sourceKind": "uploaded",
            },
        )
        await db.commit()
        with pytest.raises(AppError, match="尚未审核"):
            await studio.save_mechanism(
                db,
                7,
                project_id,
                SaveMechanismRequest(
                    datasetId=dataset.public_id,
                    title="未审核",
                    templateKeys=["thermal"],
                    catalogSelections={"thermal": thermal["id"]},
                    acceptAssumptions=True,
                ),
            )
        scoped = await studio.revise_catalog(
            db, 7, approved["id"], revision(approved, scope={"kilnCode": "K02"})
        )
        altered = await studio.revise_catalog(
            db, 7, approved["id"], revision(approved, formula="dT/dt = k * (T_sp - T)")
        )
        for version, error in ((scoped, "窑炉编号"), (altered, "求解器不兼容")):
            with pytest.raises(AppError, match=error):
                await studio.save_mechanism(
                    db,
                    7,
                    project_id,
                    SaveMechanismRequest(
                        datasetId=dataset.public_id,
                        title="不适用",
                        templateKeys=["thermal"],
                        catalogSelections={"thermal": version["id"]},
                        acceptAssumptions=True,
                    ),
                )
        saved = await studio.save_mechanism(
            db,
            7,
            project_id,
            SaveMechanismRequest(
                datasetId=dataset.public_id,
                title="当前机理",
                templateKeys=["thermal"],
                catalogSelections={"thermal": approved["id"], reference["key"]: reference["id"]},
                catalogAlternatives=[altered["id"]],
                parameters={"tauMinutes": 45},
                provenance={"tauMinutes": f"catalog:{approved['id']}:tauMinutes"},
                acceptAssumptions=True,
            ),
        )
        assert saved["provenance"]["tauMinutes"] == f"catalog:{approved['id']}:tauMinutes"
        assert saved["catalogAlternatives"] == [altered["id"]]
        resaved = await studio.revise_catalog(
            db, 7, approved["id"], revision(approved, name="闭环温度热惯性（修订）")
        )
        assert resaved["id"] != approved["id"]
        after = await studio.get_asset(db, 7, saved["id"], "mechanism")
        assert after.payload["catalogSelections"]["thermal"] == approved["id"]

        items = {
            item["id"]: item
            for item in (await studio.mechanism_catalog(db, 7, dataset.public_id))["items"]
        }
        assert items[thermal["id"]]["status"] == "reviewed"
        assert not items[approved["id"]]["solverIssues"]
        assert items[scoped["id"]]["compatibilityIssues"]
