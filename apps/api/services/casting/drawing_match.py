"""图纸识参：多模态抽参 + 按尺寸/规格/方案模糊匹配物料（Top5 + 相似度）。"""

from __future__ import annotations

import base64
import json
import math
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.services.casting import yield_analysis as yield_svc
from api.services.models.runtime import build_llm_client, build_llm_client_by_id
from common.errors import AppError, ErrorCode
from common.logging import get_logger

logger = get_logger(__name__)

_EXTRACT_PROMPT = """你是铸造产成品/砂型图纸识参助手。请从附图中提取可用于匹配 MES 物料档案的结构化参数。
只输出一个 JSON 对象（不要 Markdown 代码围栏），字段如下：
{
  "dims": [{"label": "H|A|B|W|L|其它", "value": 数字}],
  "diameters": [数字],
  "specHints": ["可能的规格型号片段"],
  "schemeText": "图上方案/说明全文或摘要",
  "keywords": ["品名或方案关键词，如方案二、砂型、池壁"],
  "confidence": 0.0到1.0
}
规则：
- dims.value 必须是图上标注的尺寸数字（毫米等），不要臆造。
- 若某字段无法识别则用空数组/空字符串。
- keywords 尽量短（2～8 字），便于数据库模糊匹配。
"""

_DIM_TOLERANCE = 2.0
_WIDE_RECALL_TOP = 200
_TOP_CANDIDATES = 5
_W_SIZE = 0.70
_W_SPEC = 0.18
_W_SCHEME = 0.12


