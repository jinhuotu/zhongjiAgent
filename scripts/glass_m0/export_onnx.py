# -*- coding: utf-8 -*-
"""M0：把训练好的 best.pt 导出为静态 ONNX，并落一份 meta.json。

用法（在 M0 隔离环境里执行）：
    python export_onnx.py --pt E:\\downLoad\\best.pt

产物：
    <model_dir>/v1/best.onnx
    <model_dir>/v1/meta.json

设计要点：
- dynamic=False + batch=1 + imgsz 固定，避免推理端处理动态维度。
- 不嵌入 NMS（YOLO 的 ONNX 导出默认不含 NMS），后处理放在推理端手写，
  这样 model_server 用标准 onnxruntime 即可，不依赖 ultralytics。
- meta.json 记录类别、输入尺寸、来源、文件大小，供服务端探活与前端展示。
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

IMGSZ = 1280
OPSET = 12


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pt", required=True, help="best.pt 路径")
    parser.add_argument(
        "--model-dir",
        default=str(Path("apps") / "model_server" / "models" / "glass"),
        help="模型根目录，内部按版本号建子目录",
    )
    parser.add_argument("--version", default="v1", help="版本号，如 v1")
    parser.add_argument("--source", default="", help="模型来源说明（数据集/训练脚本）")
    parser.add_argument("--mAP50", type=float, default=None, help="训练时的 mAP50，仅记录")
    parser.add_argument("--mAP50-95", type=float, default=None, help="训练时的 mAP50-95，仅记录")
    parser.add_argument(
        "--dynamic",
        dest="dynamic",
        action="store_true",
        default=True,
        help="动态 H/W 输入（默认开启，与 ultralytics stride 对齐 letterbox 一致）",
    )
    parser.add_argument("--no-dynamic", dest="dynamic", action="store_false", help="固定 H/W 输入")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    pt_path = Path(args.pt).expanduser().resolve()
    if not pt_path.exists():
        raise SystemExit(f"找不到 pt 文件: {pt_path}")

    from ultralytics import YOLO

    model = YOLO(str(pt_path))

    names = model.names
    if isinstance(names, dict):
        names_map = {int(k): str(v) for k, v in names.items()}
    else:
        names_map = {i: str(v) for i, v in enumerate(names)}

    print(f"[export] 载入 {pt_path.name}，类别数={len(names_map)}")
    print(f"[export] 类别: {names_map}")

    exported = model.export(
        format="onnx",
        imgsz=IMGSZ,
        opset=OPSET,
        simplify=True,
        dynamic=args.dynamic,
        half=False,
    )
    onnx_src = Path(exported).resolve()
    if not onnx_src.exists():
        raise SystemExit(f"导出失败，未找到 onnx: {onnx_src}")

    out_dir = (Path(args.model_dir) / args.version).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    onnx_dst = out_dir / "best.onnx"
    if onnx_src != onnx_dst:
        shutil.copy2(onnx_src, onnx_dst)

    meta = {
        "version": args.version,
        "format": "onnx",
        "imgsz": IMGSZ,
        "opset": OPSET,
        "dynamic": args.dynamic,
        "nms_embedded": False,
        "letterbox": "stride",  # 按 stride 对齐 padding，与 ultralytics 推理一致
        "stride": 32,
        "num_classes": len(names_map),
        "classes": [names_map[i] for i in sorted(names_map)],
        "source_pt": str(pt_path),
        "source_note": args.source,
        "onnx_size_bytes": onnx_dst.stat().st_size,
        "onnx_size_mb": round(onnx_dst.stat().st_size / 1024 / 1024, 2),
        "train_metrics": {
            "mAP50": args.mAP50,
            "mAP50_95": args.mAP50_95,
        },
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"[export] ONNX 已写入: {onnx_dst} ({meta['onnx_size_mb']} MB)")
    print(f"[export] meta 已写入: {out_dir / 'meta.json'}")


if __name__ == "__main__":
    main()
