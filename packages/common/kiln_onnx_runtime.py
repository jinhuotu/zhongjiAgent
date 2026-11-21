"""交付包独立 ONNX Runtime 程序；不导入平台模块。"""

RUNNER_RUNTIME = r'''"""通用 ONNX FP32 推理；只依赖 numpy、onnxruntime。"""

import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from preprocess import prepare


def load_session(spec):
    path = ROOT / spec["file"]
    blob = path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != spec["sha256"]:
        raise ValueError("模型文件校验失败：" + spec["file"])
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(blob, sess_options=options, providers=["CPUExecutionProvider"])


def run(manifest, document, verify=False, benchmark=0):
    specs = {spec["modelIndex"]: spec for spec in manifest["models"]}
    sessions, outputs = {}, []
    for item in document["inputs"]:
        index = item["modelIndex"]
        if isinstance(index, bool) or not isinstance(index, int) or index not in specs:
            raise ValueError("无效的 modelIndex")
        spec = specs[index]
        if index not in sessions:
            sessions[index] = load_session(spec)
        session = sessions[index]
        feeds = prepare(item, spec)
        values = session.run(["predictions"], feeds)[0][0]
        if len(values) != len(spec["offsetsMinutes"]) or not np.isfinite(values).all():
            raise ValueError("模型未返回完整有限预测值")
        record = {
            "modelIndex": index,
            "algorithm": spec["algorithm"],
            "task": spec["config"]["task"],
            "unit": spec["unit"],
            "offsetsMinutes": spec["offsetsMinutes"],
            "values": values.tolist(),
            "providers": session.get_providers(),
        }
        if verify:
            expected = numeric_expected(item, values.shape)
            tolerance = manifest["verificationTolerance"]
            if not np.allclose(values, expected, **tolerance):
                raise ValueError("与平台逐点核对失败，modelIndex=" + str(index))
            record["maxAbsoluteDifference"] = float(np.max(np.abs(values - expected)))
        if benchmark:
            for _ in range(5):
                session.run(["predictions"], feeds)
            timings = []
            for _ in range(benchmark):
                start = time.perf_counter()
                session.run(["predictions"], feeds)
                timings.append((time.perf_counter() - start) * 1000)
            record["benchmark"] = {
                "runs": benchmark,
                "p50Ms": float(np.percentile(timings, 50)),
                "p95Ms": float(np.percentile(timings, 95)),
                "scope": "已加载 CPU 会话；不含预处理、文件读取及网络",
                "system": platform.platform(),
                "architecture": platform.machine(),
                "onnxruntime": ort.__version__,
            }
        outputs.append(record)
    return outputs


def numeric_expected(item, shape):
    expected = np.asarray(item["expectedValues"], dtype=float)
    if expected.shape != shape or not np.isfinite(expected).all():
        raise ValueError("平台核对值缺失或形状不一致")
    return expected


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="窑炉通用 ONNX CPU 推理")
    parser.add_argument("--input", type=Path, default=ROOT / "input_example.json")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--benchmark", type=int, default=0, help="本机已加载会话的实测次数")
    args = parser.parse_args()
    if not 0 <= args.benchmark <= 100000:
        parser.error("benchmark 须在 0 到 100000 之间")
    try:
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
        document = json.loads(args.input.read_text(encoding="utf-8"))
        print(
            json.dumps(
                run(manifest, document, args.verify, args.benchmark),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
    except Exception as exc:
        parser.exit(1, "推理失败：" + str(exc) + "\n")


if __name__ == "__main__":
    main()
'''
