# -*- coding: utf-8 -*-
"""M0：对比 best.pt（ultralytics）与 best.onnx（纯 onnxruntime）的检测结果一致性。

目的：确认 ONNX 导出没有损失精度，且"我们自己手写的 letterbox + NMS 后处理"
与 ultralytics 原生推理结果一致，从而证明 model_server 走 onnxruntime 的方案成立。

用法：
    python verify_pt_vs_onnx.py \
        --pt E:\\downLoad\\best.pt \
        --onnx apps\\model_server\\models\\glass\\v1\\best.onnx \
        --images E:\\BaiduNetdiskDownload\\SSGD\\SSGD_YOLO\\images\\val \
        --limit 50

产出：
    out/verify_report.json        —— 机器可读的完整报告
    out/verify_summary.txt        —— 人读摘要
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from ultralytics import YOLO

IMGSZ = 1280
CONF = 0.25  # 与演示版默认阈值一致
IOU_NMS = 0.45
MAX_DET = 300
MATCH_IOU = 0.5  # 判定"同一框"的 IoU 门槛
STRIDE = 32


def letterbox(img: np.ndarray, new_shape: int = IMGSZ, stride: int = STRIDE, color=(114, 114, 114)):
    """等比缩放 + 按 stride 对齐的灰边填充，返回 (图像, 缩放比, (pad_x, pad_y))。

    与 ultralytics 的 LetterBox 行为一致：padding 只补齐到 stride 的整数倍，
    而不是补满成正方形（这一点是 pt/onnx 结果能否一致的关键）。
    """
    h, w = img.shape[:2]
    r = min(new_shape / h, new_shape / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    dw, dh = new_shape - nw, new_shape - nh
    dw, dh = dw % stride, dh % stride
    dw, dh = dw / 2, dh / 2
    top, bottom = int(round(dh - 0.1)), int(round(dh + 0.1))
    left, right = int(round(dw - 0.1)), int(round(dw + 0.1))
    padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)
    return padded, r, (left, top)


def nms_numpy(boxes: np.ndarray, scores: np.ndarray, iou_thr: float) -> list[int]:
    """标准贪心 NMS。boxes 为 xyxy。"""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        iw = np.maximum(0.0, xx2 - xx1)
        ih = np.maximum(0.0, yy2 - yy1)
        inter = iw * ih
        union = areas[i] + areas[order[1:]] - inter
        iou = np.where(union > 0, inter / union, 0.0)
        order = order[1:][iou <= iou_thr]
    return keep


class OnnxDetector:
    """纯 onnxruntime 检测器（M0 用于验证；M2 会抽到 model_server）。"""

    def __init__(self, onnx_path: str):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self.session = ort.InferenceSession(
            onnx_path, sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

    def predict(self, img_bgr: np.ndarray) -> np.ndarray:
        """返回 xyxy + conf + cls 的 (N,6) 数组，坐标已还原到原图。"""
        h0, w0 = img_bgr.shape[:2]
        padded, ratio, (pad_x, pad_y) = letterbox(img_bgr, IMGSZ)
        rgb = cv2.cvtColor(padded, cv2.COLOR_BGR2RGB)
        blob = rgb.astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None, ...]
        out = self.session.run([self.output_name], {self.input_name: blob})[0]
        pred = np.squeeze(out, axis=0).T  # (8400, 4+nc)
        boxes_xywh = pred[:, :4]
        scores_all = pred[:, 4:]

        cls_ids = scores_all.argmax(axis=1)
        confs = scores_all[np.arange(len(scores_all)), cls_ids]
        mask = confs >= CONF
        boxes_xywh, confs, cls_ids = boxes_xywh[mask], confs[mask], cls_ids[mask]
        if len(boxes_xywh) == 0:
            return np.zeros((0, 6), dtype=np.float32)

        # xywh(中心, letterbox 坐标) -> xyxy(原图坐标)
        cx, cy, bw, bh = boxes_xywh.T
        x1 = cx - bw / 2
        y1 = cy - bh / 2
        x2 = cx + bw / 2
        y2 = cy + bh / 2
        x1 = (x1 - pad_x) / ratio
        y1 = (y1 - pad_y) / ratio
        x2 = (x2 - pad_x) / ratio
        y2 = (y2 - pad_y) / ratio
        boxes = np.stack([x1, y1, x2, y2], axis=1)
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w0)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h0)

        keep: list[int] = []
        for c in np.unique(cls_ids):
            idx = np.where(cls_ids == c)[0]
            kept = nms_numpy(boxes[idx], confs[idx], IOU_NMS)
            keep.extend(idx[kept].tolist())
        keep = sorted(keep, key=lambda i: -confs[i])[:MAX_DET]
        return np.column_stack([boxes[keep], confs[keep], cls_ids[keep]]).astype(np.float32)


def iou_xyxy(a: np.ndarray, b: np.ndarray) -> float:
    xx1, yy1 = max(a[0], b[0]), max(a[1], b[1])
    xx2, yy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, xx2 - xx1), max(0.0, yy2 - yy1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def match(a: np.ndarray, b: np.ndarray) -> tuple[int, float, int, int]:
    """贪心匹配两个检测数组（同类别且 IoU>=阈值）。

    返回 (匹配数, 平均IoU, 仅a有的框数, 仅b有的框数)。
    """
    used_b: set[int] = set()
    ious: list[float] = []
    for row in a:
        best_i, best_iou = -1, 0.0
        for j, cand in enumerate(b):
            if j in used_b or int(cand[5]) != int(row[5]):
                continue
            v = iou_xyxy(row[:4], cand[:4])
            if v > best_iou:
                best_i, best_iou = j, v
        if best_i >= 0 and best_iou >= MATCH_IOU:
            used_b.add(best_i)
            ious.append(best_iou)
    matched = len(ious)
    mean_iou = float(np.mean(ious)) if ious else 0.0
    return matched, mean_iou, len(a) - matched, len(b) - matched


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--pt", required=True)
    p.add_argument("--onnx", required=True)
    p.add_argument("--images", required=True, help="图片文件夹")
    p.add_argument("--limit", type=int, default=50, help="最多验证多少张")
    p.add_argument("--conf", type=float, default=CONF, help="置信度阈值")
    p.add_argument("--out", default=str(Path(__file__).parent / "out"))
    return p.parse_args()


def main() -> None:
    global CONF
    args = parse_args()
    CONF = args.conf
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    img_dir = Path(args.images).resolve()
    files = sorted(
        p for p in img_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp"}
    )[: args.limit]
    if not files:
        raise SystemExit(f"目录内没有图片: {img_dir}")

    print(f"[verify] pt={args.pt}")
    print(f"[verify] onnx={args.onnx}")
    print(f"[verify] 验证图片数={len(files)}")

    pt_model = YOLO(args.pt)
    names = pt_model.names
    detector = OnnxDetector(args.onnx)

    per_image = []
    tot_matched = tot_pt = tot_onnx = 0
    all_matched_ious: list[float] = []
    onnx_times: list[float] = []
    pt_times: list[float] = []

    import time

    for idx, f in enumerate(files, 1):
        img = cv2.imread(str(f))
        if img is None:
            print(f"[verify] 跳过无法读取: {f.name}")
            continue

        t0 = time.perf_counter()
        r = pt_model.predict(img, imgsz=IMGSZ, conf=CONF, iou=IOU_NMS, max_det=MAX_DET, verbose=False)[0]
        pt_times.append(time.perf_counter() - t0)
        pt_boxes = r.boxes
        pt_arr = np.column_stack(
            [
                pt_boxes.xyxy.cpu().numpy(),
                pt_boxes.conf.cpu().numpy(),
                pt_boxes.cls.cpu().numpy(),
            ]
        ) if len(pt_boxes) else np.zeros((0, 6), dtype=np.float32)

        t1 = time.perf_counter()
        onnx_arr = detector.predict(img)
        onnx_times.append(time.perf_counter() - t1)

        matched, mean_iou, only_pt, only_onnx = match(pt_arr, onnx_arr)
        tot_matched += matched
        tot_pt += len(pt_arr)
        tot_onnx += len(onnx_arr)
        all_matched_ious.extend([mean_iou] * matched if matched else [])

        per_image.append(
            {
                "file": f.name,
                "pt_count": int(len(pt_arr)),
                "onnx_count": int(len(onnx_arr)),
                "matched": matched,
                "mean_iou": round(mean_iou, 4),
                "only_pt": only_pt,
                "only_onnx": only_onnx,
            }
        )
        if idx % 10 == 0 or idx == len(files):
            print(f"[verify] {idx}/{len(files)} 完成，累计匹配 {tot_matched}")

    mean_matched_iou = float(np.mean(all_matched_ious)) if all_matched_ious else 0.0
    match_rate = tot_matched / tot_pt if tot_pt else 0.0
    report = {
        "imgsz": IMGSZ,
        "conf": CONF,
        "iou_nms": IOU_NMS,
        "match_iou_threshold": MATCH_IOU,
        "letterbox": "stride",
        "classes": names if isinstance(names, dict) else {i: v for i, v in enumerate(names)},
        "images_tested": len(per_image),
        "total_pt_boxes": tot_pt,
        "total_onnx_boxes": tot_onnx,
        "total_matched": tot_matched,
        "match_rate": round(match_rate, 4),
        "mean_matched_iou": round(mean_matched_iou, 4),
        "latency_ms": {
            "onnx_mean": round(float(np.mean(onnx_times)) * 1000, 1) if onnx_times else None,
            "onnx_p90": round(float(np.percentile(onnx_times, 90)) * 1000, 1) if onnx_times else None,
            "pt_mean": round(float(np.mean(pt_times)) * 1000, 1) if pt_times else None,
        },
        "per_image": per_image,
    }
    (out_dir / "verify_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = (
        f"PT vs ONNX 一致性验证\n"
        f"{'=' * 40}\n"
        f"验证图片: {len(per_image)} 张\n"
        f"PT 检出框总数: {tot_pt}\n"
        f"ONNX 检出框总数: {tot_onnx}\n"
        f"成功匹配(同类别且IoU>={MATCH_IOU}): {tot_matched}\n"
        f"匹配率 (匹配/PT框): {match_rate:.2%}\n"
        f"匹配框平均 IoU: {mean_matched_iou:.4f}\n"
        f"结论: {'通过' if match_rate >= 0.95 and mean_matched_iou >= 0.9 else '需关注'}\n"
    )
    (out_dir / "verify_summary.txt").write_text(summary, encoding="utf-8")
    print("\n" + summary)


if __name__ == "__main__":
    main()
