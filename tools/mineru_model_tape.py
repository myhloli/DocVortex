"""Model boundary playback for natively trusted recordings only; PDF analysis, rendering, or footage results are not cached."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import inspect
import json
import time


def input_digest(value):
    """Freeze model input by value, overrides array/PIL pixels, container types and scalars; unknown types are explicitly rejected."""
    import numpy as np
    from PIL.Image import Image

    def normalize(item):
        """Convert the input into a stable structure without object addresses, preserving dictionary key types and sequence order."""
        if isinstance(item, np.ndarray):
            return ["array", str(item.dtype), item.shape, hashlib.sha256(item.tobytes()).hexdigest()]
        if isinstance(item, Image):
            return ["image", item.mode, item.size, hashlib.sha256(item.tobytes()).hexdigest()]
        if isinstance(item, np.generic):
            return normalize(item.item())
        if isinstance(item, bytes):
            return ["bytes", hashlib.sha256(item).hexdigest()]
        if isinstance(item, dict):
            pairs = [(normalize(key), normalize(part)) for key, part in item.items()]
            return ["dict", sorted(pairs, key=lambda pair: json.dumps(pair[0], sort_keys=True))]
        if isinstance(item, (tuple, list, set, frozenset)):
            parts = [normalize(part) for part in item]
            if isinstance(item, (set, frozenset)):
                parts.sort(key=lambda part: json.dumps(part, sort_keys=True))
            return [type(item).__name__, parts]
        if item is None or isinstance(item, (str, int, float, bool)):
            return [type(item).__name__, item]
        raise TypeError(f"Unsupported model input: {type(item)}")

    return hashlib.sha256(
        json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def mutation_patches(before, after, path=(), seen=None):
    """Record visible modifications on model inputs, updated by path to preserve aliases between table tasks and results dictionaries."""
    import numpy as np
    from PIL.Image import Image

    if before is after:
        return []
    if seen is None:
        seen = set()
    if isinstance(before, (dict, list, tuple, np.ndarray, Image)):
        pair = (id(before), id(after))
        if pair in seen:
            return []
        seen.add(pair)
    if type(before) is not type(after):
        return [("set", path, deepcopy(after))]
    if isinstance(before, dict):
        patches = [("delete", (*path, key), None) for key in before if key not in after]
        for key, value in after.items():
            if key not in before:
                patches.append(("set", (*path, key), deepcopy(value)))
            else:
                patches.extend(mutation_patches(before[key], value, (*path, key), seen))
        return patches
    if isinstance(before, (tuple, list)):
        if len(before) != len(after):
            return [("list" if isinstance(after, list) else "set", path, deepcopy(after))]
        return [
            patch for index, (a, b) in enumerate(zip(before, after)) for patch in mutation_patches(a, b, (*path, index), seen)
        ]
    if isinstance(before, np.ndarray):
        if before.shape == after.shape and before.dtype == after.dtype and before.tobytes() == after.tobytes():
            return []
        operation = "array" if before.shape == after.shape and before.dtype == after.dtype else "set"
        return [(operation, path, deepcopy(after))]
    if isinstance(before, Image):
        if before.mode == after.mode and before.size == after.size and before.tobytes() == after.tobytes():
            return []
        return [("image", path, after.copy())]
    if input_digest(before) == input_digest(after):
        return []
    return [("set", path, deepcopy(after))]


def apply_mutations(root, patches):
    """Replay modifications in place rather than replacing the entire parameter tree to prevent losing references held by the parser."""
    for operation, path, value in patches:
        target = root
        for key in path[:-1]:
            target = target[key]
        if operation == "set":
            target[path[-1]] = deepcopy(value)
        elif operation == "delete":
            del target[path[-1]]
        else:
            target = target[path[-1]] if path else root
            if operation == "list":
                target[:] = deepcopy(value)
            elif operation == "array":
                target[...] = value
            elif operation == "image":
                if target.mode != value.mode or target.size != value.size:
                    raise ValueError("Cannot replay in-place PIL mode or size mutation")
                target.paste(value)
            else:
                raise ValueError(f"Unknown model mutation: {operation}")


class ModelTape:
    """Only the outermost model calls, return values, and input modifications are recorded, and recursive model calls are included within the outer boundaries."""

    def __init__(self, *, record=False, data=None, verify_inputs=False):
        """Turn off pixel summarization for timing playback, and turn on strict input verification separately for correctness playback."""
        self.record = record
        self.data = data if data is not None else {"version": 1, "calls": [], "signatures": {}}
        if self.data["version"] != 1:
            raise ValueError("Unsupported model tape version")
        self.verify_inputs = verify_inputs
        self.position = 0
        self.depth = 0
        self.callback_seconds = 0.0
        self.model_seconds = 0.0
        self.counts = Counter()
        self.failed_calls = []

    def reset(self):
        """Each time the document starts from the first model result, the previous round of analysis products will not be retained."""
        self.position = 0
        self.callback_seconds = 0.0
        self.model_seconds = 0.0
        self.counts.clear()
        self.failed_calls.clear()

    def complete(self):
        """All model events must be consumed to prevent false report acceleration after silently skipping model calls."""
        if self.failed_calls:
            raise AssertionError(f"Model tape has failed or unsupported calls: {self.failed_calls}")
        if not self.record and self.position != len(self.data["calls"]):
            raise AssertionError((self.position, len(self.data["calls"])))

    def bind(self, name, resolve):
        """The recorded model is loaded on demand, and playback only uses the saved signature, without instantiating any model."""
        if self.record:
            try:
                original = resolve()
                self.data["signatures"][name] = inspect.signature(original)
            except Exception as error:
                self.failed_calls.append((name, type(error).__name__, str(error)))
                raise
        else:
            original = None
            if name not in self.data["signatures"]:
                raise AssertionError(f"Unrecorded model method: {name}")

        def call(*values, **options):
            """Freeze boundaries include return values and in-place side effects, and exceptions are not converted to empty results or silently degraded."""
            if self.depth:
                if original is None:
                    raise AssertionError("Unexpected nested replay")
                return original(*values, **options)
            started = time.perf_counter()
            self.depth += 1
            self.counts[name] += 1
            arguments = (values, options)
            try:
                if self.record:
                    fingerprint = input_digest(arguments)
                    before = deepcopy(arguments)
                    model_started = time.perf_counter()
                    result = original(*values, **options)
                    model_seconds = time.perf_counter() - model_started
                    self.model_seconds += model_seconds
                    self.data["calls"].append(
                        {
                            "name": name,
                            "input_sha256": fingerprint,
                            "result": deepcopy(result),
                            "mutations": mutation_patches(before, arguments),
                            "model_seconds": model_seconds,
                        }
                    )
                    return result
                if self.position >= len(self.data["calls"]):
                    raise AssertionError(f"Additional model call: {name}")
                event = self.data["calls"][self.position]
                if event["name"] != name:
                    raise AssertionError((name, event["name"], self.position))
                if self.verify_inputs and input_digest(arguments) != event["input_sha256"]:
                    raise AssertionError(f"Model input changed: {name} event {self.position}")
                self.position += 1
                apply_mutations(arguments, event["mutations"])
                return deepcopy(event["result"])
            except Exception as error:
                self.failed_calls.append((name, type(error).__name__, str(error)))
                raise
            finally:
                self.depth -= 1
                self.callback_seconds += time.perf_counter() - started

        call.__signature__ = self.data["signatures"][name]
        return call


class ModelProxy:
    """Lazy connection model method by attribute name to avoid unconditional loading of unused large models during recording."""

    def __init__(self, tape, name, resolve):
        """The parsing function is only executed when the actual recording accesses the model, and playback does not touch the model factory."""
        self._tape = tape
        self._name = name
        self._resolve = resolve
        self._cache = {}

    def __getattr__(self, name):
        """Only specified model methods and OCR submodels are allowed; unknown access will result in an error immediately."""
        if name in self._cache:
            return self._cache[name]

        def resolve():
            """Delayed retrieval of real model members, retaining the internal calls and configuration of existing models."""
            return getattr(self._resolve(), name)

        qualified = f"{self._name}.{name}"
        if name in {"batch_predict", "predict", "ocr", "batch_extract_with_layout", "batch_two_step_extract"}:
            value = self._tape.bind(qualified, resolve)
        elif name in {"text_detector", "text_recognizer"}:
            value = ModelProxy(self._tape, qualified, resolve)
        else:
            raise AttributeError(f"Unsupported model proxy attribute: {qualified}")
        self._cache[name] = value
        return value


class ContextProxy:
    """Only proxy model boundaries, PDF windows, table restoration, text evidence and material paths remain true to execution."""

    def __init__(self, tape, original=None):
        """Save device configuration for consistent lifecycle behavior, and playback uses lightweight metadata at the time of recording."""
        self._tape = tape
        self._original = original
        self._cache = {}
        if tape.record:
            tape.data["context"] = {"device": str(original.device)}
        self.device = tape.data["context"]["device"]

    def __getattr__(self, name):
        """Create proxies for the actual model attributes used, without replacing the analysis phase functions."""
        if name not in {
            "ocr_model",
            "layout_model",
            "mfr_model",
            "seal_model",
            "table_orientation_cls_model",
            "table_cls_model",
            "wireless_table_model",
            "wired_table_model",
        }:
            raise AttributeError(name)
        if name not in self._cache:

            def resolve():
                """The corresponding real model properties are only accessed during recording."""
                return getattr(self._original, name)

            self._cache[name] = ModelProxy(self._tape, name, resolve)
        return self._cache[name]

    def get_ocr_model(self, *args, **kwargs):
        """Different OCR configurations use different event names to avoid mismatch between table OCR and text OCR."""
        name = "configured_ocr:" + input_digest((args, kwargs))
        if name not in self._cache:

            def resolve():
                """Keep the real OCR factory parameters and only initialize when recording model calls."""
                return self._original.get_ocr_model(*args, **kwargs)

            self._cache[name] = ModelProxy(self._tape, name, resolve)
        return self._cache[name]
