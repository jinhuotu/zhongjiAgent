"""Executable, serializable algorithms with chronological holdouts and causal features.

No model-generated code is executed. A forecast sees only origin-time observations
and the declared future setpoint plan. Metrics always come from held-out targets.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import PurePosixPath
from time import perf_counter
from typing import NamedTuple

import numpy as np
from pydantic import BaseModel, ValidationError

from common.kiln_studio_evaluation import error_statistics
from common.kiln_studio_external import external_config_issue, external_predictions
from common.kiln_studio_lstm import (
    fit_lstm,
    history_sequence,
    predict_lstm,
    predict_lstm_many,
    predict_lstm_path,
)
from common.kiln_studio_observations import clean_rows, observation_quality
from common.kiln_studio_plugins import install_algorithms
from common.kiln_studio_schema import AlgorithmMetadata, ExperimentRequest

TEMPLATES = [
    {
        "key": "energy",
        "name": "集总能量平衡",
        "formula": "q_gas = [C_eff·max(dT_sp/dt,0) + 3600·UA·max(T_sp−T_a,0)] / (1000·η·LHV)",
        "explanation": (
            "按烧成计划估算燃气需求。升温时计入窑体与装载的等效蓄热，"
            "保温时主要依靠等效散热项。冷却段仍需结合停火状态判断适用性。"
        ),
        "variables": ["设定温度 ℃", "计划升温速率 ℃/h", "燃气流量 Nm³/h"],
        "parameters": ["capacityKJ", "lossKW", "ambientC", "lhvMJ", "efficiency"],
        "task": "energy",
        "role": "燃气流量预测",
        "check": (
            "核对燃气计量边界、低位热值及装载条件。"
            "完整热平衡还需要装载质量、烟气质量流量及温度等测量。"
        ),
    },
    {
        "key": "thermal",
        "name": "闭环温度热惯性",
        "formula": "dT/dt = (T_sp−T)/τ",
        "explanation": (
            "窑炉处于自动温控时，用起点实测温度和已知设定计划估计温度跟随速度。"
            "响应时间常数越大，实测温度追随设定越慢。"
        ),
        "variables": ["历史实测温度 ℃", "未来已知温度计划 ℃"],
        "parameters": ["tauMinutes"],
        "task": "temperature",
        "role": "温度走势预测",
        "check": "核对预测温区与温度设定的对应关系。手动调节、点火切换和停火冷却阶段应单独核验。",
    },
    {
        "key": "combustion",
        "name": "燃烧配比关系",
        "formula": "AFR = q_air/q_gas；AFR_ref = AFR_theory·λ",
        "explanation": (
            "用同标况的助燃风流量除以燃气流量，得到实测空燃比。"
            "理论配比乘参考过量空气系数，可作为历史工况的对照值。"
        ),
        "variables": ["空气流量 Nm³/h", "燃气流量 Nm³/h"],
        "parameters": ["theoreticalAfr", "excessAir", "minGas"],
        "task": "afr",
        "role": "空燃比参考与预测基线",
        "check": (
            "核对总管和各烧嘴支管的测点范围。资料里的阀位比单独保留，"
            "燃气低于门槛的区间不计算实测空燃比。"
        ),
    },
    {
        "key": "pressure",
        "name": "炉压与烟气收支",
        "formula": "dN/dt = n_in−n_out；pV=NRT",
        "explanation": (
            "窑压与进气、排烟和漏风有关。运行记录中的窑压可用于趋势回放，"
            "现场差压测点还需要与模型里的绝对压力基准对应。"
        ),
        "variables": ["窑压 Pa", "烟气流量", "炉膛有效容积"],
        "parameters": [],
        "task": None,
        "role": "待补充数据与求解器",
        "check": "确认有效炉膛容积、排烟流量或风机特性、漏风模型及压力基准。",
    },
    {
        "key": "recovery",
        "name": "助燃风余热回收",
        "formula": "Q_air = m_air·c_p·(T_air−T_a)",
        "explanation": (
            "烟气经换热器预热助燃风。量化回收热量时应同时对齐风量和换热器前后温度，"
            "并和燃气模型里的等效损失项核对边界。"
        ),
        "variables": ["空气温度", "空气质量流量", "比热"],
        "parameters": [],
        "task": None,
        "role": "待补充数据与求解器",
        "check": "确认换热器进出口风温、流量及对应时刻、比热和质量流量换算。",
    },
    {
        "key": "carbon",
        "name": "燃气能碳换算",
        "formula": "V_gas = ∫q_gas dt；E_gas = V_gas·LHV",
        "explanation": (
            "本版输出燃气体积和按热值计算的燃气能量；"
            "未设置可追溯排放因子，不输出碳排结果；无电量不输出综合能耗。"
        ),
        "variables": ["燃气流量 Nm³/h", "时间 h", "低位热值 MJ/Nm³"],
        "parameters": ["lhvMJ"],
        "task": None,
        "role": "派生指标",
        "check": "气耗按有效流量区间积分；碳排和综合能耗还需各自的排放因子与电量。",
    },
    {
        "key": "uniformity",
        "name": "温区均匀性与热耦合",
        "formula": "ΔT_zones(t) = maxᵢ Tᵢ(t) − minᵢ Tᵢ(t)",
        "explanation": (
            "同一时刻的分区温差可以描述窑内均匀性。"
            "建立分区传热模型还要识别各区燃烧输入和区间热交换。"
        ),
        "variables": ["同一时刻的有效温区实测温度 ℃", "分区燃气与助燃风流量 Nm³/h"],
        "parameters": [],
        "task": None,
        "role": "工况诊断关系",
        "check": "先核对热电偶位置及有效温区，再补充分区热容、耦合系数和装载布置。",
    },
    {
        "key": "stages",
        "name": "烧成阶段与装载热负荷",
        "formula": "Q_load ≈ m_load·c_p·ΔT；分阶段核验蓄热、保温损失与冷却",
        "explanation": (
            "升温、保温、冷却和待机的数据适用性不同。"
            "同一组等效热容和响应参数应结合装载及控制模式判断。"
        ),
        "variables": ["烧成阶段及控制模式", "装载质量 kg", "产品和窑具温度 ℃"],
        "parameters": [],
        "task": None,
        "role": "工况诊断关系",
        "check": "以工艺计划或状态字段确认阶段，核对产品、窑具和装载量。阶段信息目前由资料核对。",
    },
]
PARAMETERS = [
    (
        "ambientC",
        "环境温度",
        "℃",
        "散热项的环境基准。优先采用现场温度记录，也可录入有来源的代表值。",
    ),
    (
        "lhvMJ",
        "燃气低位热值",
        "MJ/Nm³",
        "每标方燃气可提供的低位热量。填写气质报告在对应标况下的数值。",
    ),
    (
        "efficiency",
        "等效利用效率",
        "0–1",
        "燃料热输入计入等效热平衡的比例。与热容、损失存在耦合，标定时固定。",
    ),
    (
        "capacityKJ",
        "等效热容",
        "kJ/K",
        "窑体与装载每升高 1 K 的等效蓄热量。装载变化后需重新验证，可用训练集标定。",
    ),
    (
        "lossKW",
        "等效损失系数",
        "kW/K",
        "温差每增加 1 K 对应的等效散热功率，合并窑壁及排烟等损失，可用训练集标定。",
    ),
    (
        "tauMinutes",
        "闭环响应时间常数",
        "min",
        "设定温度阶跃后，模型达到最终温差约 63% 的时间。数值越大，温度响应越慢。",
    ),
    (
        "theoreticalAfr",
        "理论空气燃气比",
        "Nm³/Nm³",
        "按燃气组成确定的理论空气量与燃气量之比，采用同一标况。",
    ),
    (
        "excessAir",
        "参考过量空气系数",
        "无量纲",
        "用于把理论配比换算成对照配比。具体值需注明工况、气质和来源。",
    ),
    (
        "minGas",
        "配比有效燃气阈值",
        "Nm³/h",
        "过滤停火或过小流量，避免用很小的燃气流量作分母产生失真的配比。",
    ),
]
# 推理复杂度按计算结构定性描述。单点实测耗时优先使用实验保存的试跑数据。
_ALGORITHM_DEFINITIONS = [
    {
        "key": "lstm",
        "name": "LSTM 温度与燃气时序预测",
        "category": "时序神经网络",
        "oneLine": "从过去一段测点序列学习未来变化",
        "strengths": ["学习连续测点的先后变化，并结合预测起点已知的工艺计划"],
        "benefits": ["可与持续值、回归和机理在相同记录上直接核对"],
        "fitScenarios": ["具有连续温度或燃气记录，并覆盖多个烧成阶段"],
        "limitations": ["本地演示使用小型网络和最多 2048 个训练样本，跨工况效果需独立检验"],
        "dataRequirements": ["完整历史窗口中的目标读数及可用工况测点"],
        "interpretability": "medium",
        "sampleSizeRisk": "high",
        "inferenceComplexity": "high",
        "interventionCapable": False,
        "status": "enabled",
        "version": "1",
        "formula": "h,c = LSTM(x_history); ŷ = y_now + W_h·h + W_x·x_plan + b",
        "explanation": "16 步历史序列、8 个隐藏单元，使用训练集归一化与反向传播更新权重。",
        "mechanismRole": "机理用于独立对照，不参与该网络的数值计算",
    },
    {
        "key": "baseline",
        "category": "对照基线",
        "oneLine": "用现在的读数估计稍后的读数",
        "strengths": ["只依赖当前读数，计算过程容易核对"],
        "benefits": ["提供一条统一的误差参照线"],
        "fitScenarios": ["读数短时间内平稳，或需要检验其他算法的改善"],
        "limitations": ["升温、停火和切换阶段可能跟不上变化"],
        "dataRequirements": ["连续的目标测点读数"],
        "interpretability": "high",
        "sampleSizeRisk": "low",
        "inferenceComplexity": "low",
        "interventionCapable": False,
        "status": "enabled",
        "version": "1",
        "name": "持续值基线",
        "formula": "ŷ(t+h)=y(t)",
        "explanation": "假定未来保持当前值，检验复杂算法是否确有增益。",
        "mechanismRole": "仅提供物理解释，不参与数值预测",
    },
    {
        "key": "ridge",
        "category": "数据模型",
        "oneLine": "从历史走势和已知工艺计划学习预测规律",
        "strengths": ["综合多个测点，并抑制单个测点对结果的影响"],
        "benefits": ["在工况相对稳定时，比只看当前值更能跟踪趋势"],
        "fitScenarios": ["积累了一些可比窑次，变化关系较平稳"],
        "limitations": ["遇到不同装载、配方或明显非线性变化时需要复核"],
        "dataRequirements": ["连续目标读数，历史测点和计划越完整越好"],
        "interpretability": "medium",
        "sampleSizeRisk": "medium",
        "inferenceComplexity": "low",
        "interventionCapable": False,
        "status": "enabled",
        "version": "1",
        "name": "时序岭回归",
        "formula": "min ||Xw−y||² + α||w||²",
        "explanation": "使用当前值、历史窗口统计及已知设定计划；仅用训练集填补缺失和标准化。",
        "mechanismRole": "仅提供解释，不参与数值预测",
    },
    {
        "key": "boosting",
        "category": "数据模型",
        "oneLine": "从历史记录学习升温、保温等阶段的不同变化",
        "strengths": ["能捕捉读数之间较复杂的关系"],
        "benefits": ["工况覆盖充分时，可发现简单模型容易漏掉的变化"],
        "fitScenarios": ["有多批数据覆盖不同阶段和典型切换"],
        "limitations": ["窑次少时容易记住个别工况，遇到新工况容易偏离"],
        "dataRequirements": ["有代表性的多批工况和足够连续的历史测点"],
        "interpretability": "low",
        "sampleSizeRisk": "high",
        "inferenceComplexity": "medium",
        "interventionCapable": False,
        "status": "enabled",
        "version": "1",
        "name": "梯度提升回归树桩",
        "formula": "F_m(x)=F_(m−1)(x)+ν·tree_m(x)",
        "explanation": "每轮选择一个二叶回归树拟合残差；轮数和学习率真实参与训练。",
        "mechanismRole": "仅提供解释，不参与数值预测",
    },
    {
        "key": "physics",
        "category": "机理模型",
        "oneLine": "按窑炉工程关系计算温度、燃气需求或参考配比",
        "strengths": ["预测可追溯到已保存的公式和参数"],
        "benefits": ["便于说明参数或工艺计划如何影响模型中的走势"],
        "fitScenarios": ["历史批次较少，且已确认适用的工程关系"],
        "limitations": ["装载变化、手动操作或参数假设失准时需复核"],
        "dataRequirements": ["任务对应机理已启用，温度和气耗需完整设定计划"],
        "interpretability": "high",
        "sampleSizeRisk": "low",
        "inferenceComplexity": "low",
        "interventionCapable": True,
        "status": "enabled",
        "version": "1",
        "name": "机理基线",
        "formula": "由所选任务的受控机理计算",
        "explanation": "温度用闭环热惯性，气耗用集总热平衡，空燃比用理论配比参考。",
        "mechanismRole": "直接计算",
    },
    {
        "key": "fusion",
        "category": "融合模型",
        "oneLine": "先按机理估算，再用历史实测修正经常出现的偏差",
        "strengths": ["保留工程关系，同时吸收已有数据中的误差信息"],
        "benefits": ["便于看到机理解释，并检查偏差修正是否改善预测"],
        "fitScenarios": ["有可信机理并有连续记录可用于估计偏差"],
        "limitations": ["工况变化后，过去的修正量未必仍然有效"],
        "dataRequirements": ["任务对应机理和可回测历史，每个温度或气耗窗口需完整设定计划"],
        "interpretability": "high",
        "sampleSizeRisk": "medium",
        "inferenceComplexity": "medium",
        "interventionCapable": True,
        "status": "enabled",
        "version": "1",
        "name": "机理与残差融合",
        "formula": "ŷ = f_physics(x,θ) + ridge(x)",
        "explanation": "先算机理预测，再用训练集拟合机理残差；保存机理参数和残差权重。",
        "mechanismRole": "机理作为残差基线参与预测",
    },
    {
        "key": "lasso",
        "name": "Lasso 稀疏回归",
        "category": "数据模型",
        "oneLine": "在历史记录中学习预测规律，让影响较弱的测点退出模型",
        "strengths": ["用较少的输入项表达预测关系"],
        "benefits": ["便于排查模型主要依赖哪些测点"],
        "fitScenarios": ["可用测点较多、希望得到简洁的线性模型"],
        "limitations": ["测点变化相似时保留项可能随数据改变，权重只表示关联"],
        "dataRequirements": ["连续目标读数及稳定的历史测点"],
        "interpretability": "medium",
        "sampleSizeRisk": "medium",
        "inferenceComplexity": "low",
        "interventionCapable": False,
        "status": "enabled",
        "version": "1",
        "formula": "min ||Xw−y||²/(2n) + α||w||₁",
        "explanation": "仅用训练数据标准化和拟合，L1 惩罚可将部分系数压到零。",
        "mechanismRole": "机理不参与数值预测",
    },
]


def content_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def target_key(config):
    return (
        f"z{config.zone}_temp"
        if config.task == "temperature" and config.zone
        else {"temperature": "temp", "energy": "gas", "afr": "afr"}[config.task]
    )


def _number(value):
    return float(value) if value is not None and math.isfinite(float(value)) else None


def make_features(history, sp_future, config, *, include_sequence=False):
    key = target_key(config)
    current = history[-1]
    threshold = current["time"] - config.historyMinutes
    start = bisect.bisect_right([p["time"] for p in history], threshold) - 1
    if start < 0:
        return None
    window = history[start:]
    values = [p.get(key) for p in window if p.get(key) is not None]
    if current.get(key) is None or not values:
        return None
    sp0 = current.get("temp_sp")
    slope = (
        (sp_future - sp0) * 60 / config.horizonMinutes
        if sp_future is not None and sp0 is not None
        else None
    )
    feats = [
        current.get(key),
        values[0],
        sum(values) / len(values),
        max(values) - min(values),
        current.get("temp"),
        current.get("gas"),
        current.get("air"),
        current.get("o2"),
        current.get("pressure"),
        sp0,
        sp_future,
        slope,
    ]
    result = {
        "x": [_number(v) for v in feats],
        "y0": current[key],
        "sp0": sp0,
        "sp1": sp_future,
        "slope": slope,
        "origin": current["ts"],
        "originTime": current["time"],
        "cycle": current["cycle"],
    }
    if include_sequence or "lstm" in (config.selectedAlgorithmKeys or []):
        result["sequence"] = history_sequence(window, key)
    return result


FEATURES = [
    "当前目标值",
    "窗口最早目标值",
    "窗口目标均值",
    "窗口目标极差",
    "当前均温",
    "当前燃气流量",
    "当前空气流量",
    "当前氧含量",
    "当前窑压",
    "当前温度设定",
    "未来已知温度设定",
    "计划平均升温速率",
]


def examples(cycles, config, min_gas, *, include_sequence=False, include_trajectory=False):
    out = []
    key = target_key(config)
    for cycle in cycles:
        rows = clean_rows(
            cycle["points"],
            afr_floor=min_gas if config.task == "afr" else None,
            exclude_zero_temperature=config.excludeZeroTemperatureRecords,
        )
        if config.task == "afr":
            rows = [
                {
                    **row,
                    "afr": row["air"] / row["gas"]
                    if row.get("gas") is not None
                    and row["gas"] >= min_gas
                    and row.get("air") is not None
                    else None,
                }
                for row in rows
            ]
        times = [p["time"] for p in rows]
        positive_steps = [b - a for a, b in zip(times, times[1:], strict=False) if b > a]
        step = float(np.median(positive_steps)) if positive_steps else 5
        if step > config.horizonMinutes:
            continue
        for i, row in enumerate(rows):
            if row.get(key) is None:
                continue
            j = bisect.bisect_left(times, row["time"] + config.horizonMinutes)
            if j >= len(rows) or times[j] - times[i] > config.horizonMinutes + min(step * 0.51, 5):
                continue
            future = rows[j]
            y = future.get(key)
            if config.task == "afr":
                y = (
                    future.get("air") / future["gas"]
                    if future.get("air") is not None
                    and future.get("gas") is not None
                    and future["gas"] >= min_gas
                    else None
                )
                if row.get("gas") is None or row["gas"] < min_gas:
                    continue
            if y is None or (config.task in {"energy", "afr"} and y < 0):
                continue
            ex = make_features(
                rows[max(0, i - 1500) : i + 1],
                future.get("temp_sp"),
                config,
                include_sequence=include_sequence,
            )
            if ex:
                ex.update(
                    y=float(y),
                    time=future["time"],
                    ts=future["ts"],
                    actualHorizonMinutes=times[j] - times[i],
                )
                if config.task == "temperature" and not config.zone:
                    ex["actualTemperatureSource"] = future.get("tempSource")
                if (
                    include_trajectory
                    or include_sequence
                    or "lstm" in (config.selectedAlgorithmKeys or [])
                ):
                    trajectory = []
                    trajectory_plans = []
                    for offset in range(1, config.horizonMinutes + 1):
                        target = row["time"] + offset
                        position = bisect.bisect_left(times, target, lo=i + 1)
                        target_value = (
                            rows[position].get(key)
                            if position < len(rows) and abs(times[position] - target) < 1e-6
                            else None
                        )
                        trajectory.append(target_value)
                        trajectory_plans.append(
                            rows[position].get("temp_sp")
                            if position < len(rows) and abs(times[position] - target) < 1e-6
                            else None
                        )
                    trajectory[-1] = float(y)
                    ex["trajectoryTargets"] = trajectory
                    ex["trajectoryPlans"] = trajectory_plans
                out.append(ex)
    return sorted(out, key=lambda x: (x["originTime"], x["cycle"]))


def split_examples(examples, cycle_groups=None):
    if len(examples) < 90:
        raise ValueError("有效预测样本不足90个，请增加数据、补齐目标字段或缩短历史/预测窗口")
    cycles = {}
    for ex in examples:
        cycle = ex["cycle"]
        cycles.setdefault((cycle_groups or {}).get(cycle, cycle), []).append(ex)
    ordered = sorted(cycles.values(), key=lambda x: x[0]["originTime"])
    separate = all(
        max(x["time"] for x in a) < min(x["originTime"] for x in b)
        for a, b in zip(ordered, ordered[1:], strict=False)
    )
    if len(ordered) >= 3 and separate:
        n = max(1, min(len(ordered) - 2, int(len(ordered) * 0.6)))
        m = max(n + 1, min(len(ordered) - 1, int(len(ordered) * 0.8)))
        groups = [
            [e for c in ordered[:n] for e in c],
            [e for c in ordered[n:m] for e in c],
            [e for c in ordered[m:] for e in c],
        ]
        method = (
            "按资料目录识别的完整窑次及时间先后划分"
            if cycle_groups
            else "按完整文件窑次及时间先后划分；文件应各对应一完整窑次"
        )
    else:
        times = sorted({x["originTime"] for x in examples})
        a, b = times[int(len(times) * 0.6)], times[int(len(times) * 0.8)]
        groups = [
            [e for e in examples if e["time"] < a],
            [e for e in examples if e["originTime"] >= a and e["time"] < b],
            [e for e in examples if e["originTime"] >= b],
        ]
        method = "按时间60/20/20划分，并剔除标签跨越边界的窗口；同窑次探索性验证"
    if len(groups[0]) < 30 or min(len(groups[1]), len(groups[2])) < 10:
        raise ValueError("隔离后的训练/验证/测试样本不足30/10/10，请增加完整窑次或缩短窗口")
    return groups, method


def compare_mechanisms(cycles, config, candidates):
    required = {"temperature": "thermal", "energy": "energy", "afr": "combustion"}[config.task]
    eligible = [item for item in candidates if required in item["templateKeys"]]
    if not eligible:
        raise ValueError("当前方案和所选历史版本均未启用该任务的求解机理")
    floor = max(item["parameters"]["minGas"] for item in eligible) if config.task == "afr" else 0
    samples = examples(cycles, config, floor)
    groups = {}
    for cycle in cycles:
        parent = PurePosixPath(cycle["name"].replace("\\", "/")).parent
        if re.search(r"窑次[ _-]*\d+$", parent.name):
            groups[cycle["id"]] = str(parent)
    grouped = groups if len(groups) == len(cycles) else None
    (_, validation, _), method = split_examples(samples, grouped)
    if grouped is None and method.startswith("按完整文件"):
        method = "按数据文件和时间划分；文件与实际窑次的对应关系待核对"
    # All versions use the same predictions' target timestamps and flow filter.
    common = (
        validation
        if config.task == "afr"
        else [row for row in validation if row["sp0"] is not None and row["sp1"] is not None]
    )
    if len(common) < 10:
        raise ValueError("共同有效验证点不足 10 个，请补齐温度设定或检查数据与窗口")
    scores, values = [], {}
    for item in candidates:
        follows = item in eligible
        if follows:
            predicted = [
                physics(row, item["parameters"], config.task, config.horizonMinutes)
                for row in common
            ]
            values[item["id"]] = predicted
            score = metrics(common, predicted, config.task)
        else:
            score = None
        scores.append(
            {
                "id": item["id"],
                "title": item["title"],
                "version": item["version"],
                "metric": score,
            }
        )
    ranked = [item for item in scores if item["metric"] is not None]
    best = min((item["metric"]["mae"] for item in ranked), default=None)
    stride = max(1, math.ceil(len(common) / 500))
    return {
        "scores": scores,
        "bestIds": [
            item["id"]
            for item in ranked
            if math.isclose(item["metric"]["mae"], best, rel_tol=1e-9, abs_tol=1e-9)
        ]
        if len(ranked) > 1
        else [],
        "points": [
            {
                "ts": row["ts"],
                "actual": row["y"],
                "fold": row["cycle"],
                "values": {key: predicted[i] for key, predicted in values.items()},
            }
            for i, row in enumerate(common)
            if i % stride == 0
        ],
        "count": len(common),
        "validationCount": len(validation),
        "afrFloor": floor if config.task == "afr" else None,
        "splitMethod": method,
        "start": common[0]["ts"],
        "end": common[-1]["ts"],
        "unit": {"temperature": "℃", "energy": "Nm³/h", "afr": "Nm³/Nm³"}[config.task],
    }


def physics(ex, params, task, horizon):
    if task == "afr":
        return params["theoreticalAfr"] * params["excessAir"]
    if ex["sp0"] is None or ex["sp1"] is None or ex["slope"] is None:
        return None
    if task == "energy":
        return (
            params["capacityKJ"] * max(ex["slope"], 0)
            + 3600 * params["lossKW"] * max(ex["sp1"] - params["ambientC"], 0)
        ) / (1000 * params["efficiency"] * params["lhvMJ"])
    tau = params["tauMinutes"] / 60
    h = horizon / 60
    slope = ex["slope"]
    return ex["sp1"] - slope * tau + (ex["y0"] - ex["sp0"] + slope * tau) * math.exp(-h / tau)


def calibrate(train, original, config):
    p = dict(original)
    info = []
    valid = [e for e in train if e["sp0"] is not None and e["sp1"] is not None]
    if not config.calibrate:
        return p, ["沿用保存参数：本次未重新标定"]
    if config.task == "temperature" and "tauMinutes" not in config.lockedParameters and valid:
        grid = np.geomspace(1, 600, 120)
        best = min(
            grid,
            key=lambda tau: sum(
                (
                    physics(
                        e, {**p, "tauMinutes": float(tau)}, "temperature", config.horizonMinutes
                    )
                    - e["y"]
                )
                ** 2
                for e in valid
            ),
        )
        p["tauMinutes"] = float(best)
        info.append("τ：仅用训练集在1–600分钟内网格搜索，最小化平方误差")
    elif config.task == "energy" and valid:
        a = np.array(
            [[max(e["slope"], 0), 3600 * max(e["sp1"] - p["ambientC"], 0)] for e in valid],
            dtype=float,
        ) / (1000 * p["efficiency"] * p["lhvMJ"])
        y = np.array([e["y"] for e in valid])
        names = ["capacityKJ", "lossKW"]
        weights = np.array([p[k] for k in names])
        lower, upper = [100, 0.0001], [1e7, 100]
        # Coordinate descent on bounded least squares; at most two unknowns.
        for _ in range(150):
            for j, name in enumerate(names):
                if name in config.lockedParameters or np.dot(a[:, j], a[:, j]) < 1e-10:
                    continue
                residual = y - a @ weights + a[:, j] * weights[j]
                weights[j] = np.clip(
                    np.dot(a[:, j], residual) / np.dot(a[:, j], a[:, j]), lower[j], upper[j]
                )
        p.update(dict(zip(names, weights.tolist(), strict=False)))
        condition = np.linalg.cond(a)
        info.append("C_eff、UA：仅训练集有界最小二乘；LHV和效率固定，锁定参数保持不变")
        if condition > 1e5 or np.linalg.matrix_rank(a) < 2:
            info.append("训练工况激励不足，热容与损失可能不可辨识；建议锁定一项或补充升温/保温工况")
    elif config.task == "afr":
        info.append("参考配比参数保持原值，不通过历史偏大配比反推所谓最优值")
    return p, info or ["参数已锁定或训练输入不足，保持保存值"]


def fit_preprocessor(x):
    raw = np.array([[float("nan") if v is None else v for v in row] for row in x])
    counts = np.sum(np.isfinite(raw), axis=0)
    mean = np.nansum(raw, axis=0) / np.maximum(counts, 1)
    filled = np.where(np.isfinite(raw), raw, mean)
    scale = filled.std(axis=0)
    scale[scale < 1e-8] = 1
    return {
        "mean": mean.tolist(),
        "scale": scale.tolist(),
        "missingColumns": [FEATURES[i] for i in range(len(counts)) if counts[i] == 0],
    }


def transform(x, prep):
    raw = np.array([[float("nan") if v is None else v for v in row] for row in x])
    mean, scale = np.array(prep["mean"]), np.array(prep["scale"])
    return (np.where(np.isfinite(raw), raw, mean) - mean) / scale


def fit_ridge(x, y, alpha):
    design = np.column_stack([np.ones(len(x)), x])
    penalty = np.eye(design.shape[1]) * alpha
    penalty[0, 0] = 0
    return np.linalg.solve(design.T @ design + penalty, design.T @ y).tolist()


def fit_lasso(x, y):
    """标准化后求解 L1 线性回归，惩罚强度只由训练集确定。"""
    intercept = float(y.mean())
    centered = y - intercept
    gram = x.T @ x / len(x)
    signal = x.T @ centered / len(x)
    alpha = 0.02 * float(np.max(np.abs(signal)))
    weights = np.zeros(x.shape[1])
    for _ in range(1000):
        previous = weights.copy()
        for j, norm in enumerate(gram.diagonal()):
            if norm <= 1e-12:
                continue
            correlation = signal[j] - gram[j] @ weights + norm * weights[j]
            weights[j] = np.sign(correlation) * max(0.0, abs(correlation) - alpha) / norm
        if np.max(np.abs(weights - previous)) <= 1e-8 * max(1.0, np.max(np.abs(weights))):
            break
    return [intercept, *weights.tolist()]


def fit_boosting(x, y, rounds, rate):
    initial = float(np.mean(y))
    pred = np.full(len(y), initial)
    trees = []
    candidates = [
        (j, float(t), x[:, j] <= t)
        for j in range(x.shape[1])
        for t in np.unique(np.quantile(x[:, j], np.linspace(0.05, 0.95, 15)))
    ]
    for _ in range(rounds):
        residual = y - pred
        best = None
        for j, threshold, mask in candidates:
            n = int(mask.sum())
            if n < 5 or len(y) - n < 5:
                continue
            left, right = float(residual[mask].mean()), float(residual[~mask].mean())
            error = float(np.sum((residual - np.where(mask, left, right)) ** 2))
            if best is None or error < best[0]:
                best = (error, j, threshold, left, right, mask)
        if best is None:
            break
        _, j, threshold, left, right, mask = best
        pred += rate * np.where(mask, left, right)
        trees.append([j, threshold, left, right])
    return {"initial": initial, "rate": rate, "trees": trees}


def _predict_physics(artifact, ex):
    config = artifact["config"]
    return physics(ex, artifact["parameters"], config["task"], config["horizonMinutes"])


def _predict_linear(artifact, ex):
    x = transform([ex["x"]], artifact["preprocessor"])[0]
    return float(np.dot(np.r_[1.0, x], artifact["model"]))


def _predict_boosting(artifact, ex):
    x = transform([ex["x"]], artifact["preprocessor"])[0]
    model = artifact["model"]
    return model["initial"] + model["rate"] * sum(
        left if x[j] <= t else right for j, t, left, right in model["trees"]
    )


def _predict_fusion(artifact, ex):
    base = _predict_physics(artifact, ex)
    return None if base is None else base + _predict_linear(artifact, ex)


def _fit_fusion(x, y, config, parameters, train):
    residual = y - np.array(
        [physics(e, parameters, config.task, config.horizonMinutes) for e in train]
    )
    return fit_ridge(x, residual, config.ridgeAlpha)


def _mechanism_unavailable_reason(task, template_keys, missing_plan):
    if task != "afr" and missing_plan:
        return "设定温度计划存在缺失，不能进行机理求解"
    required = {"temperature": "thermal", "energy": "energy", "afr": "combustion"}[task]
    if required not in template_keys:
        return "请在机理版本中启用该任务关联的机理模板"
    return ""


class AlgorithmImplementation(NamedTuple):
    predict: Callable
    fit: Callable | None = None
    unavailable: Callable[[str, list[str], bool], str] | None = None
    sequence: bool = False
    predict_many: Callable | None = None
    predict_path: Callable | None = None
    parameter_model: type[BaseModel] | None = None
    tasks: tuple[str, ...] = ("temperature", "energy", "afr")


# 保留内部旧名称，避免已有适配器引用失效。
_AlgorithmImplementation = AlgorithmImplementation


# 新增算法接入点：登记训练和预测实现。下架时保留实现，以便重放已保存的模型。
_IMPLEMENTATIONS = {
    "lstm": AlgorithmImplementation(
        predict_lstm,
        fit_lstm,
        sequence=True,
        predict_many=predict_lstm_many,
        predict_path=predict_lstm_path,
    ),
    "baseline": _AlgorithmImplementation(lambda artifact, ex: ex["y0"]),
    "ridge": _AlgorithmImplementation(
        _predict_linear, lambda x, y, config, parameters, train: fit_ridge(x, y, config.ridgeAlpha)
    ),
    "boosting": _AlgorithmImplementation(
        _predict_boosting,
        lambda x, y, config, parameters, train: fit_boosting(
            x, y, config.boostingRounds, config.learningRate
        ),
    ),
    "physics": _AlgorithmImplementation(
        _predict_physics, unavailable=_mechanism_unavailable_reason
    ),
    "fusion": _AlgorithmImplementation(_predict_fusion, _fit_fusion, _mechanism_unavailable_reason),
    "lasso": _AlgorithmImplementation(
        _predict_linear, lambda x, y, config, parameters, train: fit_lasso(x, y)
    ),
}

# 算法键也用于历史实验数据点，保留现有曲线字段名。
ALGORITHMS = tuple(AlgorithmMetadata.model_validate(item) for item in _ALGORITHM_DEFINITIONS)
_ALGORITHMS_BY_KEY = {item.key: item for item in ALGORITHMS}
_BUILTIN_KEYS = frozenset(_ALGORITHMS_BY_KEY)
_POINT_FIELDS = {"ts", "origin", "cycle", "split", "actual", "setpoint", "cumulative"}
if (
    len(_ALGORITHMS_BY_KEY) != len(ALGORITHMS)
    or _ALGORITHMS_BY_KEY.keys() - _IMPLEMENTATIONS.keys()
    or _ALGORITHMS_BY_KEY.keys() & _POINT_FIELDS
):
    raise ValueError("算法注册条目重复、缺少实现或与回测字段冲突")


def algorithm_library(*, enabled_only=False):
    from common.kiln_algorithm_capabilities import describe_algorithm

    return [
        describe_algorithm(item)
        for item in _ALGORITHMS_BY_KEY.values()
        if not enabled_only or item.status == "enabled"
    ]


def algorithm_implementation(key):
    return _IMPLEMENTATIONS.get(key)


def model_uses_sequence(artifact):
    impl = algorithm_implementation(artifact.get("implementationKey", artifact["algorithm"]))
    return bool(impl and impl.sequence)


def register_algorithm(definition, implementation, *, onnx_exporter=None):
    """仅供受控项目代码在进程启动时注册，不接收页面上传的 Python 代码。"""
    definition = AlgorithmMetadata.model_validate(definition)
    key = definition.key
    if (
        key in _ALGORITHMS_BY_KEY
        or key in _POINT_FIELDS
        or key == "onnx"
        or key.startswith(("custom_", "package_"))
    ):
        raise ValueError(f"算法键 {key} 重复、保留或与资产键冲突")
    if definition.baseKey or definition.external or not definition.version.strip():
        raise ValueError("新算法注册须提供独立实现及非空版本")
    if not isinstance(implementation, AlgorithmImplementation):
        raise ValueError("算法实现须为 AlgorithmImplementation")
    callbacks = (
        implementation.predict,
        implementation.fit,
        implementation.unavailable,
        implementation.predict_many,
        implementation.predict_path,
        onnx_exporter,
    )
    if not callable(implementation.predict) or any(
        v is not None and not callable(v) for v in callbacks
    ):
        raise ValueError("算法回调不可调用")
    if not implementation.tasks or set(implementation.tasks) - {"temperature", "energy", "afr"}:
        raise ValueError("算法支持的预测任务无效")
    _validate_parameter_schema(implementation.parameter_model)
    if onnx_exporter is not None:
        from common.kiln_onnx_graph import register_onnx_converter

        register_onnx_converter(key, onnx_exporter)
    _IMPLEMENTATIONS[key] = implementation
    _ALGORITHMS_BY_KEY[key] = definition


def _validate_parameter_schema(model):
    if model is None:
        return
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError("算法参数须使用 Pydantic 模型")
    if model.model_config.get("extra") != "forbid":
        raise ValueError("算法参数模型必须禁止未知字段（extra='forbid'）")
    if any(
        field.alias or field.validation_alias or field.serialization_alias
        for field in model.model_fields.values()
    ):
        raise ValueError("通用参数契约暂不支持字段别名，请直接使用字段名称")
    properties = model.model_json_schema().get("properties", {})
    if any(
        v.get("type") not in {"number", "integer", "boolean", "string"} for v in properties.values()
    ):
        raise ValueError("通用参数编辑器目前支持平铺的数字、整数、布尔及字符串参数")


def validate_algorithm_options(config, definitions):
    """每个算法用自己的 Pydantic 参数模型校验，拒绝未选中项和未知参数。"""
    library = {item.key: item for item in definitions}
    if config.algorithmOptions.keys() - library.keys():
        raise ValueError("算法参数包含未加入当前对比的算法")
    options = {}
    for key, definition in library.items():
        impl = _IMPLEMENTATIONS.get(definition.baseKey or key)
        values = config.algorithmOptions.get(key, {})
        parameter_model = impl.parameter_model if impl and not definition.external else None
        if definition.package:
            from common.kiln_algorithm_packages import parameter_model as package_options

            parameter_model = package_options(definition.package)
        if parameter_model is None:
            if values:
                raise ValueError(f"算法 {key} 未提供可配置参数契约")
            continue
        try:
            options[key] = parameter_model.model_validate(values).model_dump(mode="json")
            json.dumps(options[key], allow_nan=False)
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"算法 {key} 参数无效：{exc}") from exc
    return config.model_copy(update={"algorithmOptions": options})


def prediction_offsets(artifact):
    impl = _IMPLEMENTATIONS.get(artifact.get("implementationKey", artifact["algorithm"]))
    if impl and impl.predict_path:
        horizon = artifact["config"]["horizonMinutes"]
        weights = artifact.get("model")
        offsets = weights.get("offsets", [horizon]) if isinstance(weights, dict) else [horizon]
        if (
            not isinstance(offsets, list)
            or any(type(step) is not int or not 0 < step <= horizon for step in offsets)
            or sorted(set(offsets)) != offsets
            or horizon not in offsets
        ):
            raise ValueError("多时距输出 offsets 须为升序、不重复的正整数，并包含原生预测时距")
        return offsets
    heads = artifact.get("minuteHeads", artifact.get("temperatureHeads", {}))
    return sorted({*(int(step) for step in heads), artifact["config"]["horizonMinutes"]})


def _check_algorithm_version(artifact):
    key = artifact.get("implementationKey", artifact["algorithm"])
    definition = _ALGORITHMS_BY_KEY.get(key)
    if (
        key not in _BUILTIN_KEYS
        and definition
        and artifact.get("algorithmVersion") != definition.version
    ):
        raise ValueError(f"算法 {key} 的历史实现版本不匹配，请保留旧键和实现后再回放")


def predict_path(artifact, example):
    _check_algorithm_version(artifact)
    impl = _IMPLEMENTATIONS.get(artifact.get("implementationKey", artifact["algorithm"]))
    if not impl or not impl.predict_path:
        return {}
    values = impl.predict_path(artifact, example)
    if not isinstance(values, dict) or any(type(step) is not int for step in values):
        raise ValueError("多时距推理须返回整数分钟到预测值的映射")
    allowed = prediction_offsets(artifact)
    return {
        minute: max(0, value) if artifact["config"]["task"] in {"energy", "afr"} else value
        for minute, value in values.items()
        if minute in allowed and value is not None and math.isfinite(value)
    }


def algorithm_enabled(key):
    item = _ALGORITHMS_BY_KEY.get(key)
    return item is not None and item.status == "enabled"


def algorithm_unavailable_reason(key, task, template_keys, missing_plan=False, *, package=None):
    if package:
        return "" if task in package.tasks else "算法包不支持当前预测任务"
    impl = _IMPLEMENTATIONS.get(key)
    if impl is None:
        return "算法实现未注册"
    if task not in impl.tasks:
        return "算法不支持当前预测任务"
    check = impl.unavailable
    return check(task, template_keys, missing_plan) if check else ""


def predict_model(artifact, ex):
    if artifact.get("algorithmPackage"):
        from common.kiln_algorithm_packages import predict_package

        return predict_package(artifact, [ex], each=True)[0]
    _check_algorithm_version(artifact)
    key = artifact.get("implementationKey", artifact["algorithm"])
    task = artifact["config"]["task"]
    if key == "onnx":
        value = external_predictions(artifact["model"], [ex])[0]
    else:
        implementation = _IMPLEMENTATIONS.get(key)
        if implementation is None:
            raise ValueError(f"不支持的算法产物：{key}")
        value = implementation.predict(artifact, ex)
    if value is None or not math.isfinite(float(value)):
        return None
    return float(max(0, value)) if task in {"energy", "afr"} else float(value)


def predict_many(artifact, rows):
    if artifact.get("algorithmPackage"):
        from common.kiln_algorithm_packages import predict_package

        return predict_package(artifact, rows)
    _check_algorithm_version(artifact)
    key = artifact.get("implementationKey", artifact["algorithm"])
    impl = _IMPLEMENTATIONS.get(key)
    if not impl or not impl.predict_many:
        return [predict_model(artifact, row) for row in rows]
    values = impl.predict_many(artifact, rows)
    if len(values) != len(rows):
        raise ValueError("算法批量输出长度与输入不一致")
    nonnegative = artifact["config"]["task"] in {"energy", "afr"}
    return [
        None
        if value is None or not math.isfinite(value)
        else max(0, value)
        if nonnegative
        else value
        for value in values
    ]


def cumulative_volumes(rows, values):
    """Integrate contiguous valid segments; never bridge cycles or long gaps."""
    totals = []
    total, hours = 0.0, 0.0
    steps = [
        (right["time"] - left["time"]) / 60
        for left, right in zip(rows, rows[1:], strict=False)
        if left["cycle"] == right["cycle"] and right["time"] > left["time"]
    ]
    max_gap = float(np.median(steps)) * 1.5 if steps else 0
    for i, (row, value) in enumerate(zip(rows, values, strict=True)):
        if i and row["cycle"] == rows[i - 1]["cycle"]:
            dt = (row["time"] - rows[i - 1]["time"]) / 60
            if 0 < dt <= max_gap and value is not None and values[i - 1] is not None:
                total += (value + values[i - 1]) * dt / 2
                hours += dt
        totals.append(total)
    return totals, hours


def metrics(rows, predictions, task=None):
    result = error_statistics([row["y"] for row in rows], predictions)
    if not result["count"]:
        return result
    if task == "energy":
        actual, hours = cumulative_volumes(
            rows,
            [
                row["y"] if value is not None else None
                for row, value in zip(rows, predictions, strict=True)
            ],
        )
        predicted, _ = cumulative_volumes(rows, predictions)
        result.update(
            windowActualNm3=actual[-1],
            windowPredictedNm3=predicted[-1],
            totalErrorPct=(predicted[-1] - actual[-1]) / actual[-1] * 100
            if actual[-1] > 0
            else None,
            coveredHours=hours,
        )
    return result


def run_experiment(
    cycles,
    config: ExperimentRequest,
    parameters,
    template_keys,
    definitions=None,
    external_models=None,
):
    if config.zone and config.task != "temperature":
        raise ValueError("温区选择仅用于温度任务")
    if definitions is None:
        library = {item.key: item for item in algorithm_library(enabled_only=True)}
        requested = config.selectedAlgorithmKeys or [key for key in library if key != "lstm"]
        if any(key not in library for key in requested):
            raise ValueError("对比列表中存在不可用的算法")
        definitions = [library[key] for key in requested]
    config = validate_algorithm_options(config, definitions)
    samples = examples(
        cycles,
        config,
        parameters["minGas"],
        include_sequence=any(
            bool(impl and impl.sequence)
            for item in (definitions or [])
            for impl in [_IMPLEMENTATIONS.get(item.baseKey or item.key)]
        ),
        include_trajectory=config.task in {"temperature", "energy"},
    )
    splits, method = split_examples(samples)
    train, validation, test = splits
    p, calibration = calibrate(train, parameters, config)
    prep = fit_preprocessor([e["x"] for e in train])
    x = transform([e["x"] for e in train], prep)
    y = np.array([e["y"] for e in train])
    trained = {}
    cards = []
    preds = {}
    missing_plan = any(e["sp0"] is None or e["sp1"] is None for e in samples)
    validation_start = min(example["originTime"] for example in validation + test)
    for definition in definitions:
        key = definition.key
        external = definition.external
        implementation_key = "onnx" if external else definition.baseKey or key
        implementation = _IMPLEMENTATIONS.get(implementation_key)
        note = (
            external_config_issue(external, config, validation_start)
            if external
            else algorithm_unavailable_reason(
                implementation_key, config.task, template_keys, missing_plan, package=definition.package
            )
        )
        artifact = {
            "algorithm": key,
            "algorithmVersion": definition.version,
            "config": config.model_dump(),
            "parameters": p,
            "preprocessor": prep,
            "features": FEATURES,
            "model": None,
        }
        fit_config = config
        if definition.tuning:
            tuning = definition.tuning.model_dump(exclude_none=True)
            artifact.update(implementationKey=implementation_key, tuning=tuning)
            fit_config = config.model_copy(update=tuning)
        if not note and external:
            encoded = (external_models or {}).get(key)
            if not encoded:
                note = "外部 ONNX 模型产物不可读取"
            else:
                model = {
                    "onnx": encoded,
                    "featureIndices": external.featureIndices,
                    "inputName": external.inputName,
                    "outputName": external.outputName,
                    "inputType": external.inputType,
                    "sha256": external.sha256,
                }
                artifact.update(implementationKey=implementation_key, model=model)
                try:
                    values = external_predictions(model, validation + test)
                    if config.task in {"energy", "afr"}:
                        values = [max(0, value) if value is not None else None for value in values]
                    pv, pt = values[: len(validation)], values[len(validation) :]
                    if not any(value is not None for value in pt):
                        note = "外部模型在测试窗口没有有效预测，请核对特征覆盖和输出"
                except ValueError as exc:
                    note = str(exc)
        elif not note:
            if definition.package:
                from common.kiln_algorithm_packages import fit_package

                artifact["algorithmPackage"] = definition.package.model_dump(mode="json")
                artifact["model"] = fit_package(definition, x, y, fit_config, p, train)
            elif implementation and implementation.fit:
                artifact["model"] = implementation.fit(x, y, fit_config, p, train)
            try:
                json.dumps(artifact, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"算法 {key} 的训练产物必须是有限数值、可 JSON 序列化的对象"
                ) from exc
            pv = predict_many(artifact, validation)
            pt = predict_many(artifact, test)
        if not note:
            from common.kiln_studio_temperature_path import fit_temperature_heads

            heads = fit_temperature_heads(train, fit_config, p, implementation_key)
            if heads:
                artifact["minuteHeads" if config.task == "energy" else "temperatureHeads"] = heads
            calibration_errors = sorted(
                abs(value - row["y"])
                for row, value in zip(validation, pv, strict=True)
                if value is not None
            )
            if len(calibration_errors) >= 10:
                quantile = min(
                    len(calibration_errors) - 1,
                    math.ceil((len(calibration_errors) + 1) * 0.9) - 1,
                )
                artifact["predictionInterval"] = {
                    "radius": calibration_errors[quantile],
                    "level": 0.9,
                    "calibrationCount": len(calibration_errors),
                    "method": "split-conformal",
                }
            durations = []
            benchmark = test[: min(30, len(test))]
            if not definition.package:
                for sample in benchmark:
                    started = perf_counter()
                    predict_model(artifact, sample)
                    durations.append((perf_counter() - started) * 1000)
            started = perf_counter()
            predict_many(artifact, benchmark)
            inference_benchmark = {
                "environment": "container-cpu" if definition.package else "server-cpu",
                "count": len(benchmark),
                "maxMs": max(durations, default=None),
                "meanMs": sum(durations) / len(durations) if durations else None,
                "batchMs": (perf_counter() - started) * 1000,
            }
            trained[key] = artifact
            preds[key] = pv + pt
            card = {
                **definition.model_dump(mode="json"),
                "available": True,
                "note": note,
                "validation": metrics(validation, pv, config.task),
                "test": metrics(test, pt, config.task),
                "predictionInterval": artifact.get("predictionInterval"),
                "inferenceBenchmark": inference_benchmark,
                "predictionOffsets": prediction_offsets(artifact),
                "trainingSummary": {
                    key: artifact["model"][key]
                    for key in (
                        "trainingCount",
                        "availableTrainingCount",
                        "epochs",
                        "hiddenSize",
                        "sequenceLength",
                    )
                }
                if key == "lstm"
                else None,
            }
            if config.task == "energy":
                for split in ("validation", "test"):
                    if "windowPredictedNm3" in card[split]:
                        card[split]["fuelEnergyMJ"] = card[split]["windowPredictedNm3"] * p["lhvMJ"]
        else:
            card = {
                **definition.model_dump(mode="json"),
                "available": False,
                "note": note,
                "validation": None,
                "test": None,
            }
        cards.append(card)
    rows = validation + test
    cumulatives = {}
    if config.task == "energy":
        for key, values in {"actual": [e["y"] for e in rows], **preds}.items():
            cv, _ = cumulative_volumes(validation, values[: len(validation)])
            ct, _ = cumulative_volumes(test, values[len(validation) :])
            cumulatives[key] = cv + ct
    stride = max(1, math.ceil(len(rows) / 900))
    evaluation_rows = [
        {
            "ts": e["ts"],
            "origin": e["origin"],
            "cycle": e["cycle"],
            "split": "validation" if i < len(validation) else "test",
            "actual": e["y"],
            **(
                {"actualTemperatureSource": e["actualTemperatureSource"]}
                if e.get("actualTemperatureSource")
                else {}
            ),
            "setpoint": e["sp1"],
            "cumulative": {key: values[i] for key, values in cumulatives.items()},
            **{key: values[i] for key, values in preds.items()},
        }
        for i, e in enumerate(rows)
    ]
    points = [point for i, point in enumerate(evaluation_rows) if i % stride == 0]
    summaries = [
        {
            "name": name,
            "count": len(group),
            "from": min(e["origin"] for e in group),
            "to": max(e["ts"] for e in group),
            "cycles": sorted({e["cycle"] for e in group}),
        }
        for name, group in zip(("训练", "验证", "测试"), splits, strict=False)
    ]
    warnings = [
        "曲线是固定预测时长的滚动回测；每个预测起点只读取当时及之前观测和已知设定计划",
        "用验证集调参；依据测试结果选型后，请另留独立窗口做最终核验",
        "预测范围仅使用验证记录标定，在测试记录上核对覆盖率；预测差值需经现场验证",
    ]
    if "同窑次" in method:
        warnings.append("未实现跨窑次独立检验：结果仅为同窑次时间留出验证")
    if prep["missingColumns"]:
        warnings.append("训练中全缺失特征按零常量处理：" + "、".join(prep["missingColumns"]))
    if config.task == "afr":
        warnings.append("仅预测配比，不输出最优控制指令；停火与低燃气流量区间不计入AFR指标")
    return {
        "config": config.model_dump(),
        "parameters": p,
        "calibration": calibration,
        "algorithms": cards,
        "points": points,
        "evaluationRows": evaluation_rows,
        "observationQuality": observation_quality(cycles, config.excludeZeroTemperatureRecords),
        "maxGapMinutes": {
            cycle["id"]: float(np.median(np.diff([row["time"] for row in cycle["points"]]))) * 1.5
            for cycle in cycles
            if len(cycle["points"]) > 1
        },
        "split": summaries,
        "splitMethod": method,
        "featureNames": FEATURES,
        "warnings": warnings,
        "unit": {"temperature": "℃", "energy": "Nm³/h", "afr": "Nm³/Nm³"}[config.task],
        "targetKey": target_key(config),
        "artifacts": trained,
        "engineVersion": "kiln-studio-1.0",
    }


def simulate(cycles, models, fraction, offset):
    cycle = max(cycles, key=lambda c: c["points"][0]["time"])
    rows = cycle["points"]
    index = max(1, min(len(rows) - 2, int((len(rows) - 1) * fraction)))
    history = rows[: index + 1]
    outputs = []
    for artifact in models:
        config = ExperimentRequest.model_validate(artifact["config"])
        rows = clean_rows(
            cycle["points"],
            afr_floor=artifact["parameters"]["minGas"] if config.task == "afr" else None,
            exclude_zero_temperature=config.excludeZeroTemperatureRecords,
        )
        history = rows[: index + 1]
        endtime = history[-1]["time"] + config.horizonMinutes
        times = [p["time"] for p in rows]
        j = bisect.bisect_left(times, endtime)
        if j >= len(rows):
            raise ValueError("当前时刻剩余计划不足模型预测时长")
        future = rows[j]
        if future["time"] - history[-1]["time"] > config.horizonMinutes + 5:
            raise ValueError("预测时刻的设定计划缺测，无法对齐保存模型的预测时长")
        sp = future.get("temp_sp")
        if offset and sp is not None and not 0 <= sp + offset <= 1800:
            raise ValueError("调整后的设定温度超出本求解器0–1800℃范围")
        if config.task == "afr" and (
            history[-1].get("gas") is None or history[-1]["gas"] < artifact["parameters"]["minGas"]
        ):
            outputs.append(
                {
                    "task": config.task,
                    "algorithm": artifact["algorithm"],
                    "baseline": None,
                    "adjusted": None,
                    "note": "当前燃气低于有效阈值，空燃比不定义",
                }
            )
            continue
        if offset and config.task == "afr":
            outputs.append(
                {
                    "task": config.task,
                    "algorithm": artifact["algorithm"],
                    "baseline": None,
                    "adjusted": None,
                    "note": "空燃比模型未验证温度干预响应，不计算温度偏移的反事实效果",
                }
            )
            continue
        if offset and artifact["algorithm"] not in {"physics", "fusion"}:
            outputs.append(
                {
                    "task": config.task,
                    "algorithm": artifact["algorithm"],
                    "baseline": None,
                    "adjusted": None,
                    "note": (
                        "纯数据及持续值模型未验证温度干预响应，请选择机理或融合算法进行场景试算"
                    ),
                }
            )
            continue
        if offset and sp is None:
            raise ValueError("缺少未来温度计划，不能试算温度偏移")
        model_history = history
        if config.task == "afr":
            model_history = [
                {
                    **row,
                    "afr": row["air"] / row["gas"]
                    if row.get("air") is not None
                    and row.get("gas") is not None
                    and row["gas"] >= artifact["parameters"]["minGas"]
                    else None,
                }
                for row in history
            ]
        ex = make_features(
            model_history, sp, config, include_sequence=model_uses_sequence(artifact)
        )
        altered = make_features(
            model_history,
            sp + offset if sp is not None else None,
            config,
            include_sequence=model_uses_sequence(artifact),
        )
        if not ex:
            raise ValueError("当前时刻缺少目标值或足够历史窗口")
        base, changed = predict_model(artifact, ex), predict_model(artifact, altered)
        outputs.append(
            {
                "task": config.task,
                "algorithm": artifact["algorithm"],
                "baseline": base,
                "adjusted": changed,
                "unit": {"temperature": "℃", "energy": "Nm³/h", "afr": "Nm³/Nm³"}[config.task],
                "horizonMinutes": config.horizonMinutes,
                "note": "机理假设下的场景响应，尚未验证实际干预效果"
                if offset
                else "固定模型版本的起点预测",
            }
        )
    return {
        "cursor": history[-1]["ts"],
        "cycle": cycle["id"],
        "temperatureOffsetC": offset,
        "outputs": outputs,
        "scope": (
            "仅使用起点及以前实测和未来已知设定计划；不读取未来实测特征。各任务独立预测，尚未串联。"
        ),
    }


# API 与后台任务均导入此引擎，受控扩展在函数定义完成后统一注册。
install_algorithms()
