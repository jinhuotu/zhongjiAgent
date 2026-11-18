"""CPU inference for self-contained ONNX tabular regressors."""

from __future__ import annotations

import base64
import math
from functools import lru_cache

import numpy as np

MAX_EXTERNAL_MODEL_BYTES = 8 * 1024 * 1024
_ALLOWED_NODES = {
    b"": {
        b"Abs",
        b"Add",
        b"Cast",
        b"Clip",
        b"Constant",
        b"Div",
        b"Exp",
        b"Flatten",
        b"Gemm",
        b"Identity",
        b"Log",
        b"MatMul",
        b"Mul",
        b"Neg",
        b"Pow",
        b"ReduceMean",
        b"ReduceSum",
        b"Relu",
        b"Reshape",
        b"Sigmoid",
        b"Sqrt",
        b"Squeeze",
        b"Sub",
        b"Tanh",
        b"Unsqueeze",
    },
    b"ai.onnx.ml": {b"Imputer", b"LinearRegressor", b"Scaler", b"TreeEnsembleRegressor"},
}
_ALLOWED_NODES[b"ai.onnx"] = _ALLOWED_NODES[b""]
_DTYPES = {"tensor(float)": "float32", "tensor(double)": "float64"}


def _read_varint(data, start):
    value = 0
    for shift in range(0, 64, 7):
        if start >= len(data):
            break
        byte = data[start]
        start += 1
        value |= (byte & 127) << shift
        if not byte & 128 and (shift < 63 or byte <= 1):
            return value, start
    raise ValueError("ONNX 文件不完整或格式无效")


def _protobuf_fields(data):
    start = 0
    while start < len(data):
        tag, start = _read_varint(data, start)
        number, wire = divmod(tag, 8)
        if wire == 0:
            value, start = _read_varint(data, start)
        elif wire in (1, 2, 5):
            size, start = _read_varint(data, start) if wire == 2 else ({1: 8, 5: 4}[wire], start)
            end = start + size
            if end > len(data):
                raise ValueError("ONNX 文件不完整")
            value = data[start:end]
            start = end
        else:
            raise ValueError("不支持的 ONNX 编码")
        yield number, wire, value


def _check_tensor(data):
    dims = []
    for number, wire, value in _protobuf_fields(data):
        if number == 13 or (number == 14 and (wire != 0 or value != 0)):
            raise ValueError("请导出权重内嵌的单文件 ONNX")
        if number != 1:
            continue
        if wire == 0:
            dims.append(value)
        if wire == 2:
            offset = 0
            while offset < len(value):
                dim, offset = _read_varint(value, offset)
                dims.append(dim)
                if len(dims) > 4:
                    break
        if len(dims) > 4 or any(dim > 8192 for dim in dims) or math.prod(dims) > 2097152:
            raise ValueError("ONNX 常量张量超出窑炉表格模型的执行范围")


def _check_node(data):
    domain, op = b"", b""
    for number, wire, value in _protobuf_fields(data):
        if wire != 2:
            continue
        if number == 4:
            op = bytes(value)
        elif number == 7:
            domain = bytes(value)
        elif number == 5:
            for field, attr_wire, attr in _protobuf_fields(value):
                if field in (6, 11, 21, 22, 23):
                    raise ValueError("当前导入支持独立的表格回归图")
                if field in (5, 10) and attr_wire == 2:
                    _check_tensor(attr)
    if op not in _ALLOWED_NODES.get(domain, ()):
        raise ValueError(f"当前运行器不支持算子 {op.decode('ascii', errors='replace')[:60]}")


def _check_model(blob):
    graphs = 0
    for field, wire, value in _protobuf_fields(memoryview(blob)):
        if field in (20, 25, 26):
            raise ValueError("请导出单图推理模型")
        if field != 7 or wire != 2:
            continue
        graphs += 1
        for number, graph_wire, item in _protobuf_fields(value):
            if number == 15:
                raise ValueError("当前表格回归模型使用内嵌稠密权重")
            if number == 5 and graph_wire == 2:
                _check_tensor(item)
            if number == 1 and graph_wire == 2:
                _check_node(item)
    if graphs != 1:
        raise ValueError("ONNX 文件必须包含一个推理图")


