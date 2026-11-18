"""四类窑次气耗对照。

相似窑次：同时长比例对齐历史窑次的实测气量。
纯数据：其它窑次上的 气量 = a + b·温度 + c·升温速率。
仅机理：气量 = α·max(升温速率, 0) + β·max(温度-40, 0)。
机理融合：机理气量加上按阶段学习的残差。

当前窑次不参与拟合。没有其它窑次时，机理用调研报告里的稳态点
（900℃约 30 Nm³/h，1395℃约 51 Nm³/h），纯数据和相似窑次标为不可用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

STAGES = ("预热", "升温", "保温", "降温")
ZONE_NAMES = ("一区", "二区", "三区", "四区", "五区", "六区")
CO2_KG_PER_NM3 = 2.16
# 1395℃稳态约 51 Nm³/h；100℃/h 升温在此之外再加约 18 Nm³/h。
ALPHA0 = 0.18
BETA0 = 51.0 / (1395.0 - 40.0)
MAX_POINTS = 2500


@dataclass
class CycleSeries:
    cycle_id: int | None
    kiln_code: str
    batch_no: str
    source_file: str
    curve_version: str | None
    load_kg: float | None
    points: list[dict[str, Any]]


@dataclass
class _Prepared:
    meta: CycleSeries
    hours: list[float]
    temp: list[float | None]
    temp_sp: list[float | None]
    temp_meas: list[float | None]
    gas: list[float | None]
    air: list[float | None]
    o2: list[float | None]
    o2_sp: list[float | None]
    afr: list[float | None]
    furnace_p: list[float | None]
    slope: list[float]
    stage: list[str]
    zones: list[list[dict[str, Any]]]


def build_workspace(
    target: CycleSeries, peers: list[CycleSeries], *, downsample: bool = True
) -> dict[str, Any]:
    prepared = _prepare(target)
    target_start = min(
        (p["ts"] for p in target.points if isinstance(p.get("ts"), datetime)), default=None
    )
    historical = [
        peer
        for peer in peers
        if peer.points
        and peer is not target
        and (target.cycle_id is None or peer.cycle_id != target.cycle_id)
        and target_start is not None
        and max(
            (p["ts"] for p in peer.points if isinstance(p.get("ts"), datetime)),
            default=target_start,
        )
        < target_start
    ]
    others = [_prepare(peer) for peer in historical]
    same = [peer for peer in others if peer.meta.kiln_code == target.kiln_code]
    train = same if sum(len(peer.hours) for peer in same) >= 30 else others
    alpha, beta, physics_source = _fit_physics(train)
    data_coeff = _fit_data(train)
    residuals, residual_std, residual_ready = _fit_residual(train, alpha, beta)

    hours = prepared.hours
    physics = [
        _physics_at(prepared.slope[i], prepared.temp[i], alpha, beta) for i in range(len(hours))
    ]
    fusion = [
        round(max(0.0, physics[i] + residuals.get(prepared.stage[i], 0.0)), 3)
        if physics[i] is not None
        else None
        for i in range(len(hours))
    ]
    physics_vals = [round(value, 3) if value is not None else None for value in physics]
    data_vals = _data_series(prepared, data_coeff)
    similar_vals, similar_peers, cross_kiln = _similar_series(prepared, others)

    band = residual_std * 1.28 if residual_ready and residual_std > 0 else None
    low: list[float | None] = []
    high: list[float | None] = []
    for value in fusion:
        if value is None:
            low.append(None)
            high.append(None)
            continue
        width = band if band is not None else 0.12 * value
        low.append(round(max(0.0, value - width), 3))
        high.append(round(value + width, 3))

    duration = hours[-1] if hours else 0.0
    suggest = duration if duration < 0.2 else round(duration * 0.62, 2)

    models = [
        _model_card(
            "similar",
            "相似窑次基线",
            "同时长比例对齐同曲线、近装载的历史窑次",
            similar_vals,
            prepared,
            _similar_note(similar_peers, cross_kiln, prepared),
        ),
        _model_card(
            "data",
            "纯数据（直接学习）",
            "气量 = a + b·温度 + c·升温速率，系数来自其它窑次",
            data_vals,
            prepared,
            _data_note(data_coeff),
        ),
        _model_card(
            "physics",
            "仅机理",
            "气量 = α·升温速率 + β·(温度-40℃)",
            physics_vals,
            prepared,
            _physics_note(alpha, beta, physics_source),
        ),
        _model_card(
            "fusion",
            "机理融合",
            "机理气量 + 按预热/升温/保温/降温学习的残差",
            fusion,
            prepared,
            _fusion_note(residuals, residual_ready),
        ),
    ]

    gaps = _gaps(prepared, similar_vals, data_vals, physics_source)
    notes = [
        "电耗未接入，碳排只按燃气 × 2.16 kgCO₂/Nm³",
        "助燃风温度和后燃室温度不在工况表中",
        f"当前窑次 {prepared.meta.source_file} 不参与拟合",
        "历史文件回放试算；未来设定曲线作为已知计划，不代表未来炉温预测或在线独立验证",
    ]
    if len(prepared.hours) >= MAX_POINTS:
        notes.append("点数较多，计算时做了等间隔抽样")

    points = [
        {
            "hour": round(hours[i], 3),
            "stage": prepared.stage[i],
            "measured": _round(prepared.gas[i]),
            "similar": similar_vals[i],
            "data": data_vals[i],
            "physics": physics_vals[i],
            "fusion": fusion[i],
            "low": low[i],
            "high": high[i],
            "tempSp": _round(prepared.temp_sp[i]),
            "tempMeas": _round(prepared.temp_meas[i]),
            "o2": _round(prepared.o2[i]),
            "o2Sp": _round(prepared.o2_sp[i]),
            "afr": _round(prepared.afr[i]),
            "air": _round(prepared.air[i]),
            "furnaceP": _round(prepared.furnace_p[i]),
            "zones": prepared.zones[i],
        }
        for i in range(len(hours))
    ]
    if downsample:
        points = _downsample(points)
    measured_total = _trap(hours, prepared.gas)
    fusion_total = _trap(hours, fusion)
    return {
        "durationHours": round(duration, 3),
        "suggestCursorHour": suggest,
        "points": points,
        "models": models,
        "peers": [_peer_payload(peer) for peer in similar_peers[:3]],
        "gaps": gaps,
        "notes": notes,
        "trainCount": len(train),
        "dataCoefficients": list(data_coeff) if data_coeff is not None else None,
        "recognition": [
            {
                "key": key,
                "label": label,
                "unit": unit,
                "present": sum(value is not None for value in values),
                "total": len(hours),
            }
            for key, label, unit, values in (
                ("tempSp", "设定温度", "℃", prepared.temp_sp),
                ("tempMeas", "六区均温", "℃", prepared.temp_meas),
                ("measured", "燃气流量", "Nm³/h", prepared.gas),
                ("air", "助燃风", "Nm³/h", prepared.air),
                ("o2", "氧含量", "%", prepared.o2),
                ("furnaceP", "窑压", "Pa", prepared.furnace_p),
            )
        ],
        "report": {
            "title": _report_title(prepared),
            "modelVersion": "机理融合 V1",
            "scope": _scope_text(prepared, len(train)),
            "physics": _physics_note(alpha, beta, physics_source),
            "alpha": round(alpha, 4),
            "beta": round(beta, 5),
            "residuals": {stage: round(residuals.get(stage, 0.0), 3) for stage in STAGES},
            "stages": [
                {
                    "name": stage,
                    "actual": round(_trap_stage(hours, prepared.gas, prepared.stage, stage), 1),
                    "fusion": round(_trap_stage(hours, fusion, prepared.stage, stage), 1),
                }
                for stage in STAGES
            ],
            "carbonMeasuredKg": round(measured_total * CO2_KG_PER_NM3, 1),
            "carbonFusionKg": round(fusion_total * CO2_KG_PER_NM3, 1),
            "carbonFactor": CO2_KG_PER_NM3,
            "trainCount": len(train),
            "excluded": prepared.meta.source_file,
            "gaps": gaps,
            "notes": notes,
        },
    }


def derive_gas(points: list[dict[str, Any]]) -> None:
    """总管瞬时为空时，用累计流量差分，否则用六区燃气之和。"""
    if any((point.get("gas") or 0) > 0.05 for point in points):
        return
    derived = False
    for index in range(1, len(points)):
        prev = points[index - 1].get("gas_total")
        cur = points[index].get("gas_total")
        if prev is None or cur is None:
            continue
        delta_h = _hours_between(points[index - 1].get("ts"), points[index].get("ts"))
        delta_g = float(cur) - float(prev)
        if delta_h <= 0 or delta_g < -1:
            continue
        points[index]["gas"] = delta_g / delta_h
        derived = True
    if derived and points[0].get("gas") is None and len(points) > 1:
        points[0]["gas"] = points[1].get("gas")
        return
    for point in points:
        if point.get("gas") is not None:
            continue
        parts = [
            zone.get("gas")
            for zone in point.get("zones") or []
            if isinstance(zone.get("gas"), int | float)
        ]
        if parts:
            point["gas"] = float(sum(parts))


def _prepare(series: CycleSeries) -> _Prepared:
    raw = [point for point in series.points if isinstance(point.get("ts"), datetime)]
    raw.sort(key=lambda point: point["ts"])
    dedup: dict[datetime, dict[str, Any]] = {}
    for point in raw:
        dedup[point["ts"]] = point
    ordered = [dedup[key] for key in sorted(dedup)]
    if len(ordered) > MAX_POINTS:
        step = (len(ordered) - 1) / (MAX_POINTS - 1)
        ordered = [ordered[int(i * step)] for i in range(MAX_POINTS)]
    origin = ordered[0]["ts"] if ordered else None
    hours = [_hours_between(origin, point["ts"]) for point in ordered]
    temp_sp = [_num(point.get("temp_sp")) for point in ordered]
    temp_meas = [_num(point.get("zone_temp")) for point in ordered]
    temp = [sp if sp is not None else meas for sp, meas in zip(temp_sp, temp_meas, strict=True)]
    slope = _slopes(hours, temp)
    tmax = max((value for value in temp if value is not None), default=0.0)
    stage = [_stage(temp[i], slope[i], tmax) for i in range(len(ordered))]
    zones = [_zones(point.get("zones")) for point in ordered]
    return _Prepared(
        meta=series,
        hours=hours,
        temp=temp,
        temp_sp=temp_sp,
        temp_meas=temp_meas,
        gas=[_non_negative(_num(point.get("gas"))) for point in ordered],
        air=[_num(point.get("air")) for point in ordered],
        o2=[_num(point.get("o2")) for point in ordered],
        o2_sp=[_num(point.get("o2_sp")) for point in ordered],
        afr=[_num(point.get("afr")) for point in ordered],
        furnace_p=[_num(point.get("furnace_p")) for point in ordered],
        slope=slope,
        stage=stage,
        zones=zones,
    )


def _fit_physics(train: list[_Prepared]) -> tuple[float, float, str]:
    x1: list[float] = []
    x2: list[float] = []
    target: list[float] = []
    for peer in train:
        for index, gas in enumerate(peer.gas):
            temp = peer.temp[index]
            if gas is None or temp is None or gas < 0 or gas > 400:
                continue
            x1.append(max(peer.slope[index], 0.0))
            x2.append(max(temp - 40.0, 0.0))
            target.append(gas)
    fitted = _solve_two(x1, x2, target)
    if fitted is None:
        return ALPHA0, BETA0, "default"
    alpha, beta = fitted
    if not (0.02 <= alpha <= 1.5 and 0.01 <= beta <= 0.12):
        return ALPHA0, BETA0, "default"
    return alpha, beta, "fitted"


def _fit_data(train: list[_Prepared]) -> tuple[float, float, float] | None:
    rows: list[list[float]] = []
    target: list[float] = []
    for peer in train:
        sampled = _stride(list(range(len(peer.hours))), 800)
        for index in sampled:
            gas = peer.gas[index]
            temp = peer.temp[index]
            if gas is None or temp is None or gas < 0 or gas > 400:
                continue
            rows.append([1.0, temp, max(peer.slope[index], 0.0)])
            target.append(gas)
    if len(rows) < 30:
        return None
    coeff = _solve_least_squares(rows, target)
    if coeff is None:
        return None
    intercept, temp_k, ramp_k = coeff
    if abs(intercept) > 400 or abs(temp_k) > 2 or abs(ramp_k) > 5:
        return None
    return intercept, temp_k, ramp_k


def _fit_residual(
    train: list[_Prepared], alpha: float, beta: float
) -> tuple[dict[str, float], float, bool]:
    buckets: dict[str, list[float]] = {stage: [] for stage in STAGES}
    all_err: list[float] = []
    for peer in train:
        for index, gas in enumerate(peer.gas):
            pred = _physics_at(peer.slope[index], peer.temp[index], alpha, beta)
            if gas is None or pred is None:
                continue
            err = max(-40.0, min(40.0, gas - pred))
            buckets[peer.stage[index]].append(err)
            all_err.append(err)
    residuals = {
        stage: (sum(values) / len(values) if len(values) >= 5 else 0.0)
        for stage, values in buckets.items()
    }
    if len(all_err) < 5:
        return residuals, 0.0, False
    mean = sum(all_err) / len(all_err)
    var = sum((item - mean) ** 2 for item in all_err) / len(all_err)
    return residuals, var**0.5, True


def _similar_series(
    target: _Prepared, peers: list[_Prepared]
) -> tuple[list[float | None], list[_Prepared], bool]:
    same = [
        peer
        for peer in peers
        if peer.meta.kiln_code == target.meta.kiln_code and _similar_ok(target, peer)
    ]
    other = [
        peer
        for peer in peers
        if peer.meta.kiln_code != target.meta.kiln_code and _similar_ok(target, peer)
    ]
    chosen = same or other
    if not chosen or not target.hours or target.hours[-1] <= 0:
        return [None] * len(target.hours), [], False
    curves = [_normalized_gas(peer) for peer in chosen]
    curves = [curve for curve in curves if curve[0]]
    if not curves:
        return [None] * len(target.hours), [], False
    duration = target.hours[-1]
    values: list[float | None] = []
    for hour in target.hours:
        frac = hour / duration
        samples = [_interp(xs, ys, frac) for xs, ys in curves]
        samples = [item for item in samples if item is not None]
        values.append(round(sum(samples) / len(samples), 3) if samples else None)
    return values, chosen, not same


def _similar_ok(target: _Prepared, peer: _Prepared) -> bool:
    if not peer.hours or peer.hours[-1] <= 0:
        return False
    tmax_t = max((value for value in target.temp if value is not None), default=None)
    tmax_p = max((value for value in peer.temp if value is not None), default=None)
    if tmax_t is not None and tmax_p is not None and abs(tmax_t - tmax_p) > 150:
        return False
    if target.hours and target.hours[-1] > 1:
        ratio = peer.hours[-1] / target.hours[-1]
        if ratio < 0.5 or ratio > 2:
            return False
    curve_t = (target.meta.curve_version or "").strip()
    curve_p = (peer.meta.curve_version or "").strip()
    if curve_t and curve_p and curve_t != curve_p:
        return False
    if target.meta.load_kg and peer.meta.load_kg:
        ratio = peer.meta.load_kg / target.meta.load_kg
        if ratio < 0.6 or ratio > 1.4:
            return False
    return True


def _data_series(
    prepared: _Prepared, coeff: tuple[float, float, float] | None
) -> list[float | None]:
    if coeff is None:
        return [None] * len(prepared.hours)
    intercept, temp_k, ramp_k = coeff
    out: list[float | None] = []
    for index, temp in enumerate(prepared.temp):
        if temp is None:
            out.append(None)
            continue
        value = intercept + temp_k * temp + ramp_k * max(prepared.slope[index], 0.0)
        out.append(round(min(300.0, max(0.0, value)), 3))
    return out


def _physics_at(slope: float, temp: float | None, alpha: float, beta: float) -> float | None:
    if temp is None:
        return None
    return max(0.0, alpha * max(slope, 0.0) + beta * max(temp - 40.0, 0.0))


def _model_card(
    key: str,
    name: str,
    summary: str,
    values: list[float | None],
    prepared: _Prepared,
    note: str,
) -> dict[str, Any]:
    available = any(value is not None for value in values)
    return {
        "key": key,
        "name": name,
        "summary": summary,
        "available": available,
        "note": note,
        "totalGas": round(_trap(prepared.hours, values), 1) if available else None,
        "mae": _mae(prepared.gas, values) if available else None,
        "stageGas": {
            stage: round(_trap_stage(prepared.hours, values, prepared.stage, stage), 1)
            for stage in STAGES
        }
        if available
        else None,
    }


def _gaps(
    prepared: _Prepared,
    similar: list[float | None],
    data_vals: list[float | None],
    physics_source: str,
) -> list[str]:
    gaps: list[str] = []
    if not (prepared.meta.curve_version or "").strip():
        gaps.append("曲线版本未填写，相似窑次只按温度和时长匹配")
    if prepared.meta.load_kg is None:
        gaps.append("装载量未填写")
    if not any(value is not None for value in similar):
        gaps.append("缺少同曲线或近温度的历史窑次，相似窑次基线不可用")
    if not any(value is not None for value in data_vals):
        gaps.append("纯数据模型缺少足够的其它窑次")
    if physics_source == "default":
        gaps.append("机理参数使用调研报告稳态点，有其它窑次后改为拟合")
    if not any(value is not None for value in prepared.temp):
        gaps.append("没有读到温度曲线，阶段和机理无法识别")
    return gaps


def _peer_payload(peer: _Prepared) -> dict[str, Any]:
    points = _downsample(
        [
            {"hour": round(peer.hours[i], 3), "gas": _round(peer.gas[i])}
            for i in range(len(peer.hours))
        ],
        limit=80,
    )
    batch = peer.meta.batch_no or "未编号"
    return {
        "id": peer.meta.cycle_id,
        "label": f"窑次 {batch}",
        "kilnCode": peer.meta.kiln_code,
        "totalGas": round(_trap(peer.hours, peer.gas), 1),
        "points": points,
    }


def _report_title(prepared: _Prepared) -> str:
    batch = prepared.meta.batch_no or _basename(prepared.meta.source_file)
    return f"{prepared.meta.kiln_code} 窑次 {batch} 能耗预测报告"


def _basename(name: str) -> str:
    text = name.replace("\\", "/").rstrip("/")
    return text.split("/")[-1] or name


def _scope_text(prepared: _Prepared, train_count: int) -> str:
    if train_count == 0:
        return (
            f"适用于已上传的六区燃气梭式窑工况表。本窑次 {prepared.meta.source_file} "
            "单独对照，机理参数来自调研报告稳态点。"
        )
    return (
        "适用于已上传的六区燃气梭式窑工况表。"
        f"本窑次 {prepared.meta.source_file} 留出对照，拟合使用其余 {train_count} 个窑次。"
    )


def _similar_note(peers: list[_Prepared], cross: bool, prepared: _Prepared) -> str:
    if not peers:
        return "没有可对齐的历史窑次"
    where = "含其它窑号" if cross else "同窑"
    curve = (prepared.meta.curve_version or "").strip() or "未填曲线版本"
    return f"对齐 {len(peers)} 个窑次（{where}，曲线 {curve}）"


def _data_note(coeff: tuple[float, float, float] | None) -> str:
    if coeff is None:
        return "其它窑次不足，未拟合"
    intercept, temp_k, ramp_k = coeff
    return f"a={intercept:.2f}，b={temp_k:.4f}，c={ramp_k:.3f}"


def _physics_note(alpha: float, beta: float, source: str) -> str:
    origin = "其它窑次拟合" if source == "fitted" else "调研报告稳态点（900℃约30、1395℃约51 Nm³/h）"
    return f"α={alpha:.3f}，β={beta:.4f}，{origin}"


def _fusion_note(residuals: dict[str, float], ready: bool) -> str:
    if not ready:
        return "没有其它窑次，残差按 0"
    text = "，".join(f"{stage}{residuals.get(stage, 0.0):+.1f}" for stage in STAGES)
    return f"分阶段残差 Nm³/h：{text}"


def _stage(temp: float | None, slope: float, tmax: float) -> str:
    if temp is None:
        return "升温"
    if slope < -8:
        return "降温"
    if temp < max(280.0, 0.35 * tmax) and slope < 50:
        return "预热"
    if abs(slope) < 12 and temp >= 0.75 * tmax and tmax > 0:
        return "保温"
    return "升温"


def _slopes(hours: list[float], temps: list[float | None]) -> list[float]:
    count = len(hours)
    out = [0.0] * count
    if count < 2:
        return out
    for index in range(count):
        if index == 0:
            left, right = 0, 1
        elif index == count - 1:
            left, right = count - 2, count - 1
        else:
            left, right = index - 1, index + 1
        t0, t1 = temps[left], temps[right]
        delta = hours[right] - hours[left]
        if t0 is None or t1 is None or delta == 0:
            out[index] = 0.0
        else:
            out[index] = (t1 - t0) / delta
    return out


def _normalized_gas(peer: _Prepared) -> tuple[list[float], list[float]]:
    duration = peer.hours[-1]
    xs: list[float] = []
    ys: list[float] = []
    for hour, gas in zip(peer.hours, peer.gas, strict=True):
        if gas is None:
            continue
        xs.append(hour / duration)
        ys.append(gas)
    return xs, ys


def _interp(xs: list[float], ys: list[float], x: float) -> float | None:
    if not xs:
        return None
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    low = 0
    high = len(xs) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if xs[mid] <= x:
            low = mid
        else:
            high = mid
    span = xs[high] - xs[low]
    if span <= 0:
        return ys[low]
    weight = (x - xs[low]) / span
    return ys[low] * (1 - weight) + ys[high] * weight


def _trap(hours: list[float], values: list[float | None]) -> float:
    total = 0.0
    for index in range(1, len(hours)):
        delta = hours[index] - hours[index - 1]
        if delta <= 0:
            continue
        left, right = values[index - 1], values[index]
        if left is None and right is None:
            continue
        if left is None:
            left = right
        if right is None:
            right = left
        if left is None or right is None:
            continue
        total += (left + right) / 2 * delta
    return total


def _trap_stage(
    hours: list[float], values: list[float | None], stages: list[str], stage: str
) -> float:
    total = 0.0
    for index in range(1, len(hours)):
        if stages[index] != stage:
            continue
        delta = hours[index] - hours[index - 1]
        if delta <= 0:
            continue
        left, right = values[index - 1], values[index]
        if left is None and right is None:
            continue
        if left is None:
            left = right
        if right is None:
            right = left
        if left is None or right is None:
            continue
        total += (left + right) / 2 * delta
    return total


def _mae(actual: list[float | None], pred: list[float | None]) -> float | None:
    errs = [
        abs(left - right)
        for left, right in zip(actual, pred, strict=True)
        if left is not None and right is not None
    ]
    if not errs:
        return None
    return round(sum(errs) / len(errs), 2)


def _solve_two(x1: list[float], x2: list[float], target: list[float]) -> tuple[float, float] | None:
    if len(target) < 30:
        return None
    s11 = s12 = s22 = p1 = p2 = 0.0
    for a, b, y in zip(x1, x2, target, strict=True):
        s11 += a * a
        s12 += a * b
        s22 += b * b
        p1 += a * y
        p2 += b * y
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-6:
        return None
    alpha = (p1 * s22 - p2 * s12) / det
    beta = (s11 * p2 - s12 * p1) / det
    return alpha, beta


def _solve_least_squares(rows: list[list[float]], target: list[float]) -> list[float] | None:
    width = len(rows[0])
    matrix = [[0.0] * width for _ in range(width)]
    vector = [0.0] * width
    for row, y in zip(rows, target, strict=True):
        for i in range(width):
            vector[i] += row[i] * y
            for j in range(width):
                matrix[i][j] += row[i] * row[j]
    return _solve(matrix, vector)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(vector)
    work = [row[:] + [vector[index]] for index, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(work[row][col]))
        if abs(work[pivot][col]) < 1e-8:
            return None
        work[col], work[pivot] = work[pivot], work[col]
        scale = work[col][col]
        for col_index in range(col, size + 1):
            work[col][col_index] /= scale
        for row in range(size):
            if row == col:
                continue
            factor = work[row][col]
            for col_index in range(col, size + 1):
                work[row][col_index] -= factor * work[col][col_index]
    return [work[index][size] for index in range(size)]


def _downsample(items: list[dict[str, Any]], limit: int = 180) -> list[dict[str, Any]]:
    count = len(items)
    if count <= limit:
        return items
    indexes = sorted({round(i * (count - 1) / (limit - 1)) for i in range(limit)})
    return [items[index] for index in indexes]


def _stride(indexes: list[int], cap: int) -> list[int]:
    if len(indexes) <= cap:
        return indexes
    step = len(indexes) / cap
    return [indexes[int(i * step)] for i in range(cap)]


def _zones(raw: Any) -> list[dict[str, Any]]:
    items = list(raw or [])
    out: list[dict[str, Any]] = []
    for index, name in enumerate(ZONE_NAMES):
        src = items[index] if index < len(items) and isinstance(items[index], dict) else {}
        out.append(
            {
                "name": name,
                "temp": _round(_num(src.get("temp"))),
                "gas": _round(_num(src.get("gas"))),
                "air": _round(_num(src.get("air"))),
                "valve": _round(_num(src.get("valve"))),
            }
        )
    return out


def _hours_between(start: Any, end: Any) -> float:
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        number = float(value)
        if number != number:
            return None
        return number
    return None


def _non_negative(value: float | None) -> float | None:
    if value is None:
        return None
    return max(value, 0.0)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, digits)
