"""Real archive parsing, causal holdouts, persisted algorithm and scenario checks."""

import base64
import io
import json
import zipfile
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI, UploadFile
from httpx import ASGITransport, AsyncClient
from test_kiln_mechanism_catalog import studio_db  # noqa: F401

from api.deps import get_current_user, get_db
from api.routers.kiln_studio import router as studio_router
from api.services import kiln_studio as studio
from common.errors import AppError
from common.kiln_studio_engine import (
    algorithm_library,
    compare_mechanisms,
    cumulative_volumes,
    examples,
    predict_model,
    run_experiment,
    simulate,
    split_examples,
)
from common.kiln_studio_files import (
    ArchiveBudget,
    demo_archive,
    infer_mapping,
    normalize_table,
    parse_file,
    parse_timestamp,
    prepare_cycle,
    safe_members,
)
from common.kiln_studio_schema import (
    AlgorithmTuning,
    ExperimentRequest,
    ExternalAlgorithmImport,
    PhysicsParameters,
)


@pytest.fixture(scope="module")
def parsed():
    return [parse_file(name, blob) for name, blob in safe_members("demo.zip", demo_archive())]


@pytest.fixture(scope="module")
def cycles(parsed):
    return [
        {
            "id": manifest["id"],
            "points": prepare_cycle(normalize_table(data, manifest["mapping"])[0], manifest["id"])[
                0
            ],
        }
        for manifest, data in parsed
        if manifest["kind"] == "table"
    ]


def config(task="temperature", **kwargs):
    return ExperimentRequest(
        datasetId="dataset", mechanismId="mechanism", task=task, boostingRounds=8, **kwargs
    )


async def test_external_onnx_import_runs_and_keeps_artifact_for_prediction(request, cycles):
    # ONNX opset 13: y = x + 2, dynamic batch, one float32 feature.
    blob = base64.b64decode(
        "CAgSEGtpbG4tc3R1ZGlvLXRlc3Q6YgoRCgF4CgRiaWFzEgF5IgNBZGQSD2RlbW9fcmVncmVzc2lvbioQCAEQ"
        "AUIEYmlhc0oEAAAAQFoUCgF4Eg8KDQgBEgkKAxIBTgoCCAFiFAoBeRIPCg0IARIJCgMSAU4KAggBQgIQDQ=="
    )
    spec = ExternalAlgorithmImport(
        name="外部温度模型",
        task="temperature",
        horizonMinutes=30,
        historyMinutes=30,
        trainedUntil="2000-01-01T00:00:00+08:00",
        featureIndices=[0],
        probe=[25],
    )
    catalog_session = request.getfixturevalue("studio_db")
    async with catalog_session() as db:
        with pytest.raises(AppError, match="ONNX"):
            await studio.import_external_algorithm(
                db, 7, UploadFile(io.BytesIO(b"wrong format"), filename="fake.onnx"), spec
            )
        app = FastAPI()
        app.include_router(studio_router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=7)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/kiln-studio/algorithm-library/external",
                data={"spec": spec.model_dump_json()},
                files={"file": ("external.onnx", blob, "application/octet-stream")},
            )
        assert response.status_code == 200
        imported = response.json()["data"]
        assert imported["external"]["probeOutput"] == 27
        assert imported["external"]["inputName"] == "x"
        assert imported["external"]["outputName"] == "y"
        json.dumps(imported)
        keys = ["baseline", imported["key"]]
        req = config(selectedAlgorithmKeys=keys)
        definitions = await studio.selected_algorithm_definitions(db, 7, keys)
        models = studio._external_model_contents(7, definitions, req)
        result = run_experiment(
            cycles, req, PhysicsParameters().model_dump(), ["thermal"], definitions, models
        )
        key = imported["key"]
        assert [card["key"] for card in result["algorithms"]] == keys
        assert result["algorithms"][1]["validation"]["coverage"] == 1
        for point in result["points"]:
            assert point[key] == pytest.approx(point["baseline"] + 2, abs=0.001)
        saved = json.loads(json.dumps(result["artifacts"], allow_nan=False))
        assert predict_model(saved[key], {"x": [41]}) == 43
        late = definitions[-1].external.model_copy(
            update={"trainedUntil": spec.trainedUntil.replace(year=2099)}
        )
        blocked = run_experiment(
            cycles,
            req,
            PhysicsParameters().model_dump(),
            ["thermal"],
            [definitions[0], definitions[-1].model_copy(update={"external": late})],
            models,
        )
        assert "重叠" in blocked["algorithms"][1]["note"]
        assert key not in blocked["artifacts"]


