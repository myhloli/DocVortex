"""Freezing model boundaries preserves real PDF working and input side effects, and does not rely on MinerU or real models."""

from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pytest

from tools.mineru_model_tape import ModelTape, ModelProxy, ContextProxy


def test_tape_preserves_shared_table_side_effects_and_pixels():
    """Classification and table models can delete/add shared result fields, and the external alias still points to the original dictionary after playback."""

    def predict(tasks):
        """Simulates in-place result backfilling of real classification/table batch_predict."""
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
    """Model input validation and call coverage cannot be bypassed due to identical parameter shapes or parser exceptions."""

    def predict(image):
        """Fixed return values are deliberately not pixel dependent, input differences still need to be detected separately."""
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
    """Lazy loading during recording, model factory not triggered during playback, OCR parameter detection still sees the original signature."""
    import inspect

    calls = []

    def predict(images, *, batch_size=4):
        """Simulation requires the model batch_size to be selected by signature."""
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
    """Even if the business code fails to capture the model, acceptance cannot produce successful recording of missing events."""

    def fail(items):
        """Simulation model execution failed."""
        raise RuntimeError("inference failed")

    tape = ModelTape(record=True)
    proxy = ModelProxy(tape, "model", lambda: SimpleNamespace(predict=fail))
    with pytest.raises(RuntimeError):
        proxy.predict([])
    with pytest.raises(AssertionError, match="failed"):
        tape.complete()
