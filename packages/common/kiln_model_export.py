"""数值、Skill 和 Python 三种导出格式，转换函数不查询数据库、不修改模型。"""

import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from common.errors import AppError, ErrorCode
from common.kiln_export_runtime import PYTHON_RUNTIME

PORTABLE_ALGORITHMS = {"baseline", "physics", "ridge", "lasso", "boosting", "fusion", "lstm"}
TASK_NAMES = {"temperature": "温度", "energy": "燃气流量", "afr": "空燃比"}


def export_numeric_model(bundle, models, dataset_config):
    # 此结构是现有第五步导出的公开契约，禁止添加格式专属字段。
    instructions = (
        "使用 common.kiln_studio_engine.predict_model；输入特征顺序在各模型features内；"
        "观测仅限预测起点及以前，设定计划是已知输入。"
    )
    if any(model.get("algorithmPackage") for model in models):
        instructions += "上传算法包模型需要原始 ZIP 和固定的隔离执行镜像，导出 JSON 仅携带权重与版本引用。"
    return {
        "format": "kiln-studio-numeric-model-v1",
        "bundle": bundle,
        "models": models,
        "datasetConfig": dataset_config,
        "engineVersion": "kiln-studio-1.0",
        "replayInstructions": instructions,
    }


def _input_fields(model, unit):
    units = [unit] * 4 + ["℃", "Nm³/h", "Nm³/h", "%", "Pa", "℃", "℃", "℃/h"]
    return [
        {
            "name": name,
            "unit": units[index] if index < len(units) else None,
            "meaning": name,
            "position": index,
            "type": "number|null",
        }
        for index, name in enumerate(model["features"])
    ]


def _model_contract(model, manifest, index):
    config = model["config"]
    result = {
        "modelIndex": index,
        "algorithm": model["algorithm"],
        "algorithmName": manifest["algorithmName"],
        "task": config["task"],
        "horizonMinutes": config["horizonMinutes"],
        "historyMinutes": config["historyMinutes"],
        "inputs": _input_fields(model, manifest["unit"]),
        "output": {
            "name": "value",
            "unit": manifest["unit"],
            "meaning": (
                f"预测起点之后 {config['horizonMinutes']} 分钟的{TASK_NAMES[config['task']]}"
            ),
        },
    }
    from common.kiln_studio_engine import algorithm_implementation

    impl = algorithm_implementation(model.get("implementationKey", model["algorithm"]))
    if impl and impl.sequence:
        target = (
            f"z{config['zone']}_temp"
            if config["task"] == "temperature" and config.get("zone")
            else {"temperature": "temp", "energy": "gas", "afr": "afr"}[config["task"]]
        )
        result["sequence"] = {
            "length": model["model"]["sequenceLength"],
            "channels": [target, "temp", "temp_sp", "gas", "air", "o2", "pressure"],
            "meaning": "从历史窗口按时间均匀选取的原始测点，归一化由推理脚本完成",
        }
    return result


def export_skill(data):
    bundle = data["bundle"]
    contracts = [
        _model_contract(model, manifest, index)
        for index, (model, manifest) in enumerate(
            zip(data["models"], bundle["manifest"], strict=True)
        )
    ]
    capabilities = [
        f"{item['algorithmName']}：{TASK_NAMES[item['task']]}，提前 {item['horizonMinutes']} 分钟"
        for item in contracts
    ]
    return {
        "format": "kiln-studio-ai-skill-v1",
        "name": bundle["title"],
        "version": bundle["version"],
        "description": "；".join(capabilities),
        "capabilities": capabilities,
        "inputs": contracts,
        "outputs": [{"modelIndex": item["modelIndex"], **item["output"]} for item in contracts],
        "invocation": {
            "entrypoint": "common.kiln_studio_engine.predict_model",
            "instructions": data["replayInstructions"],
            "featureOrder": (
                "按各模型 features 顺序构造 x；y0、sp0、sp1、slope 对应第 0、9、10、11 项"
            ),
            "deployment": "这是调用规格及完整数值模型，不包含已部署的 HTTP 服务或第三方平台注册",
        },
        "modelPackage": data,
    }


def validate_python_models(data):
    unsupported = [
        model.get("implementationKey", model["algorithm"])
        for model in data["models"]
        if model.get("implementationKey", model["algorithm"]) not in PORTABLE_ALGORITHMS
    ]
    if unsupported:
        raise AppError(
            ErrorCode.VALIDATION,
            "仅依赖 NumPy 的离线包暂不支持这些实现：" + "、".join(sorted(set(unsupported))),
            422,
        )
    if not data["models"]:
        raise AppError(ErrorCode.VALIDATION, "当前模型包没有可导出的模型", 422)
    for model in data["models"]:
        if len(model.get("features", [])) != 12:
            raise AppError(ErrorCode.VALIDATION, "模型特征结构与当前离线推理契约不匹配", 422)


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def export_python(data, example):
    validate_python_models(data)
    bundle = data["bundle"]
    contracts = export_skill(data)["inputs"]
    readme = f"""# {bundle["title"]} · {bundle["version"]}

来源：项目 {bundle["projectId"]}，模型组合 {bundle["id"]}。
引擎：{data["engineVersion"]}。

## 运行

仅依赖 Python 和 NumPy，不需要安装本平台、pandas、scikit-learn 或 ONNX Runtime。
在本目录执行（Windows PowerShell）：

```powershell
python -m pip install -r requirements.txt
python predict.py
python predict.py --verify
python predict.py --input my_input.json
```

脚本默认读取同目录 model.json 和 input_example.json。
input_example.json 来自此模型绑定数据版本的真实历史窗口；expected 是平台 predict_model
对该输入的计算值，供 --verify 核对，不是精度指标。模型已保存的验证和测试指标在 model.json 中。

## 输入与输出

输入顶层为 inputs 数组，每项含 modelIndex 和 features 对象。features 的键名必须与对应模型
features 数组中的名称一致，脚本按模型顺序取值；缺测使用 null，不能遗漏键或填充未来实测。
LSTM 还需要 sequence，通道与形状见下方真实模型规格。示例 origin 标记预测起点，
推理仅使用 features 和 sequence；已知未来设定属于计划输入。
输出为各子模型的 value、unit、task、horizonMinutes。

本脚本与平台 predict_model 一致，默认计算各子模型的原生时距终点。
minuteHeads / temperatureHeads 及其他完整权重保留在 model.json 中；本脚本不编排滚动回放、
自动构建历史特征或逐分钟递归曲线，也不连接现场设备。
若修改示例输入，应移除 expected 或不使用 --verify。

## 模型输入规格

```json
{_json(contracts)}```
"""
    return {
        "format": "kiln-studio-python-package-v1",
        "name": bundle["title"],
        "version": bundle["version"],
        "files": [
            {"path": "model.json", "content": _json(data)},
            {"path": "predict.py", "content": PYTHON_RUNTIME},
            {"path": "README.md", "content": readme},
            {"path": "input_example.json", "content": _json(example)},
            {"path": "requirements.txt", "content": "numpy\n"},
        ],
    }


def zip_python_files(package):
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        for file in package["files"]:
            info = ZipInfo(file["path"])
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, file["content"].encode("utf-8"))
    return stream.getvalue()
