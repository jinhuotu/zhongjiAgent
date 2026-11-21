"""Protocol adapter inside the isolated image. Only this process imports uploaded code."""

import importlib
import json
import os
import sys
from pathlib import Path

import numpy as np


def run(request):
    source = Path("/tmp/source")
    source.mkdir()
    for name, content in request["sources"].items():
        (source / name).write_text(content, encoding="utf-8")
    sys.path.insert(0, str(source))
    algorithm = importlib.import_module("algorithm")
    fit, predict = getattr(algorithm, "fit", None), getattr(algorithm, "predict", None)
    if not callable(fit) or not callable(predict):
        raise ValueError("algorithm.py 需要提供 fit 和 predict 函数")
    body = request["body"]
    if request["operation"] == "fit":
        return fit(
            np.asarray(body["x"], dtype=float),
            np.asarray(body["y"], dtype=float),
            body["config"],
            body["parameters"],
            body["train_rows"],
        )
    artifact, rows = body["artifact"], body["rows"]
    batch = getattr(algorithm, "predict_many", None)
    if request["operation"] == "predict_many" and callable(batch):
        return list(batch(artifact, rows))
    return [predict(artifact, row) for row in rows]


def main():
    # Keep stdout as a protocol channel even when an algorithm prints diagnostics.
    with os.fdopen(os.dup(1), "wb", closefd=True) as response:
        with open(os.devnull, "wb") as null:
            os.dup2(null.fileno(), 1)
            os.dup2(null.fileno(), 2)
        try:
            value = run(json.load(sys.stdin.buffer))
            encoded = json.dumps({"value": value}, allow_nan=False).encode("utf-8")
        except Exception as exc:
            encoded = json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:300]}"}).encode()
        response.write(encoded)


if __name__ == "__main__":
    main()
