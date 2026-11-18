# -*- coding: utf-8 -*-
"""M0 诊断：验证 ultralytics 的 letterbox 是否按 stride 对齐。

做法：额外导出一个动态输入 ONNX，用手写的"stride 对齐 letterbox"推理，
与 ultralytics 原生 pt 推理对比；若一致，则确认差异来自 padding 策略而非导出损失。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from ultralytics import YOLO

IMGSZ = 1280
CONF = 0.25
IOU_NMS = 0.45
MAX_DET = 300
MATCH_IOU = 0.5
STRIDE = 32
VAL = Path(r"E:\BaiduNetdiskDownload\SSGD\SSGD_YOLO\images\val")


def letterbox_stride(img, new_shape=IMGSZ, stride=STRIDE):
    h, w = img.shape[:2]
    r = min(new_shape / h, new_shape / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    dw, dh = new_shape - nw, new_shape - nh
    dw, dh = dw % stride, dh % stride
    dw, dh = dw / 2, dh / 2
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return padded, r, (left, top)


def nms(boxes, scores, thr):
    if not len(boxes):
        return []
    x1, y1, x2, y2 = boxes.T
    areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
        order = order[1:][iou <= thr]
    return keep


class DynDetector:
    def __init__(self, path):
        self.s = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
        self.i = self.s.get_inputs()[0].name
        self.o = self.s.get_outputs()[0].name

    def predict(self, img):
        h0, w0 = img.shape[:2]
        padded, r, (px, py) = letterbox_stride(img)
        blob = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None]
        out = self.s.run([self.o], {self.i: blob})[0]
        pred = np.squeeze(out, 0).T
        xywh, scores = pred[:, :4], pred[:, 4:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(scores)), cls]
        m = conf >= CONF
        xywh, conf, cls = xywh[m], conf[m], cls[m]
        if not len(xywh):
            return np.zeros((0, 6), np.float32)
        cx, cy, bw, bh = xywh.T
        boxes = np.stack([(cx - bw / 2 - px) / r, (cy - bh / 2 - py) / r,
                          (cx + bw / 2 - px) / r, (cy + bh / 2 - py) / r], 1)
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w0)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h0)
        keep = []
        for c in np.unique(cls):
            idx = np.where(cls == c)[0]
            keep += idx[nms(boxes[idx], conf[idx], IOU_NMS)].tolist()
        keep = sorted(keep, key=lambda i: -conf[i])[:MAX_DET]
        return np.column_stack([boxes[keep], conf[keep], cls[keep]]).astype(np.float32)


def iou(a, b):
    xx1, yy1 = max(a[0], b[0]), max(a[1], b[1])
    xx2, yy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, xx2 - xx1) * max(0, yy2 - yy1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def main():
    pt = YOLO(r"E:\downLoad\best.pt")
    model = YOLO(r"E:\downLoad\best.pt")
    dyn_path = str(Path(__file__).parent / "best_dynamic.onnx")
    if not Path(dyn_path).exists():
        print("[diag] 导出动态 ONNX ...")
        model.export(format="onnx", imgsz=IMGSZ, opset=12, simplify=True, dynamic=True, half=False)
        import shutil
        shutil.copy2(r"E:\downLoad\best.onnx", dyn_path)
    dyn = DynDetector(dyn_path)

    files = sorted(p for p in VAL.iterdir() if p.suffix.lower() in {".jpg", ".png"})[:50]
    tot_m = tot_pt = tot_d = 0
    ious = []
    for f in files:
        img = cv2.imread(str(f))
        b = pt.predict(img, imgsz=IMGSZ, conf=CONF, iou=IOU_NMS, max_det=MAX_DET, verbose=False)[0].boxes
        pt_arr = np.column_stack([b.xyxy.cpu().numpy(), b.conf.cpu().numpy(), b.cls.cpu().numpy()]) if len(b) else np.zeros((0, 6), np.float32)
        d_arr = dyn.predict(img)
        used = set()
        for row in pt_arr:
            best, bj = 0.0, -1
            for j, c in enumerate(d_arr):
                if j in used or int(c[5]) != int(row[5]):
                    continue
                v = iou(row[:4], c[:4])
                if v > best:
                    best, bj = v, j
            if bj >= 0 and best >= MATCH_IOU:
                used.add(bj)
                ious.append(best)
                tot_m += 1
        tot_pt += len(pt_arr)
        tot_d += len(d_arr)
    print(f"[diag] stride对齐 + 动态ONNX vs PT:")
    print(f"  PT框={tot_pt}  ONNX框={tot_d}  匹配={tot_m}  匹配率={tot_m/max(tot_pt,1):.2%}  平均IoU={np.mean(ious):.4f}")


if __name__ == "__main__":
    main()
