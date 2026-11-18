# -*- coding: utf-8 -*-
"""M0：对 pt / onnx 在 SSGD val 集上跑官方验证，比较 mAP，确认导出无精度损失。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ultralytics import YOLO

DATA = r"E:\BaiduNetdiskDownload\SSGD\SSGD_YOLO\dataset.yaml"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pt", default=r"E:\downLoad\best.pt")
    ap.add_argument("--onnx", default=r"E:\code\python\zhongjiAgent\apps\model_server\models\glass\v1\best.onnx")
    ap.add_argument("--out", default=str(Path(__file__).parent / "out" / "val_metrics.json"))
    args = ap.parse_args()

    result = {}
    for tag, path in [("pt", args.pt), ("onnx", args.onnx)]:
        print(f"[val] 验证 {tag}: {path}")
        model = YOLO(path)
        m = model.val(data=DATA, imgsz=1280, split="val", verbose=False)
        result[tag] = {
            "mAP50": round(float(m.box.map50), 4),
            "mAP50_95": round(float(m.box.map), 4),
            "per_class_mAP50": {
                (model.names[i] if isinstance(model.names, dict) else str(i)): round(float(v), 4)
                for i, v in enumerate(m.box.ap50)
            },
        }
        print(f"[val] {tag} mAP50={result[tag]['mAP50']} mAP50-95={result[tag]['mAP50_95']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[val] 结果写入 {out}")


if __name__ == "__main__":
    main()