def _strip_json_fence(text: str) -> str:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def normalize_extracted(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    dims_out: list[dict[str, Any]] = []
    for item in raw.get("dims") or []:
        if not isinstance(item, dict):
            continue
        val = _as_float(item.get("value"))
        if val is None:
            continue
        label = str(item.get("label") or "").strip() or "?"
        dims_out.append({"label": label, "value": val})
    diameters: list[float] = []
    for d in raw.get("diameters") or []:
        fv = _as_float(d)
        if fv is not None:
            diameters.append(fv)
    return {
        "dims": dims_out,
        "diameters": diameters,
        "specHints": [str(x).strip() for x in (raw.get("specHints") or []) if str(x).strip()],
        "schemeText": str(raw.get("schemeText") or "").strip(),
        "keywords": [str(x).strip() for x in (raw.get("keywords") or []) if str(x).strip()],
        "confidence": _as_float(raw.get("confidence")),
    }


def _extracted_dim_values(extracted: dict[str, Any]) -> list[float]:
    vals: list[float] = []
    for d in extracted.get("dims") or []:
        v = _as_float(d.get("value") if isinstance(d, dict) else None)
        if v is not None and v > 0:
            vals.append(v)
    for d in extracted.get("diameters") or []:
        v = _as_float(d)
        if v is not None and v > 0:
            vals.append(v)
    uniq: list[float] = []
    for v in vals:
        if any(abs(v - u) <= _DIM_TOLERANCE for u in uniq):
            continue
        uniq.append(v)
    return uniq


def _match_tokens(extracted: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for x in list(extracted.get("specHints") or []) + list(extracted.get("keywords") or []):
        t = str(x).strip()
        if len(t) >= 2:
            tokens.append(t)
    for m in re.finditer(r"方案[一二三四五六七八九十\d]+", str(extracted.get("schemeText") or "")):
        tokens.append(m.group(0))
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out[:12]


def sql_inventory_drawing_recall(
    *,
    dim_values: list[float],
    tokens: list[str],
    top: int = _WIDE_RECALL_TOP,
) -> str:
    n = max(1, min(int(top), 300))
    clauses: list[str] = []
    for v in dim_values[:12]:
        vv = float(v)
        for col in (
            "p.SizeA",
            "p.SizeB",
            "p.SizeH",
            "i.InventorySizeLength",
            "i.InventorySizeWidth",
            "i.InventorySizeHeight",
        ):
            clauses.append(f"(ABS(ISNULL({col}, -999999) - {vv}) <= {_DIM_TOLERANCE})")
    for tok in tokens[:8]:
        g = yield_svc._sql_escape(tok)  # noqa: SLF001
        if not g:
            continue
        clauses.append(f"i.InventoryName LIKE N'%{g}%'")
        clauses.append(f"i.InventorySpecification LIKE N'%{g}%'")
        clauses.append(f"i.InventoryCode LIKE N'%{g}%'")
    where = (
        " OR ".join(clauses)
        if clauses
        else "p.SizeA IS NOT NULL OR p.SizeB IS NOT NULL OR p.SizeH IS NOT NULL"
    )
    return f"""
SELECT TOP {n}
  i.InventoryGUID, i.InventoryCode, i.InventoryName, i.InventorySpecification,
  i.InventorySizeLength, i.InventorySizeWidth, i.InventorySizeHeight,
  p.SizeA, p.SizeB, p.SizeH, p.Volume
FROM invn.Inventory i
LEFT JOIN invn.Inventory_Product p ON p.InventoryGUID = i.InventoryGUID
WHERE {where}
ORDER BY i.InventoryCode
""".strip()


def _product_size_text(row: dict[str, Any]) -> str | None:
    a = _as_float(yield_svc._row_get(row, "SizeA", "sizeA"))  # noqa: SLF001
    b = _as_float(yield_svc._row_get(row, "SizeB", "sizeB"))  # noqa: SLF001
    h = _as_float(yield_svc._row_get(row, "SizeH", "sizeH"))  # noqa: SLF001
    parts = [str(int(x)) if x == int(x) else str(x) for x in (a, b, h) if x is not None]
    return "×".join(parts) if parts else None


def _row_product_ab_h(row: dict[str, Any]) -> list[tuple[str, float]]:
    """成品档案主尺寸 SizeA/B/H（对应 IMES 列 A/B/H）。"""
    out: list[tuple[str, float]] = []
    for label, key in (("A", "SizeA"), ("B", "SizeB"), ("H", "SizeH")):
        v = _as_float(yield_svc._row_get(row, key))  # noqa: SLF001
        if v is not None and v > 0:
            out.append((label, v))
    return out


def _row_size_values(row: dict[str, Any]) -> list[float]:
    vals = [v for _, v in _row_product_ab_h(row)]
    for k in (
        "InventorySizeLength",
        "InventorySizeWidth",
        "InventorySizeHeight",
    ):
        v = _as_float(yield_svc._row_get(row, k))  # noqa: SLF001
        if v is not None and v > 0:
            vals.append(v)
    return vals


def _body_dim_values(extracted: dict[str, Any]) -> list[float]:
    """用于成品匹配的尺寸：排除孔径 diameters（方案图 Φ50 等不应拉低相似度）。"""
    diameters = {
        round(v, 3)
        for v in (_as_float(d) for d in (extracted.get("diameters") or []))
        if v is not None and v > 0
    }
    body: list[float] = []
    for d in extracted.get("dims") or []:
        if not isinstance(d, dict):
            continue
        v = _as_float(d.get("value"))
        if v is None or v <= 0:
            continue
        if round(v, 3) in diameters:
            continue
        if any(abs(v - u) <= _DIM_TOLERANCE for u in body):
            continue
        body.append(v)
    return body


def _size_score(
    extracted: dict[str, Any],
    row: dict[str, Any],
) -> tuple[float, list[str]]:
    """以成品 A/B/H 覆盖率为主打分，避免方案图多余标注（如 800、Φ50）拉低分。

    例：图上 800/1400/220/Φ50，成品 1400×420×220 → 命中 H、B 共 2/3，
    旧算法按「图上数字命中率」会把 800、50 算进分母得到约 27.5%。
    """
    product = _row_product_ab_h(row)
    if not product:
        # 无成品尺寸时退回：图上数字命中任意库存尺寸字段
        body = _body_dim_values(extracted)
        row_dims = _row_size_values(row)
        if not body or not row_dims:
            return 0.0, []
        hits = 0
        reasons: list[str] = []
        for ev in body:
            for rv in row_dims:
                if abs(ev - rv) <= _DIM_TOLERANCE:
                    hits += 1
                    reasons.append(f"尺寸 {ev:g}≈{rv:g}")
                    break
        return hits / len(body), reasons

    body = _body_dim_values(extracted)
    if not body:
        return 0.0, []

    reasons: list[str] = []
    hits = 0
    for label, rv in product:
        if any(abs(ev - rv) <= _DIM_TOLERANCE for ev in body):
            hits += 1
            reasons.append(f"成品{label}={rv:g} 已在图中")
    coverage = hits / len(product)
    # 命中 2 项及以上视为强相关（方案图常缺 A 或某一边）
    if hits >= 2 and len(product) >= 2:
        coverage = max(coverage, 0.9)
    if hits == len(product):
        coverage = 1.0
    return coverage, reasons


def _text_score(tokens: list[str], haystack: str) -> tuple[float, list[str]]:
    if not tokens:
        return 0.0, []
    text = (haystack or "").lower()
    if not text:
        return 0.0, []
    reasons: list[str] = []
    hits = 0
    for tok in tokens:
        if tok.lower() in text:
            hits += 1
            reasons.append(f"含「{tok}」")
    return hits / len(tokens), reasons


def score_inventory_row(row: dict[str, Any], *, extracted: dict[str, Any]) -> dict[str, Any]:
    dim_vals = _extracted_dim_values(extracted)
    tokens = _match_tokens(extracted)
    name = str(yield_svc._row_get(row, "InventoryName", "name") or "")  # noqa: SLF001
    spec = str(yield_svc._row_get(row, "InventorySpecification", "spec") or "")  # noqa: SLF001
    code = str(yield_svc._row_get(row, "InventoryCode", "code") or "")  # noqa: SLF001
    size_s, size_reasons = _size_score(extracted, row)
    spec_s, spec_reasons = _text_score(tokens, f"{spec} {code}")
    scheme_s, scheme_reasons = _text_score(tokens, f"{name} {spec}")
    w_size, w_spec, w_scheme = _W_SIZE, _W_SPEC, _W_SCHEME
    if not dim_vals and not _body_dim_values(extracted):
        w_size = 0.0
        w_spec, w_scheme = 0.45, 0.55
    elif not tokens:
        w_size = 1.0
        w_spec = w_scheme = 0.0
    similarity = 100.0 * (w_size * size_s + w_spec * spec_s + w_scheme * scheme_s)
    reasons: list[str] = []
    seen: set[str] = set()
    for r in size_reasons + spec_reasons + scheme_reasons:
        if r not in seen:
            seen.add(r)
            reasons.append(r)
    guid = str(
        yield_svc._row_get(row, "InventoryGUID", "inventoryGuid", "GUID") or ""  # noqa: SLF001
    )
    return {
        "inventoryGuid": guid,
        "code": code or None,
        "name": name or None,
        "spec": spec or None,
        "productSize": _product_size_text(row),
        "sizeA": _as_float(yield_svc._row_get(row, "SizeA")),  # noqa: SLF001
        "sizeB": _as_float(yield_svc._row_get(row, "SizeB")),  # noqa: SLF001
        "sizeH": _as_float(yield_svc._row_get(row, "SizeH")),  # noqa: SLF001
        "similarity": round(similarity, 1),
        "matchReasons": reasons[:8],
    }


async def extract_drawing_params(
    db: AsyncSession,
    *,
    image_bytes: bytes,
    content_type: str = "image/png",
    mode: str = "fast",
    vision_model_id: str | None = None,
) -> dict[str, Any]:
    if not image_bytes:
        raise AppError(ErrorCode.VALIDATION, "图片为空", status_code=422)
    if len(image_bytes) > 12 * 1024 * 1024:
        raise AppError(ErrorCode.VALIDATION, "图片过大（上限 12MB）", status_code=422)

    mime = (content_type or "image/png").split(";")[0].strip().lower()
    if mime not in ("image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"):
        mime = "image/png"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    if (vision_model_id or "").strip():
        client = await build_llm_client_by_id(
            db, vision_model_id.strip(), prefer_model_type="multimodal_vision"
        )
    else:
        # 未指定时优先启用中的多模态视觉，否则回退快速问答模型
        from api.services.models import configs as model_configs

        vision = await model_configs.resolve_first_enabled_llm(
            db, model_type="multimodal_vision"
        )
        if vision is not None:
            client = await build_llm_client_by_id(db, vision.public_id)
        else:
            client = await build_llm_client(db, mode)

    result = await client.complete_message(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _EXTRACT_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        mode=mode,
    )
    content = str(result.get("content") or "").strip()
    if not content:
        raise AppError(ErrorCode.INTERNAL, "多模态模型未返回识参结果", status_code=502)
    try:
        parsed = json.loads(_strip_json_fence(content))
    except json.JSONDecodeError as exc:
        logger.warning("drawing extract JSON parse failed: %s", content[:400])
        raise AppError(
            ErrorCode.INTERNAL, f"识参结果不是合法 JSON：{exc}", status_code=502
        ) from exc
    if not isinstance(parsed, dict):
        raise AppError(ErrorCode.INTERNAL, "识参结果格式错误", status_code=502)
    return normalize_extracted(parsed)


async def match_inventories_by_extracted(
    db: AsyncSession,
    *,
    extracted: dict[str, Any],
    top: int = _TOP_CANDIDATES,
) -> list[dict[str, Any]]:
    import asyncio

    from api.services.mcp.isolated_stdio import (
        run_execute_queries_isolated,
        snapshot_mcp_server,
    )

    norm = normalize_extracted(extracted)
    dim_vals = _extracted_dim_values(norm)
    tokens = _match_tokens(norm)
    if not dim_vals and not tokens:
        return []

    server = await yield_svc.find_mssql_server(db)
    snap = snapshot_mcp_server(server)
    sql = sql_inventory_drawing_recall(dim_values=dim_vals, tokens=tokens)
    try:
        batch = await asyncio.to_thread(
            run_execute_queries_isolated,
            server_snapshot=snap,
            steps=[("recall", sql)],
            timeout_seconds=90.0,
        )
    except AppError:
        raise
    except Exception as e:
        logger.exception("casting.drawing_match recall failed")
        raise AppError(
            ErrorCode.INTERNAL, f"物料匹配查询失败: {e}", status_code=500
        ) from e

    block = batch.get("recall") or {}
    rows = block.get("rows")
    if not isinstance(rows, list):
        rows = yield_svc._parse_mcp_rows(block.get("content") or block.get("raw"))  # noqa: SLF001

    scored: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = score_inventory_row(row, extracted=norm)
        guid = item.get("inventoryGuid") or ""
        if not guid or guid in seen or item["similarity"] <= 0:
            continue
        seen.add(guid)
        scored.append(item)
    scored.sort(key=lambda x: (-float(x["similarity"]), str(x.get("code") or "")))
    return scored[: max(1, min(int(top), 20))]


async def match_drawing(
    db: AsyncSession,
    *,
    image_bytes: bytes | None = None,
    content_type: str = "image/png",
    extracted: dict[str, Any] | None = None,
    mode: str = "fast",
    top: int = _TOP_CANDIDATES,
    vision_model_id: str | None = None,
) -> dict[str, Any]:
    warnings: list[str] = []
    vision_used: dict[str, Any] | None = None
    if extracted is not None and image_bytes is None:
        norm = normalize_extracted(extracted)
    elif image_bytes is not None:
        try:
            norm = await extract_drawing_params(
                db,
                image_bytes=image_bytes,
                content_type=content_type,
                mode=mode,
                vision_model_id=vision_model_id,
            )
        except AppError:
            raise
        except Exception as e:
            logger.exception("casting.drawing_match extract failed")
            raise AppError(
                ErrorCode.INTERNAL, f"图纸识参失败: {e}", status_code=502
            ) from e
        if extracted:
            hand = normalize_extracted(extracted)
            for key in ("dims", "diameters", "specHints", "schemeText", "keywords"):
                if hand.get(key):
                    norm[key] = hand[key]
        if (vision_model_id or "").strip():
            vision_used = {"id": vision_model_id.strip()}
    else:
        raise AppError(
            ErrorCode.VALIDATION,
            "请上传图纸图片，或传入已抽取的 extracted 参数",
            status_code=422,
        )

    candidates = await match_inventories_by_extracted(db, extracted=norm, top=top)
    if not candidates:
        warnings.append("未匹配到相似物料，可调整抽参后重新匹配")

    return {
        "extracted": norm,
        "candidates": candidates,
        "candidateCount": len(candidates),
        "warnings": warnings,
        "visionModel": vision_used,
        "message": (
            f"已匹配 {len(candidates)} 个候选物料，请选择后继续分析"
            if candidates
            else "未匹配到候选物料"
        ),
    }
