"""算法库能力来源于执行器和转换器，不由名称或前端枚举推断。"""

from importlib.util import find_spec

from common.kiln_studio_schema import AlgorithmCapabilities


def _capability(status, detail):
    return {"status": status, "detail": detail}


def describe_algorithm(definition):
    from common.kiln_model_export import PORTABLE_ALGORITHMS
    from common.kiln_onnx_graph import has_onnx_converter
    from common.kiln_studio_engine import algorithm_implementation

    external = definition.external
    key = definition.baseKey or definition.key
    impl = algorithm_implementation(key) if not external else None
    source = "external" if external else "preset" if definition.baseKey else "builtin"
    mode = "frozen" if external else "trainable" if impl and impl.fit else "calculated"
    training = {
        "frozen": "使用上传的冻结模型进行评估，平台不会重新训练。",
        "trainable": "通过已注册的 fit 实现，在隔离的训练窗口上训练并保存权重。",
        "calculated": "按当前读数或已保存机理参数直接计算，不训练新的统计权重。",
    }[mode]
    if impl and impl.predict_path:
        minute = _capability("conditional", "已注册多时距推理；实际分钟覆盖以本次训练结果为准。")
    elif key in {"ridge", "fusion"} and not external:
        minute = _capability(
            "conditional", "温度/燃气任务可训练分钟预测头，须有足够样本；以实际输出覆盖为准。"
        )
    elif key in {"physics", "baseline"} and not external:
        minute = _capability(
            "conditional", "回放可按机理或持续值生成逐分钟曲线，不代表逐分钟训练模型。"
        )
    else:
        minute = _capability("unsupported", "当前实现只声明原生预测时距终点，不插值冒充分钟预测。")
    if not external and has_onnx_converter(key):
        installed = find_spec("onnx") is not None
        onnx = _capability(
            "supported" if installed else "conditional",
            "转换器已注册；导出时仍须逐点核对。"
            if installed
            else "转换器已注册，但服务端需安装 onnx-export 可选依赖。",
        )
    else:
        onnx = _capability("unsupported", "尚未注册该实现的通用 ONNX 交付转换器。")
    python = _capability(
        "supported" if not external and key in PORTABLE_ALGORITHMS else "unsupported",
        "已接入独立 NumPy 运行模板。"
        if not external and key in PORTABLE_ALGORITHMS
        else "独立 NumPy 运行模板尚未适配该实现。",
    )
    capabilities = AlgorithmCapabilities(
        trainingMode=mode,
        trainingDetail=training,
        supportedTasks=[external.task] if external else list(impl.tasks) if impl else [],
        inference=_capability(
            "supported" if external or impl else "unsupported",
            "外部 ONNX 已通过导入试跑。"
            if external
            else "真实推理回调已注册。"
            if impl
            else "未注册推理实现。",
        ),
        minuteForecast=minute,
        onnxExport=onnx,
        pythonExport=python,
    )
    return definition.model_copy(
        update={
            "sourceKind": source,
            "capabilities": capabilities,
            "parameterSchema": impl.parameter_model.model_json_schema()
            if impl and impl.parameter_model
            else None,
        }
    )
