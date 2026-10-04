"""Use native characters and local line bands to restore the first word and hanging numbered segments without inferring semantic segment boundaries."""

import re
import statistics

from .geometry import _bbox_union_many
from .line_layout import _line_effective_height


def restore_drop_cap_paragraphs(lines):
    """The large initial letter, the immediate lowercase ending, and the wrapping of the continuation line together prove that the dropped initial letter belongs to the complete text paragraph."""
    group = max((line.paragraph_group for line in lines if line.paragraph_group is not None), default=-1) + 1
    for first in lines:
        chars = first.chars
        if first.angle or len(chars) < 4 or not re.fullmatch(r"[A-Z]", chars[0].get("char", "")):
            continue
        index = 1
        while index < len(chars) and chars[index].get("char", "").isspace():
            box = chars[index]["bbox"]
            if box[2] > box[0] or box[3] > box[1]:
                break
            index += 1
        suffix = []
        for char in chars[index:]:
            if not re.fullmatch(r"[a-z]", char.get("char", "")):
                break
            suffix.append(char)
        if not 2 <= len(suffix) <= 11:
            continue
        cap = chars[0]["bbox"]
        body = _bbox_union_many([char["bbox"] for char in suffix])
        em = statistics.median(char["bbox"][3] - char["bbox"][1] for char in suffix)
        if (
            em <= 0
            or cap[3] - cap[1] < 3 * em
            or not -0.1 * em <= body[0] - cap[2] <= 1.5 * em
            or not cap[1] + 0.2 * (cap[3] - cap[1]) <= body[3] <= cap[1] + 0.75 * (cap[3] - cap[1])
        ):
            continue
        # Only the partial text column is connected; the reset of font size, line spacing, left margin or new numbering is terminated immediately.
        wrapped_edge = max(
            (
                line.bbox[2]
                for line in lines
                if line is not first and abs(line.bbox[0] - body[0]) <= 0.5 * em and body[1] < line.bbox[1] < cap[3]
            ),
            default=body[2],
        )
        peers = sorted(
            (
                line
                for line in lines
                if line is not first
                and line.angle == 0
                and line.bbox[0] >= cap[0] - 0.4 * em
                and line.bbox[2] <= wrapped_edge + 0.5 * em
                and 0.85 * em <= _line_effective_height(line, line.bbox) <= 1.15 * em
                and line.bbox[1] >= body[1] - 0.2 * em
            ),
            key=lambda line: (line.bbox[1], line.bbox[0]),
        )
        run = [first]
        bottom = body[3]
        for line in peers:
            if line.semantic_type is not None or re.match(r"^\s*(?:\d+[.)]|[a-z][.)])\s", line.text):
                break
            if line.bbox[1] - bottom > 0.85 * em:
                break
            if line.bbox[1] < cap[3] - em and line.bbox[0] < body[0] - 0.5 * em:
                continue
            run.append(line)
            bottom = max(bottom, line.bbox[3])
        wrapped = [line for line in run[1:] if abs(line.bbox[0] - body[0]) <= 0.5 * em and line.bbox[1] > body[1] + 0.5 * em]
        returned = [line for line in run[1:] if line.bbox[1] >= cap[3] - em and abs(line.bbox[0] - cap[0]) <= 0.5 * em]
        if len(wrapped) < 2 or len(returned) < 2:
            continue
        if index > 1:
            prefix = chars[0]["char"] + "".join(char["char"] for char in chars[1:index])
            if first.text.startswith(prefix):
                first.text = chars[0]["char"] + first.text[len(prefix) :]
        for line in run:
            line.semantic_type = "text"
            line.paragraph_group = group
            line.title_suppressed = True
        group += 1


def group_hanging_letter_paragraphs(lines):
    """Independent letter numbers and the left margin of the continuous text prove that the same item continues on the line, and only the inline style is retained in bold."""
    group = max((line.paragraph_group for line in lines if line.paragraph_group is not None), default=-1) + 1
    for marker in lines:
        if marker.angle or marker.semantic_type is not None or not re.fullmatch(r"[a-z][.)]", marker.text.strip()):
            continue
        em = _line_effective_height(marker, marker.bbox)
        hosts = [
            line
            for line in lines
            if line is not marker
            and line.angle == 0
            and line.semantic_type is None
            and line.paragraph_group is None
            and abs(line.bbox[3] - marker.bbox[3]) <= 0.25 * em
            and 0.5 * em <= line.bbox[0] - marker.bbox[2] <= 3 * em
            and len(line.text.split()) >= 5
        ]
        if len(hosts) != 1:
            continue
        first = hosts[0]
        run = [first]
        candidates = sorted(
            (
                line
                for line in lines
                if line.angle == 0 and line.bbox[1] > first.bbox[1] + 0.5 * em and abs(line.bbox[0] - first.bbox[0]) <= 0.2 * em
            ),
            key=lambda line: line.bbox[1],
        )
        for line in candidates:
            if (
                line.semantic_type is not None
                or line.paragraph_group is not None
                or not 0.85 * em <= _line_effective_height(line, line.bbox) <= 1.15 * em
                or line.font_signature != first.font_signature
                or not -0.15 * em <= line.bbox[1] - run[-1].bbox[3] <= 0.55 * em
                or re.match(r"^\s*(?:\d+|[a-z])[.)]\s", line.text)
            ):
                break
            run.append(line)
        if len(run) < 3:
            continue
        # The last line of the same font size and different font immediately following the main text may contain italic emphasis; the original paragraph rules are restored and cannot be forcibly truncated.
        if any(
            all(line is not member for member in run)
            and 0.85 * em <= _line_effective_height(line, line.bbox) <= 1.15 * em
            and line.font_signature != first.font_signature
            and -0.15 * em <= line.bbox[1] - run[-1].bbox[3] <= 0.55 * em
            for line in candidates
        ):
            continue
        # Inline formulas, references, or fragments that have not yet been merged are still the responsibility of the original member rules and cannot be forced into segments beyond them.
        if any(
            line is not marker
            and all(line is not member for member in run)
            and first.bbox[1] <= line.bbox[1] <= run[-1].bbox[3]
            and first.bbox[0] + 0.2 * em < line.bbox[0] < max(member.bbox[2] for member in run)
            for line in lines
        ):
            continue
        for line in [marker, *run]:
            line.semantic_type = "text"
            line.title_suppressed = True
            line.paragraph_group = group
        group += 1
