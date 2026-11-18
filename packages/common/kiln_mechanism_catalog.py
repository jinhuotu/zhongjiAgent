"""Structured catalog entries and document candidate extraction for kiln studio."""

from __future__ import annotations

import re
import unicodedata

from common.kiln_studio_engine import PARAMETERS, TEMPLATES, content_hash
from common.kiln_studio_schema import MechanismScope, PhysicsParameters

TEMPLATE_BY_KEY = {item["key"]: item for item in TEMPLATES}
PARAMETER_INFO = {key: (label, unit) for key, label, unit, _ in PARAMETERS}
REQUIRED_FIELDS = {
    "thermal": {"temp": "实测温度", "temp_sp": "温度设定"},
    "energy": {"gas": "燃气流量", "temp_sp": "温度设定"},
    "combustion": {"gas": "燃气流量", "air": "助燃风流量"},
}
FORMULA_KEYS = {
    "dt/dt": "thermal",
    "q_gas": "energy",
    "afr": "combustion",
    "afr_ref": "combustion",
    "dn/dt": "pressure",
    "pv": "pressure",
    "q_air": "recovery",
    "v_gas": "carbon",
    "e_gas": "carbon",
    "δt_zones(t)": "uniformity",
    "q_load": "stages",
}
EQUATION = re.compile(
    r"^(?:(?P<label>[^:：=＝]{1,60})[:：]\s*)?"
    r"(?P<lhs>[\wΔδ]+(?:/[\w]+|\([\w]+\))?)\s*[=＝≈]\s*(?P<rhs>.{1,300})$"
)
NAMED_RELATION = re.compile(
    r"^(?:机理|工程关系|物理模型)\s*[:：]\s*(?P<name>[^:：。]{2,60})"
    r"\s*[:：]\s*(?P<description>.{10,600})$"
)


def template_content(key):
    template = TEMPLATE_BY_KEY[key]
    return {
        "name": template["name"],
        "explanation": template["explanation"],
        "formula": template["formula"],
        "variables": template["variables"],
        "parameterNotes": "\n".join(
            f"{PARAMETER_INFO[k][0]} ({PARAMETER_INFO[k][1]})" for k in template["parameters"]
        ),
        "parameterValues": {},
        "check": template["check"],
        "scope": MechanismScope().model_dump(),
    }


def built_in_versions():
    return [
        {
            **template_content(key),
            "id": f"template:{key}",
            "key": key,
            "templateKey": key,
            "status": "builtin",
            "version": "内置",
            "task": template["task"],
            "role": template["role"],
            "solverSupported": bool(template["task"]),
            "evidence": [],
            "validationStatus": "按当前数据验证",
        }
        for key, template in TEMPLATE_BY_KEY.items()
    ]


def canonical_formula(value):
    normalized = unicodedata.normalize("NFKC", value).replace("−", "-")
    return re.sub(r"\s+", "", normalized)


def solver_supported(key, content):
    template = TEMPLATE_BY_KEY.get(key)
    return bool(
        template
        and template["task"]
        and canonical_formula(content["formula"]) == canonical_formula(template["formula"])
        and set(content["variables"]) == set(template["variables"])
    )


def compatibility(version, dataset):
    issues = []
    if not dataset:
        return issues
    scope = version.get("scope", {})
    if version.get("sourceKind") == "synthetic" and dataset.get("sourceKind") != "synthetic":
        issues.append("来源为合成资料，当前为现场数据")
    labels = {
        "kilnCode": "窑炉编号",
        "zoneCount": "温区数",
        "burnerCount": "烧嘴数",
        "flowBasis": "流量口径",
    }
    for key, label in labels.items():
        if scope.get(key) and scope[key] != dataset["config"].get(key):
            issues.append(f"{label}与当前数据不一致")
    return issues


def missing_solver_inputs(key, dataset):
    if not dataset:
        return []
    fields = {
        field["key"]
        for file in dataset.get("summary", [])
        for field in file.get("fields", [])
        if field["present"]
    }
    if any(re.fullmatch(r"z\d+_temp", field) for field in fields):
        fields.add("temp")
    issues = [
        f"缺少{label}"
        for field, label in REQUIRED_FIELDS.get(key, {}).items()
        if field not in fields
    ]
    if key in {"energy", "combustion"} and dataset["config"].get("flowBasis") != "normal":
        issues.append("当前流量口径尚未确认为标况")
    return issues


def parameter_presets(template_key, values):
    allowed = set(TEMPLATE_BY_KEY.get(template_key, {}).get("parameters", []))
    if values.keys() - allowed:
        raise ValueError("参数值必须对应当前机理已映射的求解器参数")
    checked = PhysicsParameters.model_validate(values).model_dump()
    return {key: checked[key] for key in values}


def document_relations(sources):
    for source in sources:
        for line in re.split(r"[\n；;]", source["text"]):
            line = line.strip(" \t•*-`#")
            named = NAMED_RELATION.fullmatch(line)
            if named:
                name = named["name"].strip()
                key = next(
                    (k for k, template in TEMPLATE_BY_KEY.items() if name == template["name"]), None
                )
                identity = name.casefold()
                yield key or f"relation-{content_hash(identity)[:24]}", name, "", source
                continue
            match = EQUATION.fullmatch(line)
            if not match:
                continue
            lhs, rhs = match["lhs"], match["rhs"].strip()
            # Ordinary numeric settings are parameters, not engineering relationships.
            if not re.search(r"[A-Za-zΑ-ω\u4e00-\u9fff]", rhs) or not re.search(
                r"[*/×·÷+−\-^()∫√]", rhs
            ):
                continue
            canonical_lhs = unicodedata.normalize("NFKC", lhs).casefold()
            key = FORMULA_KEYS.get(canonical_lhs)
            label = (match["label"] or "").strip()
            identity = label.casefold() if label else canonical_lhs
            yield (
                key or f"relation-{content_hash(identity)[:24]}",
                label or f"{lhs} 关系",
                f"{lhs} = {rhs}",
                source,
            )


def document_candidates(sources, matched, values, provenance):
    formulas = {}
    for key, name, formula, source in document_relations(sources):
        formulas.setdefault(key, []).append((name, formula, source))
    for key in dict.fromkeys([*(key for key, entries in matched.items() if entries), *formulas]):
        lines = formulas.get(key, [])
        template = TEMPLATE_BY_KEY.get(key)
        evidence = list(
            {s["id"]: s for s in [*matched.get(key, []), *(line[2] for line in lines)]}.values()
        )[:8]
        found_formulas = list(dict.fromkeys(line[1] for line in lines if line[1]))
        if template:
            content = template_content(key)
            standard = {canonical_formula(part) for part in re.split(r"[；;]", content["formula"])}
            if any(canonical_formula(value) not in standard for value in found_formulas):
                content["formula"] = "；".join(found_formulas)[:1500]
            evidence_ids = {item["id"] for item in evidence}
            content["parameterValues"] = {
                k: values[k] for k in template["parameters"] if provenance[k] in evidence_ids
            }
        else:
            content = {
                "name": lines[0][0],
                "explanation": evidence[0]["text"][:2400],
                "formula": "；".join(found_formulas)[:1500],
                "variables": [],
                "parameterNotes": "",
                "parameterValues": {},
                "check": "请核实原文、输入单位及适用工况。",
                "scope": MechanismScope().model_dump(),
            }
        yield key, content, evidence