def test_selected_algorithms_and_two_versions_use_the_same_backtest_rows(cycles):
    library = {item.key: item for item in algorithm_library()}
    definitions = [library["baseline"]]
    for key, alpha in (("custom_ridge_low", 0.1), ("custom_ridge_high", 10.0)):
        definitions.append(
            library["ridge"].model_copy(
                update={
                    "key": key,
                    "baseKey": "ridge",
                    "version": key,
                    "tuning": AlgorithmTuning(ridgeAlpha=alpha),
                }
            )
        )
    keys = [item.key for item in definitions]
    req = config(selectedAlgorithmKeys=keys)
    parameters = PhysicsParameters().model_dump()
    result = run_experiment(cycles, req, parameters, ["thermal"], definitions)
    assert [item["key"] for item in result["algorithms"]] == keys
    assert all(item["validation"]["coverage"] == 1 for item in result["algorithms"])
    assert set(result["artifacts"]) == set(keys)
    low, high = (result["artifacts"][key] for key in keys[1:])
    assert low["config"] == high["config"] == result["config"]
    assert low["model"] != high["model"]
    assert low["implementationKey"] == high["implementationKey"] == "ridge"
    assert low["tuning"] == {"ridgeAlpha": 0.1}
    assert high["tuning"] == {"ridgeAlpha": 10.0}
    assert any(point[keys[1]] != point[keys[2]] for point in result["points"])
    builtin = run_experiment(
        cycles, config(selectedAlgorithmKeys=["lasso"]), parameters, ["thermal"]
    )
    assert [item["key"] for item in builtin["algorithms"]] == ["lasso"]


def test_mechanism_comparison_holds_out_whole_kilns_and_ranks_shared_points(cycles):
    named = [
        {**cycle, "name": f"烧成.zip!/数据_窑次_{batch}/day-{i}.xls"}
        for i, (cycle, batch) in enumerate(zip(cycles, [1, 1, 2, 2, 3], strict=True))
    ]
    params = PhysicsParameters().model_dump()
    result = compare_mechanisms(
        named,
        config(),
        [
            {
                "id": item_id,
                "title": item_id,
                "version": item_id,
                "parameters": {**params, "tauMinutes": tau},
                "templateKeys": keys,
            }
            for item_id, tau, keys in [
                ("current", 10, ["thermal"]),
                ("history", 60, ["thermal"]),
                ("unavailable", 10, ["energy"]),
            ]
        ],
    )
    assert result["splitMethod"].startswith("按资料目录识别的完整窑次")
    assert {point["fold"] for point in result["points"]} == {cycle["id"] for cycle in named[2:4]}
    current, history, unavailable = result["scores"]
    assert current["metric"]["count"] == history["metric"]["count"] == result["count"]
    assert unavailable["metric"] is None
    assert result["bestIds"] == [
        min([current, history], key=lambda row: row["metric"]["mae"])["id"]
    ]


def test_mechanism_comparison_uses_a_common_air_fuel_threshold(cycles):
    named = [{**cycle, "name": f"batch-{i}.csv"} for i, cycle in enumerate(cycles)]
    params = PhysicsParameters().model_dump()
    result = compare_mechanisms(
        named,
        config("afr"),
        [
            {
                "id": item_id,
                "title": item_id,
                "version": item_id,
                "parameters": {**params, "minGas": floor},
                "templateKeys": ["combustion"],
            }
            for item_id, floor in [("current", 0.5), ("history", 1.5)]
        ],
    )
    assert result["afrFloor"] == 1.5
    assert result["bestIds"] == ["current", "history"]
    assert all(row["metric"]["count"] == result["count"] for row in result["scores"])


