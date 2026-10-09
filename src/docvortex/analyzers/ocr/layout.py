"""依据公开模型配置和 ONNX 输入输出契约独立实现版面推理。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...image import resize_image
from .runtime import create_session, read_config


@dataclass(frozen=True)
class LayoutRegion:
    """保存原图像素坐标中的类别、区域和置信度，列表位置就是阅读顺序。"""

    label: str
    bbox: tuple[float, float, float, float]
    score: float


class LayoutModel:
    """运行 PP-DocLayoutV2，直接采用图中的阅读顺序输出。"""

    def __init__(self, root: Path) -> None:
        """从配套 YAML 加载标签及缩放配置，并创建固定布局图的 CPU 会话。"""
        directory = root / "Layout" / "PP-DocLayoutV2"
        config = read_config(directory / "inference.yml")
        self.labels = config["label_list"]
        resize = next(item for item in config["Preprocess"] if item["type"] == "Resize")
        self.height, self.width = map(int, resize["target_size"])
        self.interpolation = {0: "nearest", 1: "linear", 2: "cubic", 3: "area", 4: "lanczos4"}[resize["interp"]]
        self.session = create_session(directory / "inference.onnx")

    def predict(self, rgb: np.ndarray) -> list[LayoutRegion]:
        """按 RGB 预处理，校验框数量并按双顺序键稳定排序，裁掉无效区域。"""
        height, width = rgb.shape[:2]
        pixels = resize_image(rgb, (self.width, self.height), interpolation=self.interpolation)
        pixels = np.ascontiguousarray(pixels.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
        outputs = self.session.run(
            None,
            {
                "image": pixels,
                "im_shape": np.array([[self.height, self.width]], dtype=np.float32),
                "scale_factor": np.array([[self.height / height, self.width / width]], dtype=np.float32),
            },
        )
        if len(outputs) != 2:
            raise ValueError("Layout model must return boxes and counts")
        boxes, counts = outputs
        if boxes.ndim != 2 or boxes.shape[1] != 8 or counts.shape != (1,) or counts.dtype.kind not in "iu":
            raise ValueError("Invalid layout output signature")
        if int(counts[0]) != len(boxes):
            raise ValueError("Layout box count does not match the output")
        boxes = boxes[np.isfinite(boxes).all(axis=1) & (boxes[:, 1] >= 0.45)]
        boxes = boxes[np.lexsort((-boxes[:, 7], boxes[:, 6]))]
        regions = []
        for row in boxes:
            label = int(row[0])
            if row[0] != label or not 0 <= label < len(self.labels):
                raise ValueError("Invalid layout class ID")
            x0, y0, x1, y1 = np.clip(row[2:6], 0, [width, height, width, height])
            if x1 > x0 and y1 > y0:
                regions.append(LayoutRegion(self.labels[label], (float(x0), float(y0), float(x1), float(y1)), float(row[1])))
        return regions
