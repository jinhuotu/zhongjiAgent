"""用保存权重构建标准 ONNX 图，不重新训练，不依赖 PyTorch/sklearn。"""

import numpy as np

from common.errors import AppError, ErrorCode

SUPPORTED = {"baseline", "physics", "ridge", "lasso", "boosting", "fusion", "lstm"}
_CUSTOM_CONVERTERS = {}


def register_onnx_converter(key, converter):
    if key in SUPPORTED or key in _CUSTOM_CONVERTERS or not callable(converter):
        raise ValueError("ONNX 转换器键重复或实现不可调用")
    _CUSTOM_CONVERTERS[key] = converter


def has_onnx_converter(key):
    return key in SUPPORTED or key in _CUSTOM_CONVERTERS


def implementation(model):
    return model.get("implementationKey", model["algorithm"])


def output_offsets(model):
    if implementation(model) in _CUSTOM_CONVERTERS:
        from common.kiln_studio_engine import prediction_offsets

        return prediction_offsets(model)
    if implementation(model) == "lstm":
        return model["model"].get("offsets", [model["config"]["horizonMinutes"]])
    horizon = model["config"]["horizonMinutes"]
    heads = model.get("minuteHeads", model.get("temperatureHeads", {}))
    if implementation(model) in {"ridge", "fusion"} and all(
        str(minute) in heads for minute in range(1, horizon)
    ):
        return list(range(1, horizon + 1))
    return [horizon]


def validate_models(data):
    if not data["models"]:
        raise AppError(ErrorCode.VALIDATION, "当前模型包没有可转换的模型", 422)
    for model in data["models"]:
        key = implementation(model)
        if not has_onnx_converter(key):
            raise AppError(
                ErrorCode.VALIDATION, f"通用 ONNX 转换暂不支持 {key}，请导出数值 JSON", 422
            )
        fields = model.get("features", [])
        offsets = output_offsets(model)
        if len(fields) != 12 or len(set(fields)) != 12 or not offsets:
            raise AppError(ErrorCode.VALIDATION, "模型特征或输出时距不符合 ONNX 转换契约", 422)
        if sorted(set(offsets)) != offsets or any(not 0 < v <= 120 for v in offsets):
            raise AppError(ErrorCode.VALIDATION, "模型输出时距无效", 422)


class Graph:
    """只负责命名、常量和标准算子；业务转换保留为下方短函数。"""

    def __init__(self):
        from onnx import helper, numpy_helper

        self.helper, self.numpy_helper = helper, numpy_helper
        self.nodes, self.constants = [], []
        self.counter = 0

    def name(self):
        self.counter += 1
        return f"v{self.counter}"

    def const(self, value, dtype=np.float32):
        name = self.name()
        self.constants.append(self.numpy_helper.from_array(np.asarray(value, dtype=dtype), name))
        return name

    def op(self, kind, *inputs, **attrs):
        name = self.name()
        self.nodes.append(self.helper.make_node(kind, list(inputs), [name], **attrs))
        return name

    def col(self, value, index, axis=1):
        return self.op("Gather", value, self.const([index], np.int64), axis=axis)

    def clip(self, value, low, high):
        return self.op("Clip", value, self.const(low), self.const(high))

    def normalize(self, value, mean, scale, clip=False):
        missing = self.op("Or", self.op("IsNaN", value), self.op("IsInf", value))
        mean = self.const(mean)
        filled = self.op("Where", missing, mean, value)
        normalized = self.op("Div", self.op("Sub", filled, mean), self.const(scale))
        return self.clip(normalized, -10, 10) if clip else normalized


def _physics(g, raw, model):
    p, cfg = model["parameters"], model["config"]
    # 让常量空燃比也保留动态批次维度。
    if cfg["task"] == "afr":
        zero = g.op("Mul", g.col(raw, 0), g.const(0))
        return g.op("Add", zero, g.const(p["theoreticalAfr"] * p["excessAir"]))
    y0, sp0, sp1, slope = (g.col(raw, index) for index in (0, 9, 10, 11))
    if cfg["task"] == "energy":
        ramp = g.op("Mul", g.op("Max", slope, g.const(0)), g.const(p["capacityKJ"]))
        delta = g.op("Max", g.op("Sub", sp1, g.const(p["ambientC"])), g.const(0))
        loss = g.op("Mul", delta, g.const(3600 * p["lossKW"]))
        return g.op("Div", g.op("Add", ramp, loss), g.const(1000 * p["efficiency"] * p["lhvMJ"]))
    tau = p["tauMinutes"] / 60
    ramp = g.op("Mul", slope, g.const(tau))
    baseline = g.op("Sub", sp1, ramp)
    residual = g.op("Add", g.op("Sub", y0, sp0), ramp)
    decay = np.exp(-(cfg["horizonMinutes"] / 60) / tau)
    return g.op("Add", baseline, g.op("Mul", residual, g.const(decay)))