def test_archive_parses_numeric_tables_and_text_sources(parsed):
    assert len(parsed) == 6
    assert all(m["status"] == "ready" for m, _ in parsed)
    document = next(m for m, _ in parsed if m["kind"] == "document")
    assert any("合成演示" in s["text"] for s in document["sources"])
    assert all(s["location"].startswith("行 ") for s in document["sources"])


@pytest.mark.parametrize("name", ["../escape.csv", "/escape.csv", "C:/escape.csv"])
def test_archive_rejects_unsafe_paths(name):
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as z:
        z.writestr(name, b"test")
    with pytest.raises(ValueError, match="路径"):
        list(safe_members("a.zip", blob.getvalue()))


def test_relative_paths_and_ids_disambiguate_same_basename():
    a, _ = parse_file("one/data.csv", b"time,gas\n2026-01-01,3")
    b, _ = parse_file("two/data.csv", b"time,gas\n2026-01-01,3")
    assert a["id"] != b["id"]


def zip_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return output.getvalue()


def test_nested_zip_exposes_tables_with_full_source_path():
    from test_kiln_import import workbook

    table = workbook([["时间", "燃气瞬时流量"], ["2026-01-01 08:00:00", 30]])
    inner = zip_bytes([("历史/run.xlsx", table)])
    outer = zip_bytes([("资料/工况.ZIP", inner), ("设备说明.txt", "燃气窑炉")])
    leaves = list(safe_members("outer.zip", outer))
    assert [name for name, _ in leaves] == ["资料/工况.ZIP!/历史/run.xlsx", "设备说明.txt"]
    manifest, data = parse_file(*leaves[0])
    assert manifest["kind"] == "table" and manifest["status"] == "ready"
    assert normalize_table(data, manifest["mapping"])[0][0]["gas"] == 30


def test_same_table_basename_in_two_nested_archives_is_not_overwritten():
    inner = zip_bytes([("data.csv", "time,gas\n2026-01-01,30")])
    leaves = list(safe_members("outer.zip", zip_bytes([("a.zip", inner), ("b.zip", inner)])))
    assert len({name for name, _ in leaves}) == 2
    assert len({parse_file(name, blob)[0]["id"] for name, blob in leaves}) == 2


@pytest.mark.parametrize("path", ["../bad.csv", "/bad.csv", "C:/bad.csv"])
def test_nested_archives_apply_path_checks_at_every_level(path):
    outer = zip_bytes([("inside.zip", zip_bytes([(path, "invalid")]))])
    with pytest.raises(ValueError, match="路径"):
        list(safe_members("outer.zip", outer))


def test_nested_archive_depth_limit():
    from common.kiln_studio_files import MAX_ARCHIVE_DEPTH

    payload = zip_bytes([("data.csv", "time,gas")])
    for _ in range(MAX_ARCHIVE_DEPTH):
        payload = zip_bytes([("inner.zip", payload)])
    assert len(list(safe_members("outer.zip", payload))) == 1
    with pytest.raises(ValueError, match="层"):
        list(safe_members("outer.zip", zip_bytes([("extra.zip", payload)])))


def test_nested_archives_share_file_count_budget(monkeypatch):
    from common import kiln_studio_files as parser

    monkeypatch.setattr(parser, "MAX_ARCHIVE_ENTRIES", 3)
    inner = zip_bytes([("data.csv", "time,gas")])
    with pytest.raises(ValueError, match="合计"):
        list(safe_members("outer.zip", zip_bytes([("a.zip", inner), ("b.zip", inner)])))


def test_nested_archives_share_total_expanded_bytes(monkeypatch):
    from common import kiln_studio_files as parser

    inner = zip_bytes([("a.txt", b"a" * 20), ("b.txt", b"b" * 20)])
    monkeypatch.setattr(parser, "MAX_TOTAL", len(inner) + 39)
    with pytest.raises(ValueError, match="累计"):
        list(safe_members("outer.zip", zip_bytes([("inner.zip", inner)])))


