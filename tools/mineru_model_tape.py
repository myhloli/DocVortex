"""仅供本机可信录制的模型边界回放；不缓存 PDF 分析、渲染或素材结果。"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import inspect
import json
import time


def input_digest(value):
    """按值冻结模型输入，覆盖数组/PIL 像素、容器类型与标量；未知类型明确拒绝。"""
    import numpy as np
    from PIL.Image import Image

    def normalize(item):
        """将输入转换为无对象地址的稳定结构，保留字典键类型及序列顺序。"""
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
    """记录模型输入上的可见修改，按路径更新以保留表格任务与结果字典之间的别名。"""
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
    """重放原地修改，而非替换整个参数树，防止丢失分析器持有的引用关系。"""
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
    """只录制最外层模型调用、返回值及输入修改，递归模型调用包含在外层边界内。"""

    def __init__(self, *, record=False, data=None, verify_inputs=False):
        """计时回放关闭像素摘要，正确性回放单独开启严格输入校验。"""
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
        """每次文档重新从第一条模型结果开始，不保留上一轮分析产物。"""
        self.position = 0
        self.callback_seconds = 0.0
        self.model_seconds = 0.0
        self.counts.clear()
        self.failed_calls.clear()

    def complete(self):
        """必须消费全部模型事件，防止静默跳过模型调用后伪报加速。"""
        if self.failed_calls:
            raise AssertionError(f"Model tape has failed or unsupported calls: {self.failed_calls}")
        if not self.record and self.position != len(self.data["calls"]):
            raise AssertionError((self.position, len(self.data["calls"])))

    def bind(self, name, resolve):
        """按需加载录制模型，回放仅使用已保存签名，不实例化任何模型。"""
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
            """冻结边界包含返回值和原地副作用，异常不转为空结果或静默降级。"""
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
    """按属性名惰性连接模型方法，避免录制时无条件加载未使用的大模型。"""

    def __init__(self, tape, name, resolve):
        """解析函数仅在真实录制访问模型时执行，回放不触碰模型工厂。"""
        self._tape = tape
        self._name = name
        self._resolve = resolve
        self._cache = {}

    def __getattr__(self, name):
        """只允许已明确的模型方法和 OCR 子模型；未知访问立即报错。"""
        if name in self._cache:
            return self._cache[name]

        def resolve():
            """延迟取到真实模型成员，保留现有模型的内部调用与配置。"""
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
    """仅代理模型边界，PDF 窗口、表格恢复、文本证据与素材路径保持真实执行。"""

    def __init__(self, tape, original=None):
        """保存设备配置使生命周期行为一致，回放使用录制时的轻量元数据。"""
        self._tape = tape
        self._original = original
        self._cache = {}
        if tape.record:
            tape.data["context"] = {"device": str(original.device)}
        self.device = tape.data["context"]["device"]

    def __getattr__(self, name):
        """为实际用到的模型属性创建代理，不替换分析阶段函数。"""
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
                """仅录制时访问对应真实模型属性。"""
                return getattr(self._original, name)

            self._cache[name] = ModelProxy(self._tape, name, resolve)
        return self._cache[name]

    def get_ocr_model(self, *args, **kwargs):
        """不同 OCR 配置使用不同事件名，避免表格 OCR 和正文 OCR 错配。"""
        name = "configured_ocr:" + input_digest((args, kwargs))
        if name not in self._cache:

            def resolve():
                """保留真实 OCR 工厂参数，仅在录制模型调用时初始化。"""
                return self._original.get_ocr_model(*args, **kwargs)

            self._cache[name] = ModelProxy(self._tape, name, resolve)
        return self._cache[name]
