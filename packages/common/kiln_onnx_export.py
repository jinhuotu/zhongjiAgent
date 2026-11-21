"""通用 ONNX 交付包：转换、真实历史逐点核对、清单及独立运行程序。"""

import base64
import hashlib
import json
import platform
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np

from common import kiln_onnx_preprocess as preprocessing
from common.errors import AppError, ErrorCode
from common.kiln_model_export import export_skill
from common.kiln_onnx_examples import model_samples
from common.kiln_onnx_graph import build_onnx, implementation, output_offsets, validate_models
from common.kiln_onnx_runtime import RUNNER_RUNTIME

TOLERANCE = {"atol": 0.001, "rtol": 0.00001}


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _spec(model, contract, index, blob, zone_count):
    offsets = output_offsets(model)
    inputs = [{"name": "features", "dtype": "float32", "shape": ["batch", 12]}]
    if "sequence" in contract:
        shape = ["batch", contract["sequence"]["length"], len(contract["sequence"]["channels"])]
        inputs.append({"name": "sequence", "dtype": "float32", "shape": shape})
    elif len(offsets) > 1:
        inputs.append({"name": "futurePlans", "dtype": "float32", "shape": ["batch", len(offsets)]})
    cfg = model["config"]
    target = (
        f"z{cfg['zone']}_temp"
        if cfg["task"] == "temperature" and cfg.get("zone")
        else {"temperature": "temp", "energy": "gas", "afr": "afr"}[cfg["task"]]
    )
    return {
        "modelIndex": index,
        "file": f"models/model-{index}.onnx",
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob),
        "algorithm": model["algorithm"],
        "implementation": implementation(model),
        "algorithmName": contract["algorithmName"],
        "config": cfg,
        "features": model["features"],
        "inputFields": contract["inputs"],
        "inputs": inputs,
        "output": {"name": "predictions", "shape": ["batch", len(offsets)]},
        "offsetsMinutes": offsets,
        "unit": contract["output"]["unit"],
        "targetSignal": target,
        "zoneCount": zone_count,
        "minGas": model["parameters"]["minGas"],
        **({"sequence": contract["sequence"]} if "sequence" in contract else {}),
    }


def _verify(blob, spec, samples):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    session = ort.InferenceSession(blob, sess_options=options, providers=["CPUExecutionProvider"])
    results, prepared = [], []
    for sample in samples:
        feed = preprocessing.prepare(sample, spec)
        actual = session.run(["predictions"], feed)[0][0]
        expected = np.asarray(sample["expectedValues"], dtype=float)
        # 另外核对独立历史特征构造，防止只验证已准备特征却交付了不同的窗口规则。
        historical = {k: v for k, v in sample.items() if k not in {"features", "sequence"}}
        from_history = preprocessing.prepare(historical, spec)
        if any(not np.array_equal(feed[key], from_history[key], equal_nan=True) for key in feed):
            raise ValueError("独立历史特征构造与平台不一致")
        if (
            actual.shape != expected.shape
            or not np.isfinite(actual).all()
            or not np.allclose(actual, expected, **TOLERANCE)
        ):
            raise ValueError(
                f"{spec['algorithm']} ONNX 与平台逐点核对失败，起点 {sample['origin']}"
            )
        results.append(actual)
        prepared.append(feed)
    # 同时验证动态批次，输出不能因其他记录同批执行而改变。
    batch = {key: np.concatenate([feed[key] for feed in prepared]) for key in prepared[0]}
    combined = session.run(["predictions"], batch)[0]
    if not np.allclose(combined, results, **TOLERANCE):
        raise ValueError("ONNX 批次一致性核对失败")
    differences = np.abs(np.asarray(results) - [s["expectedValues"] for s in samples])
    return {
        "modelIndex": spec["modelIndex"],
        "sampleCount": len(samples),
        "comparedValues": int(differences.size),
        "offsetsMinutes": spec["offsetsMinutes"],
        "maxAbsoluteDifference": float(differences.max()),
        "perOffsetMaxAbsoluteDifference": differences.max(axis=0).tolist(),
        "windows": [{"cycleId": s["cycleId"], "origin": s["origin"]} for s in samples],
        "unit": spec["unit"],
        "providers": session.get_providers(),
        "status": "passed",
    }