def test_budget_is_shared_across_separate_uploads(monkeypatch):
    from common import kiln_studio_files as parser

    monkeypatch.setattr(parser, "MAX_ARCHIVE_ENTRIES", 1)
    budget = ArchiveBudget()
    list(safe_members("first.csv", b"time,gas", budget=budget))
    with pytest.raises(ValueError, match="累计"):
        list(safe_members("second.csv", b"time,gas", budget=budget))


def test_corrupt_nested_archive_reports_its_path():
    with pytest.raises(ValueError, match="inner.zip"):
        list(safe_members("outer.zip", zip_bytes([("inner.zip", b"not a zip")])))


def test_mapping_conversion_and_duplicate_time_handling():
    data = {
        "sheets": [
            (
                "data",
                [
                    ["时间", "燃气流量"],
                    ["2026-01-01T00:00", 2],
                    ["2026-01-01T00:00", 3],
                    ["bad", 4],
                ],
            )
        ]
    }
    mapping = infer_mapping(data["sheets"])
    mapping[1]["scale"] = 60
    rows, skipped = normalize_table(data, mapping)
    points, duplicates = prepare_cycle(rows, "x")
    assert skipped == 1 and duplicates == 1
    assert points[0]["gas"] == 180
    with pytest.raises(ValueError, match="重复"):
        normalize_table(data, [mapping[0], mapping[0]])


def test_timezone_conversion_is_explicit():
    assert parse_timestamp("2026-01-01T00:00:00Z").isoformat() == "2026-01-01T08:00:00"


def test_train_validation_test_are_strictly_time_ordered(cycles):
    splits, _ = split_examples(examples(cycles, config(), 0.5))
    train, validation, test = splits
    assert max(x["time"] for x in train) < min(x["originTime"] for x in validation)
    assert max(x["time"] for x in validation) < min(x["originTime"] for x in test)
    assert not ({x["cycle"] for x in train} & {x["cycle"] for x in validation})


def test_single_cycle_purges_labels_crossing_split(cycles):
    train, validation, test = split_examples(examples(cycles[-1:], config(), 0.5))[0]
    assert max(x["time"] for x in train) < min(x["originTime"] for x in validation)
    assert max(x["time"] for x in validation) < min(x["originTime"] for x in test)


@pytest.mark.parametrize("task", ["temperature", "energy", "afr"])
def test_algorithms_train_and_saved_artifacts_reproduce_prediction(cycles, task):
    request = config(task)
    result = run_experiment(
        cycles, request, PhysicsParameters().model_dump(), ["energy", "thermal", "combustion"]
    )
    assert all(a["available"] for a in result["algorithms"])
    assert len({round(a["validation"]["mae"], 6) for a in result["algorithms"]}) >= 3
    saved = json.loads(json.dumps(result["artifacts"]))
    ex = split_examples(examples(cycles, request, 0.5))[0][1][0]
    for key, artifact in saved.items():
        assert predict_model(artifact, ex) == pytest.approx(result["points"][0][key])
    assert all(a["validation"]["coverage"] == 1 for a in result["algorithms"])


def test_test_observation_changes_do_not_modify_training_or_validation(cycles):
    req = config()
    first = run_experiment(cycles, req, PhysicsParameters().model_dump(), ["energy", "thermal"])
    changed = deepcopy(cycles)
    for row in changed[-1]["points"]:
        row["temp"] += 300
        row["gas"] += 100
    second = run_experiment(changed, req, PhysicsParameters().model_dump(), ["energy", "thermal"])
    assert first["artifacts"] == second["artifacts"]
    assert [a["validation"] for a in first["algorithms"]] == [
        a["validation"] for a in second["algorithms"]
    ]


def test_history_features_do_not_read_future_measurements(cycles):
    req = config()
    before = examples(cycles, req, 0.5)[0]
    changed = deepcopy(cycles)
    for row in changed[0]["points"]:
        if row["time"] > before["originTime"]:
            row["temp"] += 200
            row["gas"] = 1234
            row["air"] = 12345
    after = next(
        x
        for x in examples(changed, req, 0.5)
        if x["originTime"] == before["originTime"] and x["cycle"] == before["cycle"]
    )
    assert before["x"] == after["x"]
    assert before["y"] != after["y"]