def _tabular(g, raw, model):
    key = implementation(model)
    if key == "baseline":
        return g.col(raw, 0)
    if key == "physics":
        return _physics(g, raw, model)
    prep = model["preprocessor"]
    x = g.normalize(raw, prep["mean"], prep["scale"])
    if key in {"ridge", "lasso", "fusion"}:
        weights = np.asarray(model["model"])
        value = g.op("Add", g.op("MatMul", x, g.const(weights[1:, None])), g.const(weights[0]))
        return g.op("Add", _physics(g, raw, model), value) if key == "fusion" else value
    saved = model["model"]
    value = g.op("Add", g.op("Mul", g.col(x, 0), g.const(0)), g.const(saved["initial"]))
    for index, threshold, left, right in saved["trees"]:
        decision = g.op("LessOrEqual", g.col(x, int(index)), g.const(threshold))
        leaf = g.op(
            "Where", decision, g.const(left * saved["rate"]), g.const(right * saved["rate"])
        )
        value = g.op("Add", value, leaf)
    return value


def _lstm(g, raw, model):
    saved, prep = model["model"], model["preprocessor"]
    sequence = g.normalize("sequence", saved["sequenceMean"], saved["sequenceScale"], True)
    context = g.normalize(raw, prep["mean"], prep["scale"], True)
    weights = {key: np.asarray(value) for key, value in saved["weights"].items()}
    hidden_size = weights["recurrent"].shape[0]
    hidden = g.op("MatMul", context, g.const(np.zeros((12, hidden_size))))
    cell = hidden
    iw, rw, bias = (g.const(weights[key]) for key in ("input", "recurrent", "bias"))
    # 保留平台的 f/i/o/g 门序及 sigmoid 裁剪，展开固定历史长度便于跨运行时部署。
    for step in range(saved["sequenceLength"]):
        row = g.op("Gather", sequence, g.const(step, np.int64), axis=1)
        gates = g.op("Add", g.op("Add", g.op("MatMul", row, iw), g.op("MatMul", hidden, rw)), bias)
        chunks = [
            g.op(
                "Gather",
                gates,
                g.const(list(range(i * hidden_size, (i + 1) * hidden_size)), np.int64),
                axis=1,
            )
            for i in range(4)
        ]
        forget, incoming, outgoing = [g.op("Sigmoid", g.clip(v, -40, 40)) for v in chunks[:3]]
        cell = g.op(
            "Add", g.op("Mul", forget, cell), g.op("Mul", incoming, g.op("Tanh", chunks[3]))
        )
        hidden = g.op("Mul", outgoing, g.op("Tanh", cell))
    output = g.op("MatMul", hidden, g.const(weights["output"].reshape(hidden_size, -1)))
    residual = g.op("MatMul", context, g.const(weights["context"].reshape(12, -1)))
    output = g.op("Add", g.op("Add", output, residual), g.const(weights["offset"]))
    restored = g.op(
        "Add", g.op("Mul", output, g.const(saved["targetScale"])), g.const(saved["targetMean"])
    )
    return g.op("Add", restored, g.col(raw, 0))


def _minute_heads(g, raw, model, offsets):
    outputs = []
    heads = model.get("minuteHeads", model.get("temperatureHeads", {}))
    for minute in offsets:
        if minute == model["config"]["horizonMinutes"]:
            outputs.append(_tabular(g, raw, model))
            continue
        plan = g.col("futurePlans", minute - 1)
        slope = g.op("Mul", g.op("Sub", plan, g.col(raw, 9)), g.const(60 / minute))
        prefix = g.op("Gather", raw, g.const(list(range(10)), np.int64), axis=1)
        updated = g.op("Concat", prefix, plan, slope, axis=1)
        head = {
            **model,
            **heads[str(minute)],
            "config": {**model["config"], "horizonMinutes": minute},
        }
        outputs.append(_tabular(g, updated, head))
    return g.op("Concat", *outputs, axis=1)


def build_onnx(model):
    import onnx
    from onnx import TensorProto, helper

    converter = _CUSTOM_CONVERTERS.get(implementation(model))
    if converter:
        result = converter(model)
        parsed = onnx.load_model_from_string(result)
        onnx.checker.check_model(parsed, full_check=True)
        return result

    g, offsets = Graph(), output_offsets(model)
    raw = "features"
    inputs = [helper.make_tensor_value_info(raw, TensorProto.FLOAT, ["batch", 12])]
    if implementation(model) == "lstm":
        saved = model["model"]
        shape = ["batch", saved["sequenceLength"], len(saved["sequenceMean"])]
        inputs.append(helper.make_tensor_value_info("sequence", TensorProto.FLOAT, shape))
        value = _lstm(g, raw, model)
    elif len(offsets) > 1:
        inputs.append(
            helper.make_tensor_value_info("futurePlans", TensorProto.FLOAT, ["batch", len(offsets)])
        )
        value = _minute_heads(g, raw, model, offsets)
    else:
        value = _tabular(g, raw, model)
    if model["config"]["task"] in {"energy", "afr"}:
        value = g.op("Max", value, g.const(0))
    g.nodes.append(helper.make_node("Identity", [value], ["predictions"]))
    output = helper.make_tensor_value_info(
        "predictions", TensorProto.FLOAT, ["batch", len(offsets)]
    )
    graph = helper.make_graph(g.nodes, "kiln-studio", inputs, [output], g.constants)
    result = helper.make_model(
        graph, opset_imports=[helper.make_opsetid("", 13)], producer_name="kiln-studio"
    )
    result.ir_version = 8
    onnx.checker.check_model(result, full_check=True)
    return result.SerializeToString()
