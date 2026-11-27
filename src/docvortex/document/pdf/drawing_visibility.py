"""供原生与 Tagged 两条流水线共用的真实绘制开关和局部纸面采样。"""

from contextlib import closing, contextmanager
import ctypes as ct

import pypdfium2.raw as raw


class DrawingIsolationError(RuntimeError):
    """源对象无法隔离，已临时关闭的其他绘制会在退出时恢复。"""


@contextmanager
def hide_drawings(drawings):
    """暂时关闭参与绘制的成员，并在退出时恢复每个调用前活跃的对象。"""
    changed = []
    active = ct.c_int()
    try:
        for obj in drawings:
            if not raw.FPDFPageObj_GetIsActive(obj, ct.byref(active)):
                raise DrawingIsolationError("Cannot inspect PDF object activity")
            if not active.value:
                continue
            if not raw.FPDFPageObj_SetIsActive(obj, 0):
                raise DrawingIsolationError("Cannot isolate PDF graphic members")
            changed.append(obj)
        yield
    finally:
        restored = [raw.FPDFPageObj_SetIsActive(obj, 1) for obj in changed]
        if not all(restored):
            raise RuntimeError("Cannot restore PDF object activity")


def render_source_patch(page, scale, crop, *, transparent=False):
    """按与公开 PNG 相同的整页采样相位直接渲染局部画布。"""
    color = (0, 0, 0, 0) if transparent else (255, 255, 255, 255)
    with closing(page.render(scale=scale, crop=crop, fill_color=color)) as bitmap, bitmap.to_pil() as image:
        return image.copy()
