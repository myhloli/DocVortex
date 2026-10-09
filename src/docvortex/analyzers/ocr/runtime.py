"""固定 ONNX 模型的配置与 CPU 会话，权重校验由下载层负责。"""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from threading import RLock
from typing import Any

from ...errors import DocumentError

_RUNTIME_LOCK = RLock()


def read_config(path: Path) -> dict[str, Any]:
    """安全读取模型 YAML，拒绝不符合预期的顶层结构。"""
    import yaml

    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Invalid OCR model configuration: {path}")
    return value


def create_session(path: Path):
    """仅初始化 CPU 执行器，限制线程数量，避免多个会话争抢全部核心。"""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = min(4, os.cpu_count() or 1)
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


@lru_cache(maxsize=1)
def _cached_runtime(root: str, pid: int):
    """按进程和目录复用一套模型，避免跨 fork 使用父进程的 ONNX 会话。"""
    from .layout import LayoutModel
    from .text import TextModel

    directory = Path(root)
    try:
        return LayoutModel(directory), TextModel(directory)
    except Exception as exc:
        raise DocumentError("ocr_model_invalid", f"Could not initialize OCR models in {directory}: {exc}") from exc


def get_runtime():
    """进入 OCR 时才校验或下载模型，并获取进程内可复用的运行时。"""
    from .download import ensure_models

    with _RUNTIME_LOCK:
        root = ensure_models()
        return _cached_runtime(str(root), os.getpid())
