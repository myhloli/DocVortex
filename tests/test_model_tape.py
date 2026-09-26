"""冻结模型边界保留真实 PDF 工作及输入副作用，不依赖 MinerU 或真实模型。"""

from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from tools.mineru_model_tape import ModelTape, ModelProxy, ContextProxy


def test_tape_preserves_shared_table_side_effects_and_pixels():
    """分类与表格模型可删除/添加共享结果字段，回放后外部别名仍指向原字典。"""

    def predict(tasks):
        """模拟真实分类/表格 batch_predict 的原地结果回填。"""
        tasks[0]["table_res"].pop("old")
        tasks[0]["table_res"]["html"] = "<table><tr><td>A</td></tr></table>"
        tasks[0]["cell_boxes"] = np.array([[0, 0, 4, 4]], dtype=np.float32)
        return None

    shared = {"old": "unused"}
    pixels = np.arange(48, dtype=np.uint8).reshape((4, 4, 3))
    tasks = [{"table_res": shared, "table_img": pixels}, {"table_res": shared}]
    recorded = deepcopy(tasks)
    tape = ModelTape(record=True)
    tape.bind("table", lambda: predict)(recorded)
    tape.complete()
    replay = ModelTape(data=tape.data, verify_inputs=True)
    original_dictionary = tasks[0]["table_res"]
    original_pixels = tasks[0]["table_img"]
    assert replay.bind("table", lambda: None)(tasks) is None
    replay.complete()
    assert tasks[0]["table_res"] is original_dictionary is tasks[1]["table_res"]
    assert tasks[0]["table_img"] is original_pixels
    assert tasks[0]["table_res"] == recorded[0]["table_res"]
    np.testing.assert_array_equal(tasks[0]["cell_boxes"], recorded[0]["cell_boxes"])


def test_tape_rejects_changed_pixels_and_extra_or_missing_calls():
    """模型输入校验与调用覆盖不能因参数形状相同或分析器吞异常而被绕过。"""

    def predict(image):
        """固定返回值故意不依赖像素，输入差异仍需单独检测。"""
        return [{"text": "A"}]

    tape = ModelTape(record=True)
    tape.bind("layout", lambda: predict)(np.zeros((2, 2), dtype=np.uint8))
    replay = ModelTape(data=tape.data, verify_inputs=True)
    with pytest.raises(AssertionError, match="input changed"):
        replay.bind("layout", lambda: None)(np.ones((2, 2), dtype=np.uint8))
    with pytest.raises(AssertionError, match="failed"):
        replay.complete()
    replay.reset()
    with pytest.raises(AssertionError):
        replay.complete()
    result = replay.bind("layout", lambda: None)(np.zeros((2, 2), dtype=np.uint8))
    result[0]["text"] = "caller mutation"
    replay.complete()
    with pytest.raises(AssertionError, match="Additional"):
        replay.bind("layout", lambda: None)(np.zeros((2, 2), dtype=np.uint8))
    replay.reset()
    assert replay.bind("layout", lambda: None)(np.zeros((2, 2), dtype=np.uint8))[0]["text"] == "A"


def test_proxy_loads_only_used_models_and_preserves_signature():
    """录制时惰性加载，回放不触发模型工厂，OCR 参数探测仍看到原签名。"""
    import inspect

    calls = []

    def predict(images, *, batch_size=4):
        """模拟需要按签名选择 batch_size 的模型。"""
        calls.append(batch_size)
        return len(images)

    tape = ModelTape(record=True)
    context = ContextProxy(tape, SimpleNamespace(device="cpu", layout_model=SimpleNamespace(batch_predict=predict)))
    assert context.layout_model.batch_predict([1, 2]) == 2
    assert calls == [4]
    replay = ModelTape(data=tape.data)
    model = ContextProxy(replay).layout_model
    assert inspect.signature(model.batch_predict) == inspect.signature(predict)
    assert model.batch_predict([1, 2]) == 2
    assert calls == [4]
    replay.complete()
    with pytest.raises(AttributeError):
        _ = model.unknown_method


def test_record_failure_cannot_be_swallowed_by_algorithm_fallback():
    """业务代码即使捕获模型失败，验收也不能产生缺失事件的成功录制。"""

    def fail(items):
        """模拟模型执行失败。"""
        raise RuntimeError("inference failed")

    tape = ModelTape(record=True)
    proxy = ModelProxy(tape, "model", lambda: SimpleNamespace(predict=fail))
    with pytest.raises(RuntimeError):
        proxy.predict([])
    with pytest.raises(AssertionError, match="failed"):
        tape.complete()
