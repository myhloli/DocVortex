"""Magika provides a limited number of threads for file type and code language recognition."""

import time

import onnxruntime as ort
from magika import Magika as BaseMagika
from magika import __version__ as _MAGIKA_VERSION


class Magika(BaseMagika):
    """Fixed CPU session to 4/1 only for Magika, preserving model and recognition behavior."""

    def _init_onnx_session(self) -> ort.InferenceSession:
        """Magika There is no public session parameter entry yet, and the number of threads within and between operators is limited in this centralized setting."""
        started = time.perf_counter()
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(self._model_path, sess_options=options, providers=["CPUExecutionProvider"])
        elapsed_ms = 1000 * (time.perf_counter() - started)
        self._log.debug(f'ONNX DL model "{self._model_path}" loaded in {elapsed_ms:.03f} ms')
        return session

    def get_module_version(self) -> str:
        """Maintain the upstream version identification to prevent the base class from falsely reporting the DocVortex version based on the subclass module name."""
        return _MAGIKA_VERSION


__all__ = ["Magika"]