def _readme(manifest):
    lines = [
        f"- {s['algorithmName']} / {s['config']['task']}："
        f"输出第 {s['offsetsMinutes']} 分钟，单位 {s['unit']}。"
        for s in manifest["models"]
    ]
    return f"""# 通用 ONNX FP32 部署包 · {manifest["bundleVersion"]}

源模型：{manifest["bundleTitle"]}；组合 ID：{manifest["bundleId"]}。

## 快速运行（Windows PowerShell / Linux shell）

```text
python -m pip install -r requirements.txt
python run.py --verify
python run.py --input sensor_input_example.json --verify
python run.py --benchmark 100
```

运行只需要 NumPy、ONNX Runtime，不需要本平台、ONNX 构图包或训练框架。
models/ 中每个文件均为真实 ONNX（opset 13、IR 8、FP32），含预处理标准化、缺测填充、
LSTM 门计算、输出还原和燃气/空燃比非负约束；模型输入可为动态批次。

## 真实模型输出

{chr(10).join(lines)}

只输出已保存的预测时距，不通过插值补齐缺失预测。LSTM 保留全部 offsets；
带完整分钟预测头的 Ridge/Fusion 也保留完整曲线，其他模型仅输出原生时距终点。
不同子模型分别运行，不把一个子模型预测作为另一个模型的输入。

## 两种输入

input_example.json 是来自绑定数据的准备后特征；键名和顺序见 manifest.json。
sensor_input_example.json 包含预测起点及以前的真实历史记录和已知未来温度设定计划。
history.time 是同一窑次内的分钟数，按时间排序；最后一行是预测起点。
history 必须覆盖 historyMinutes，并保留窗口前一个点；代码依照平台窗口规则抽取序列，
序列长度不等于分钟数。非温度测点、异常值、零温记录按保存的配置处理。
futurePlans 是第 1..horizonMinutes 分钟的已知温度设定，不得填写未来实测。
features/sequence 输入使用原始量纲，禁止先手工标准化；图内会使用训练时保存的参数。

输出 values 与 offsetsMinutes 一一对应。expectedValues 是平台对此输入的预测值，
用于 --verify，不是未来实测值，也不是模型精度。修改输入后不要再使用旧核对值。

## 核对与部署边界

validation.json 记录生成本包时的真实历史窗口核对结果、每个输出时距的最大数值差异、
运行环境及绝对/相对容差。FP32 不保证与平台 NumPy 浮点运算逐位相同；业务精度见源模型测试指标。
--benchmark 只测当前设备已加载 CPU 会话的推理延迟，不含历史预处理、加载或网络。
本包没有执行 INT8 量化、没有 TensorRT 引擎，也没有目标边缘硬件性能认证。
CPU 运行时及其依赖需匹配设备 OS/CPU/Python；当前验证环境不代表已验证所有 x86/ARM 设备。
不连接现场设备，也不自动下发控制指令。超过模型原生时距的滚动预测由调用方编排。
"""


def export_onnx(data, cycles):
    try:
        import onnx
        import onnxruntime as ort
    except ImportError as exc:
        raise AppError(
            ErrorCode.VALIDATION, "ONNX 导出依赖未安装，请安装项目 onnx-export 可选依赖", 503
        ) from exc
    validate_models(data)
    contracts = export_skill(data)["inputs"]
    manifest = {
        "format": "kiln-studio-onnx-package-v1",
        "bundleId": data["bundle"]["id"],
        "bundleTitle": data["bundle"]["title"],
        "bundleVersion": data["bundle"]["version"],
        "engineVersion": data["engineVersion"],
        "precision": "FP32",
        "opset": 13,
        "irVersion": 8,
        "verificationTolerance": TOLERANCE,
        "models": [],
        "targetHardwareVerified": False,
        "quantized": False,
        "tensorRTBuilt": False,
    }
    files, examples, sensor_examples, reports = [], [], [], []
    for index, (model, contract) in enumerate(zip(data["models"], contracts, strict=True)):
        try:
            blob = build_onnx(model)
            spec = _spec(model, contract, index, blob, data["datasetConfig"].get("zoneCount", 0))
            samples = model_samples(model, cycles, spec["zoneCount"])
            report = _verify(blob, spec, samples)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(ErrorCode.VALIDATION, f"ONNX 转换或核对失败：{exc}", 422) from exc
        manifest["models"].append(spec)
        reports.append(report)
        examples.append(
            {"modelIndex": index, **{k: v for k, v in samples[0].items() if k != "history"}}
        )
        sensor_examples.append(
            {
                "modelIndex": index,
                **{k: v for k, v in samples[0].items() if k not in {"features", "sequence"}},
            }
        )
        files.append(
            {
                "path": spec["file"],
                "content": base64.b64encode(blob).decode("ascii"),
                "encoding": "base64",
            }
        )
    validation = {
        "status": "passed",
        "scope": "绑定数据的历史窗口及动态批次逐点数值一致性；非业务精度/目标设备认证",
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "numpyVersion": np.__version__,
        "onnxVersion": onnx.__version__,
        "onnxruntimeVersion": ort.__version__,
        "tolerance": TOLERANCE,
        "models": reports,
    }
    text_files = {
        "manifest.json": _json(manifest),
        "validation.json": _json(validation),
        "model.json": _json(data),
        "input_example.json": _json({"inputs": examples}),
        "sensor_input_example.json": _json({"inputs": sensor_examples}),
        "preprocess.py": Path(preprocessing.__file__).read_text(encoding="utf-8"),
        "run.py": RUNNER_RUNTIME,
        "README.md": _readme(manifest),
        "requirements.txt": "numpy>=1.26,<3\nonnxruntime>=1.20,<2\n",
    }
    files.extend({"path": path, "content": content} for path, content in text_files.items())
    return {
        "format": manifest["format"],
        "name": data["bundle"]["title"],
        "version": manifest["bundleVersion"],
        "validation": validation,
        "files": files,
    }


def zip_onnx_files(package):
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for file in package["files"]:
            content = (
                base64.b64decode(file["content"], validate=True)
                if file.get("encoding") == "base64"
                else file["content"].encode("utf-8")
            )
            archive.writestr(file["path"], content)
    return stream.getvalue()
