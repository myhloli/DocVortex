"""从最终正文行框识别反复错开一行的段界。"""

from ....content.spans import inline_span_plain_text


def paragraph_boundary_risk(blocks: list[dict]) -> tuple[int, str] | None:
    """检查满行被切断、短续行被并入下一段的连续几何证据，按同栏重复次数分级。"""
    previous_lane = None
    first = None
    for before, after in zip(blocks, blocks[1:]):
        if before.get("type") != "text" or after.get("type") != "text" or before.get("angle") or after.get("angle"):
            continue
        previous_lines, next_lines = before.get("lines", ()), after.get("lines", ())
        if not previous_lines or len(next_lines) < 2:
            continue
        last, tail, start = (line.get("bbox") for line in (previous_lines[-1], next_lines[0], next_lines[1]))
        if not last or not tail or not start:
            continue
        # 两个缩进起点应对齐，其间有一条显著更短、向左顶格的行。
        width = max(last[2], start[2]) - tail[0]
        if width <= 0 or not 0.025 * width <= last[0] - tail[0] <= 0.18 * width:
            continue
        if abs(last[0] - start[0]) > 0.02 * width or abs(last[2] - start[2]) > 0.10 * width:
            continue
        if tail[2] > min(last[2], start[2]) - 0.18 * width:
            continue
        height = last[3] - last[1]
        step, next_step = tail[1] - last[1], start[1] - tail[1]
        if not 0.7 * height <= step <= 2.6 * height or step <= 0 or not 0.70 * step <= next_step <= 1.4 * step:
            continue
        content = before.get("content", "")
        text = (inline_span_plain_text(content) if isinstance(content, list) else content).rstrip()
        if not text or text.endswith((".", "。", "!", "！", "?", "？", ":", "：", ";", "；", "”", "»")):
            continue
        if previous_lane is not None and max(abs(last[0] - previous_lane[0]), abs(last[2] - previous_lane[1])) <= 0.04 * width:
            return 60, f"同栏至少两处满行中断，短续行并入下段，第二处位于页高 {tail[1]:.1%}"
        previous_lane = (last[0], last[2])
        first = first or f"满行中断且短续行并入下段，位于页高 {tail[1]:.1%}"
    return (30, first) if first else None
