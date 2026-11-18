# -*- coding: utf-8 -*-
"""M0 辅助：对验证中不一致的图片，逐框打印 PT/ONNX 的类别与置信度，定位差异来源。"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

sys.path.insert(0, str(Path(__file__).parent))
from verify_pt_vs_onnx import IMGSZ, IOU_NMS, MAX_DET, OnnxDetector  # noqa: E402

IMAGES = Path(r"E:\BaiduNetdiskDownload\SSGD\SSGD_YOLO\images\val")
FILES = [
    "062916-0355-259-4-301_ng_70.jpg",
    "063016-5726-569-1-101_ng_34.jpg",
    "063016-5933-407-5-301_ng_21.jpg",
    "070815-3112-675-2-301_ng_63.jpg",
]
CONF = 0.25


def dump(tag: str, arr: np.ndarray) -> None:
    print(f"  [{tag}] {len(arr)} 框")
    for row in sorted(arr.tolist(), key=lambda r: -r[4]):
        print(
            f"    cls={int(row[5])} conf={row[4]:.4f} "
            f"xyxy=({row[0]:.0f},{row[1]:.0f},{row[2]:.0f},{row[3]:.0f})"
        )


def main() -> None:
    pt = YOLO(r"E:\downLoad\best.pt")
    onnx = OnnxDetector(r"E:\code\python\zhongjiAgent\apps\model_server\models\glass\v1\best.onnx")

    for name in FILES:
        img = cv2.imread(str(IMAGES / name))
        r = pt.predict(img, imgsz=IMGSZ, conf=CONF, iou=IOU_NMS, max_det=MAX_DET, verbose=False)[0]
        b = r.boxes
        pt_arr = np.column_stack(
            [b.xyxy.cpu().numpy(), b.conf.cpu().numpy(), b.cls.cpu().numpy()]
        )
        onnx_arr = onnx.predict(img)
        print(f"\n{name}")
        dump("PT", pt_arr)
        dump("ONNX", onnx_arr)


if __name__ == "__main__":
    main()
