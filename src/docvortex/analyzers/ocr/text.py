"""基于 DB 检测和 CTC 解码的独立 OCR 实现，图像运算使用 DocVortex 接口。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import numpy as np

from ...image import (
    contour_area,
    contour_length,
    minimum_rectangle,
    perspective_matrix,
    rasterize_polygons,
    rectangle_corners,
    resize_image,
    trace_contours,
    warp_image,
)
from .runtime import create_session, read_config


@dataclass(frozen=True)
class TextLine:
    """保存裁图局部四边形、识别文字和置信度，供正文组行与表格投影复用。"""

    quad: np.ndarray
    text: str
    score: float


def order_corners(points: np.ndarray) -> np.ndarray:
    """把矩形四点按左上、右上、右下、左下排列，用于透视回正。"""
    points = np.asarray(points, dtype=np.float32)
    left, right = np.split(points[np.argsort(points[:, 0], kind="stable")], 2)
    left = left[np.argsort(left[:, 1], kind="stable")]
    right = right[np.argsort(right[:, 1], kind="stable")]
    return np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)


def crop_line(bgr: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """透视回正检测四边形；竖长文本旋转为识别器需要的横向行图。"""
    quad = order_corners(quad)
    width = max(1, round(max(np.linalg.norm(quad[1] - quad[0]), np.linalg.norm(quad[2] - quad[3]))))
    height = max(1, round(max(np.linalg.norm(quad[3] - quad[0]), np.linalg.norm(quad[2] - quad[1]))))
    target = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype=np.float32)
    if min(width, height) < 2:
        raise ValueError("Text crop is too small for perspective sampling")
    crop = warp_image(bgr, perspective_matrix(quad, target), (width, height), interpolation="cubic", border="replicate")
    return np.ascontiguousarray(np.rot90(crop) if height / width >= 1.5 else crop)


def decode_ctc(prediction: np.ndarray, characters: list[str]) -> tuple[str, float]:
    """去除 CTC blank 和连续重复字符，置信度仅统计实际保留的字符。"""
    if prediction.ndim != 2 or prediction.shape[1] != len(characters) or not np.isfinite(prediction).all():
        raise ValueError("OCR recognition output does not match the character dictionary")
    indices = prediction.argmax(axis=1)
    keep = (indices != 0) & np.concatenate(([True], indices[1:] != indices[:-1]))
    selected = indices[keep]
    return "".join(characters[int(index)] for index in selected), float(
        prediction.max(axis=1)[keep].mean()
    ) if keep.any() else 0.0


class TextModel:
    """组合 Tiny Det 和 Small Rec，提供按块裁图的轻量文本识别。"""

    def __init__(self, root: Path) -> None:
        """读取配套运行参数和内嵌字符表，不访问任何 MinerU 包内文件。"""
        directory = root / "OCR" / "paddleocr"
        det_config = read_config(directory / "ch_PP-OCRv6_tiny_det_inference.yml")
        rec_config = read_config(directory / "ch_PP-OCRv6_small_rec_inference.yml")
        self.det_config = det_config["MinerU"]
        self.rec_config = rec_config["MinerU"]
        dictionary = rec_config["PostProcess"]["character_dict"]
        if (
            not isinstance(dictionary, list)
            or len(dictionary) != 18708
            or not all(isinstance(item, str) for item in dictionary)
        ):
            raise ValueError("Invalid PP-OCRv6 character dictionary")
        self.characters = [""] + dictionary + [" "]
        self.detector = create_session(directory / "ch_PP-OCRv6_tiny_det_infer.onnx")
        self.recognizer = create_session(directory / "ch_PP-OCRv6_small_rec_infer.onnx")
        if self.recognizer.get_outputs()[0].shape[-1] != len(self.characters):
            raise ValueError("OCR recognition graph and dictionary disagree")

    def _detect(self, bgr: np.ndarray, *, table: bool = False) -> list[np.ndarray]:
        """检测文本四边形，按配置归一化并将概率图框回映射到原裁图。"""
        height, width = bgr.shape[:2]
        ratio = min(1.0, self.det_config["limit_side_len"] / max(height, width))
        resized_h, resized_w = (max(32, round(value * ratio / 32) * 32) for value in (height, width))
        resized = resize_image(bgr, (resized_w, resized_h), interpolation="linear")
        norm = resized.astype(np.float32) / 255.0 - np.array([0.485, 0.456, 0.406], dtype=np.float32)
        norm /= np.array([0.229, 0.224, 0.225], dtype=np.float32)
        pixels = np.ascontiguousarray(norm.transpose(2, 0, 1)[None])
        output = self.detector.run(None, {"x": pixels})[0]
        if output.ndim != 4 or output.shape[:2] != (1, 1) or not np.isfinite(output).all():
            raise ValueError("Invalid OCR detection output")
        return self._boxes(output[0, 0], (width, height), table=table)

    def _boxes(self, probability: np.ndarray, size: tuple[int, int], *, table: bool = False) -> list[np.ndarray]:
        """从 DB 概率图计算多边形均值、膨胀和最小矩形，不引入 OpenCV。"""
        import pyclipper

        mask = (probability > self.det_config["thresh"]).astype(np.uint8)
        contours = trace_contours(mask)
        map_height, map_width = probability.shape
        boxes = []
        for contour in contours[: self.det_config["max_candidates"]]:
            if len(contour) < 3:
                continue
            rectangle = minimum_rectangle(contour)
            if min(rectangle[1]) < 3:
                continue
            corners = rectangle_corners(rectangle)
            x0, y0 = np.maximum(np.floor(corners.min(axis=0)).astype(int), 0)
            x1, y1 = np.minimum(np.ceil(corners.max(axis=0)).astype(int) + 1, [map_width, map_height])
            region_mask = rasterize_polygons((y1 - y0, x1 - x0), [corners - [x0, y0]])
            selected = probability[y0:y1, x0:x1][region_mask != 0]
            if selected.size == 0 or selected.mean() < self.det_config["box_thresh"]:
                continue
            length = contour_length(corners)
            distance = contour_area(corners) * (1.6 if table else self.det_config["unclip_ratio"]) / max(length, 1e-6)
            offset = pyclipper.PyclipperOffset()
            offset.AddPath(np.rint(corners * 1024).astype(np.int64).tolist(), pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
            expanded = offset.Execute(distance * 1024)
            if len(expanded) != 1:
                continue
            rectangle = minimum_rectangle(np.asarray(expanded[0], dtype=np.float32) / 1024)
            if min(rectangle[1]) < 5:
                continue
            corners = order_corners(rectangle_corners(rectangle))
            corners *= np.array([size[0] / map_width, size[1] / map_height], dtype=np.float32)
            corners = np.clip(corners, 0, [size[0] - 1, size[1] - 1]).astype(np.float32)
            if min(np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[3] - corners[0])) > 3:
                boxes.append(corners)
        # 同一行使用左到右顺序，行间按上边界递增；容差取检测行高的中位数。
        boxes.sort(key=lambda box: (float(box[:, 1].min()), float(box[:, 0].min())))
        rows: list[list[np.ndarray]] = []
        for box in boxes:
            if rows:
                previous = rows[-1][0]
                tolerance = 0.5 * min(np.ptp(previous[:, 1]), np.ptp(box[:, 1]))
                if abs(float(box[:, 1].mean() - previous[:, 1].mean())) <= tolerance:
                    rows[-1].append(box)
                    continue
            rows.append([box])
        return [box for row in rows for box in sorted(row, key=lambda box: float(box[:, 0].min()))]

    def _recognize(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        """按宽高比合批，使用动态宽度及归一化零填充，最后恢复检测行顺序。"""
        results = [("", 0.0)] * len(crops)
        order = sorted(range(len(crops)), key=lambda index: crops[index].shape[1] / crops[index].shape[0])
        _, height, base_width = self.rec_config["image_shape"]
        batch_size = self.rec_config["rec_batch_num"]
        for start in range(0, len(order), batch_size):
            indices = order[start : start + batch_size]
            width = max(base_width, max(math.ceil(height * crops[index].shape[1] / crops[index].shape[0]) for index in indices))
            width = min(self.rec_config["max_width"], width)
            pixels = np.zeros((len(indices), 3, height, width), dtype=np.float32)
            for position, index in enumerate(indices):
                crop = crops[index]
                target_width = max(self.rec_config["min_width"], min(width, math.ceil(height * crop.shape[1] / crop.shape[0])))
                resized = resize_image(crop, (target_width, height), interpolation="linear")
                pixels[position, :, :, :target_width] = resized.transpose(2, 0, 1).astype(np.float32) / 127.5 - 1.0
            output = self.recognizer.run(None, {"x": pixels})[0]
            if output.ndim != 3 or output.shape[0] != len(indices):
                raise ValueError("Invalid OCR recognition batch")
            for index, prediction in zip(indices, output, strict=True):
                results[index] = decode_ctc(prediction, self.characters)
        return results

    def predict(self, rgb: np.ndarray, *, table: bool = False) -> list[TextLine]:
        """仅在局部副本转换 RGB 到 BGR，白边保护贴边文字，并回映射检测坐标。"""
        if rgb.size == 0 or np.ptp(rgb) == 0:
            return []
        padding = 32
        bgr = np.pad(rgb[..., ::-1], ((padding, padding), (padding, padding), (0, 0)), constant_values=255)
        boxes = self._detect(bgr, table=table)
        crops = [crop_line(bgr, box) for box in boxes]
        recognized = self._recognize(crops)
        height, width = rgb.shape[:2]
        lines = []
        for box, (text, score) in zip(boxes, recognized, strict=True):
            text = text.strip()
            if not text or score < 0.5:
                continue
            quad = np.clip(box - padding, 0, [width, height]).astype(np.float32)
            if np.ptp(quad[:, 0]) > 0 and np.ptp(quad[:, 1]) > 0:
                lines.append(TextLine(quad, text, score))
        return lines
