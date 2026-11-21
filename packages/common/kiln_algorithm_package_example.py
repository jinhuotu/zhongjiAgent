"""A trainable reference package served from the algorithm management screen."""

import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile


def example_package():
    manifest = {
        "apiVersion": 1,
        "name": "示例 · 温度趋势回归",
        "version": "1.0.0",
        "description": "用过去的温度变化拟合指定预测时距的温度变化，用于验证算法包接入。",
        "tasks": ["temperature"],
        "options": {"scale": 1.0},
    }
    source = """import numpy as np


def recent_change(example):
    earliest = example["x"][1]
    return 0.0 if earliest is None else example["y0"] - earliest


def fit(x, y, config, parameters, train_rows):
    current = np.asarray([row["y0"] for row in train_rows], dtype=float)
    changes = np.asarray([recent_change(row) for row in train_rows], dtype=float)
    center = float(changes.mean())
    design = np.column_stack((np.ones(len(y)), changes - center))
    offset, weight = np.linalg.lstsq(design, y - current, rcond=None)[0]
    return {"center": center, "offset": float(offset), "weight": float(weight)}


def predict(artifact, example):
    model = artifact["model"]
    change = recent_change(example) - model["center"]
    scale = artifact["config"]["options"]["scale"]
    return float(example["y0"] + scale * (model["offset"] + model["weight"] * change))
"""
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        archive.writestr("algorithm.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("algorithm.py", source)
    return buffer.getvalue()
