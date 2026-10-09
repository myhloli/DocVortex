"""按固定校验清单获取基础 OCR 模型，不依赖模型平台 SDK。"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from http.client import HTTPException
import os
from pathlib import Path
import tempfile
from threading import RLock
from urllib.parse import quote
from urllib.request import Request, urlopen

from loguru import logger

from ...errors import DocumentError

HF_REVISION = "358310b4f64b95f9fefc372ad899356e4111f376"
MS_REVISION = "e916a9d96d645d005364abd7db8ffd4b7353cf4f"


@dataclass(frozen=True)
class ModelFile:
    """记录固定模型资源的相对路径、大小和内容校验值。"""

    path: str
    size: int
    digest: str


MODEL_FILES = (
    ModelFile(
        "Layout/PP-DocLayoutV2/inference.onnx", 213963712, "cd540dc296ff3115fe78efa65b68501e6a8dc74b198acc9834a725ffaa095aac"
    ),
    ModelFile("Layout/PP-DocLayoutV2/inference.yml", 1482, "93100e0fb2cd76b9d246a853deea85e191d8333b2cfca7be76e82e4c4ebfa819"),
    ModelFile(
        "OCR/paddleocr/ch_PP-OCRv6_tiny_det_infer.onnx",
        1780590,
        "193bab7a04fca699a6c82e6abb5b81bdb28177f0abd4062552b04908dafb19f8",
    ),
    ModelFile(
        "OCR/paddleocr/ch_PP-OCRv6_tiny_det_inference.yml",
        1068,
        "a1fa68ab31e29252936328e728633180908f997023d9eacb81f175e326676554",
    ),
    ModelFile(
        "OCR/paddleocr/ch_PP-OCRv6_small_rec_infer.onnx",
        21159378,
        "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634",
    ),
    ModelFile(
        "OCR/paddleocr/ch_PP-OCRv6_small_rec_inference.yml",
        150752,
        "c0541b06832948d7d3052e9ac6b572c6063d79ecd800493f4ec14d1b901c16d7",
    ),
)
_VERIFIED: dict[Path, tuple[object, ...]] = {}
_VERIFY_LOCK = RLock()


def model_root() -> Path:
    """返回当前用户的固定 OCR 模型目录，不在导入时创建目录。"""
    return Path.home() / ".docvortex" / "model" / "MinerU-4_models_onnx"


def _is_valid(path: Path, asset: ModelFile) -> bool:
    """首次流式校验内容，之后仅在文件身份或时间戳改变时重新计算。"""
    with _VERIFY_LOCK:
        try:
            stat = path.stat()
            if not path.is_file() or stat.st_size != asset.size:
                return False
            stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, asset.digest)
            if _VERIFIED.get(path) == stamp:
                return True
            digest = sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != asset.digest:
                return False
            _VERIFIED[path] = stamp
            return True
        except OSError:
            return False


def _asset_url(source: str, asset: ModelFile) -> str:
    """构造固定 revision 的资源 URL，两个来源必须通过相同内容校验。"""
    name = quote(asset.path, safe="/")
    if source == "huggingface":
        return f"https://huggingface.co/opendatalab/MinerU-4_models_onnx/resolve/{HF_REVISION}/{name}"
    return (
        "https://modelscope.cn/api/v1/models/OpenDataLab/MinerU-4_models_onnx/repo"
        f"?Revision={MS_REVISION}&FilePath={quote(asset.path, safe='')}"
    )


def _download_asset(asset: ModelFile, destination: Path, source: str) -> None:
    """流式下载到同目录临时文件，校验完整后原子替换，任何退出都清理临时文件。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
    temporary = Path(name)
    try:
        request = Request(_asset_url(source, asset), headers={"User-Agent": "DocVortex-OCR/1"})
        digest, size = sha256(), 0
        with os.fdopen(descriptor, "wb") as output:
            with urlopen(request, timeout=15) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > asset.size:
                        raise ValueError("response exceeds the expected model size")
                    digest.update(chunk)
                    output.write(chunk)
            if size != asset.size or digest.hexdigest() != asset.digest:
                raise ValueError("model size or SHA-256 mismatch")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_models(root: Path | None = None) -> Path:
    """优先使用完整离线缓存；缺失资源按 HF、ModelScope 顺序下载并跨进程串行化。"""
    from filelock import FileLock, Timeout

    root = (root if root is not None else model_root()).expanduser().resolve()
    if all(_is_valid(root / asset.path, asset) for asset in MODEL_FILES):
        return root
    try:
        root.mkdir(parents=True, exist_ok=True)
        with FileLock(str(root / ".download.lock"), timeout=300):
            sources = ["huggingface", "modelscope"]
            for asset in MODEL_FILES:
                destination = root / asset.path
                if _is_valid(destination, asset):
                    continue
                failures = []
                for source in sources:
                    logger.info("Downloading OCR model {} from {}", asset.path, source)
                    try:
                        _download_asset(asset, destination, source)
                    except (OSError, ValueError, HTTPException) as exc:
                        failures.append(f"{source}: {exc}")
                        logger.warning("OCR model download failed: {}: {}", source, exc)
                    else:
                        sources = [source] + [item for item in sources if item != source]
                        break
                else:
                    raise DocumentError("ocr_model_download_failed", f"Could not download {asset.path}: {'; '.join(failures)}")
        return root
    except (OSError, Timeout) as exc:
        raise DocumentError("ocr_model_download_failed", f"Could not prepare OCR model cache {root}: {exc}") from exc