def test_locked_mechanism_parameters_and_unavailable_templates(cycles):
    p = PhysicsParameters(capacityKJ=12345, lossKW=0.123).model_dump()
    result = run_experiment(
        cycles, config("energy", lockedParameters=["capacityKJ", "lossKW"]), p, ["energy"]
    )
    assert result["parameters"]["capacityKJ"] == 12345
    assert result["parameters"]["lossKW"] == 0.123
    unavailable = run_experiment(cycles, config(), p, ["energy"])
    assert not next(a for a in unavailable["algorithms"] if a["key"] == "physics")["available"]
    assert next(a for a in unavailable["algorithms"] if a["key"] == "ridge")["available"]


def test_scene_change_affects_physics_but_does_not_claim_data_model_causality(cycles):
    result = run_experiment(cycles, config("energy"), PhysicsParameters().model_dump(), ["energy"])
    scene = simulate(
        cycles, [result["artifacts"]["physics"], result["artifacts"]["ridge"]], 0.6, 20
    )
    assert scene["outputs"][0]["baseline"] != scene["outputs"][0]["adjusted"]
    assert scene["outputs"][1]["adjusted"] is None
    same = simulate(cycles, [result["artifacts"]["physics"]], 0.6, 0)
    assert same["outputs"][0]["baseline"] == same["outputs"][0]["adjusted"]


def test_new_data_versions_do_not_mutate_cycles(cycles):
    original = deepcopy(cycles)
    run_experiment(cycles, config(), PhysicsParameters().model_dump(), ["thermal"])
    assert cycles == original


def test_missing_plan_disables_mechanism_not_data_algorithms(cycles):
    changed = deepcopy(cycles)
    for cycle in changed:
        for row in cycle["points"]:
            row["temp_sp"] = None
    result = run_experiment(changed, config(), PhysicsParameters().model_dump(), ["thermal"])
    assert next(a for a in result["algorithms"] if a["key"] == "ridge")["available"]
    assert not next(a for a in result["algorithms"] if a["key"] == "fusion")["available"]
    assert np.isfinite(result["algorithms"][1]["validation"]["mae"])


def test_docx_paragraphs_have_traceable_source_anchors():
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as document:
        document.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>设备说明</w:t></w:r></w:p><w:p><w:r><w:t>热平衡：燃气输入与损失</w:t></w:r></w:p></w:body></w:document>""",
        )
    manifest, _ = parse_file("manual.docx", blob.getvalue())
    assert manifest["status"] == "ready"
    assert manifest["sources"][1]["location"] == "段落 2"
    assert manifest["sources"][1]["text"] == "热平衡：燃气输入与损失"


def test_cumulative_volume_does_not_bridge_missing_points_or_cycles():
    rows = [{"cycle": "a", "time": x} for x in (0, 5, 10, 15)]
    values, hours = cumulative_volumes(rows, [12, None, 12, 12])
    assert values[-1] == pytest.approx(1)
    assert hours == pytest.approx(1 / 12)
    rows.append({"cycle": "b", "time": 20})
    assert cumulative_volumes(rows, [12, 12, 12, 12, 100])[0][-1] == pytest.approx(3)


def test_historical_window_is_fully_available_before_forecast(cycles):
    samples = examples(cycles, config(historyMinutes=30), 0.5)
    first_time = min(row["time"] for row in cycles[0]["points"])
    assert samples[0]["originTime"] - first_time >= 30


@pytest.mark.asyncio
async def test_asset_reads_are_scoped_to_current_owner():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api.services.kiln_studio import get_asset
    from common.errors import AppError

    db = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    )
    with pytest.raises(AppError) as failure:
        await get_asset(db, 42, "another-owner-asset", "dataset")
    assert failure.value.status_code == 404
    statement = db.execute.call_args.args[0]
    assert statement.compile().params["user_id_1"] == 42