@lru_cache(maxsize=4)
def _session(encoded):
    import onnxruntime as ort

    blob = base64.b64decode(encoded, validate=True)
    if not blob or len(blob) > MAX_EXTERNAL_MODEL_BYTES:
        raise ValueError("ONNX 文件大小须在 0 到 8 MB 之间")
    _check_model(blob)
    options = ort.SessionOptions()
    options.add_session_config_entry("session.load_model_format", "ONNX")
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    options.enable_mem_pattern = False
    try:
        return ort.InferenceSession(blob, sess_options=options, providers=["CPUExecutionProvider"])
    except Exception as exc:
        raise ValueError("ONNX 文件无法加载，请核对模型格式和导出版本") from exc


def _run(session, input_name, output_name, input_type, rows):
    with np.errstate(over="ignore", invalid="ignore"):
        values = np.asarray(rows, dtype=input_type)
    if not np.isfinite(values).all():
        raise ValueError("输入超出 ONNX 张量的数值范围")
    try:
        result = session.run([output_name], {input_name: values})[0]
    except Exception as exc:
        raise ValueError("ONNX 推理失败，请核对特征顺序和模型输入要求") from exc
    if not isinstance(result, np.ndarray) or result.shape not in {(len(rows),), (len(rows), 1)}:
        raise ValueError("ONNX 必须为每行输入输出一个预测值")
    return [float(value) if math.isfinite(float(value)) else None for value in result.flat]


def validate_external_model(blob, probe):
    session = _session(base64.b64encode(blob).decode("ascii"))
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or len(outputs) != 1:
        raise ValueError("请导出单输入、单输出的 ONNX 回归模型")
    input_tensor, output_tensor = inputs[0], outputs[0]
    input_type = _DTYPES.get(input_tensor.type)
    shape = input_tensor.shape
    if not input_type or not isinstance(shape, list) or len(shape) != 2 or shape[1] != len(probe):
        raise ValueError("ONNX 输入须为二维浮点张量 [批次, 特征数]，特征数与映射一致")
    if isinstance(shape[0], int) and shape[0] != 1:
        raise ValueError("ONNX 输入批次须为动态批次或固定 1")
    if output_tensor.type not in _DTYPES:
        raise ValueError("ONNX 输出须为浮点预测值")
    output = _run(session, input_tensor.name, output_tensor.name, input_type, [probe])[0]
    if output is None:
        raise ValueError("试跑未返回有限数值，请核对输入和模型")
    return {
        "inputName": input_tensor.name,
        "outputName": output_tensor.name,
        "inputType": input_type,
        "probeOutput": output,
    }


def external_config_issue(metadata, config, validation_start=None):
    if metadata.task != config.task or metadata.zone != config.zone:
        return "外部模型的预测任务或温区与当前实验不一致"
    if metadata.horizonMinutes != config.horizonMinutes:
        return f"外部模型要求预测时长 {metadata.horizonMinutes} 分钟"
    if metadata.historyMinutes != config.historyMinutes:
        return f"外部模型要求历史窗口 {metadata.historyMinutes} 分钟"
    if validation_start is not None and metadata.trainedUntil.timestamp() / 60 >= validation_start:
        return "外部模型申报的训练截止时间与当前验证窗口重叠"
    return ""


def external_predictions(model, examples):
    session = _session(model["onnx"])
    results = []
    for example in examples:
        row = [example["x"][i] for i in model["featureIndices"]]
        if any(value is None or not math.isfinite(value) for value in row):
            results.append(None)
            continue
        # Keep each prediction independent of other evaluation records.
        results.append(
            _run(session, model["inputName"], model["outputName"], model["inputType"], [row])[0]
        )
    return results
