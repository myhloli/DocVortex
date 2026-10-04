"""Third round of true original acceptance: Assert members, segment boundaries and types without relying on modified block numbers."""

from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

import pytest

from docvortex.analyzers.native import PdfModel
from docvortex.document.pdf import PDFDocument
from docvortex.postprocess.pages import model_json_to_pages
from docvortex.schema import ModelJson
from tools.review_flash_annotations import visible

ROOT = Path(__file__).parents[2]
MANIFEST = json.loads((ROOT / "tests/fixtures/flash_round3_annotations.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=2)
def _model(name):
    """Read the fingerprinted original in full, preserving cross-page fonts and reference context."""
    document = next(d for d in MANIFEST["documents"] if d["name"] == name)
    data = (ROOT / document["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == document["sha256"]
    with PDFDocument(data) as pdf:
        return PdfModel().predict(pdf)


def _starting(name, page, prefix):
    """Locate unique blocks by visible text prefix."""
    blocks = [b for b in _model(name)[page - 1] if visible(b["content"]).startswith(prefix)]
    assert len(blocks) == 1, (name, page, prefix, [(b["type"], visible(b["content"])) for b in blocks])
    return blocks[0]


@pytest.mark.parametrize(
    "name,page,prefix,kind",
    [
        ("dual_caption", 1, "Anorexia, involuntary", "text"),
        ("dual_caption", 10, "intake,", "text"),
        ("dual_caption", 15, "TNF-α", "text"),
        ("dual_caption", 15, "Hydrazine sulfate", "text"),
        ("dual_caption", 15, "Anabolic steroids", "text"),
        ("dual_caption", 16, "antidepressant drugs", "text"),
        ("dual_caption", 16, "Assessment of the patient", "text"),
        ("dual_caption", 5, "Hypermetabolism", "paragraph_title"),
        ("dual_caption", 8, "Altered Protein Metabolism", "paragraph_title"),
        ("dual_caption", 11, "Cyproheptadine and Other Antiserotonergic Drugs", "paragraph_title"),
        ("dual_caption", 10, "Glucocorticoids", "paragraph_title"),
        ("dual_caption", 12, "Branched-chain Amino Acids", "paragraph_title"),
        ("dual_caption", 12, "Prokinetic Agents", "paragraph_title"),
        ("dual_caption", 12, "Eicosapentanoic Acid", "paragraph_title"),
        ("dual_caption", 15, "Others", "paragraph_title"),
        ("dual_caption", 15, "Nutritional, Psychological,", "paragraph_title"),
        ("fix", 1, "Demonstrating a Long-Coherence", "doc_title"),
        ("fix", 1, "1 AWS Center", "text"),
        ("fix", 10, "APPENDIX E:", "paragraph_title"),
        ("fix", 11, "APPENDIX F:", "paragraph_title"),
        ("fix", 11, "We show in Fig. 9", "text"),
        ("fix", 12, "APPENDIX G:", "paragraph_title"),
        ("fix", 12, "APPENDIX H:", "paragraph_title"),
        ("fix", 12, "APPENDIX I:", "paragraph_title"),
        ("fix", 16, "b. Thermal Johnson", "paragraph_title"),
        ("fix", 16, "Our flux lines", "text"),
    ],
)
def test_titles_and_explicit_paragraph_boundaries(name, page, prefix, kind):
    """The real title is classified separately from the organization and main text, and new paragraphs are indented to retain independent starting points."""
    exact = [block for block in _model(name)[page - 1] if visible(block["content"]) == prefix]
    block = exact[0] if len(exact) == 1 else _starting(name, page, prefix)
    assert block["type"] == kind


def test_indented_paragraph_first_word_has_one_owner_and_matching_bounds():
    """The words separated by wide spaces at the beginning of the paragraph return to this paragraph completely, and the text and line boxes no longer remain in the previous paragraph."""
    previous = _starting("dual_caption", 10, "Prednisolone,")
    current = _starting("dual_caption", 10, "Prescribing an intermediate-acting")
    assert visible(previous["content"]).endswith("day.")
    assert sum(visible(block["content"]).count("Prescribing") for block in _model("dual_caption")[9]) == 1
    assert previous["bbox"][3] < current["bbox"][1]
    assert max(line["bbox"][3] for line in previous["lines"]) < min(line["bbox"][1] for line in current["lines"])
    pages = model_json_to_pages(
        ModelJson(
            pages=_model("dual_caption"),
            page_index_map=[],
            metadata={"file_suffix": "pdf", "producer": {"name": "docvortex", "version": "test"}},
        )
    )
    public = [block for block in pages[9].blocks if getattr(block, "type", None) == "text"]
    result = next(block for block in public if visible(block.model_dump(mode="json")["content"]).startswith("Prescribing"))
    assert not result.continues_prev


def test_small_multiline_heading_is_one_complete_title_band():
    """Confirmed two-line subtitles remain a block and cannot be split again when semantic lines are joined."""
    block = _starting("dual_caption", 15, "Nutritional, Psychological,")
    assert block["type"] == "paragraph_title"
    assert visible(block["content"]).endswith("Behavioral Therapies")
    assert not any(visible(item["content"]) == "Therapies" for item in _model("dual_caption")[14])


@pytest.mark.parametrize(
    "name,page,prefix,tail",
    [
        ("dual_caption", 1, "Anorexia, involuntary", "development of cachexia."),
        ("dual_caption", 2, "were approximately", "potassium concentration."),
        ("dual_caption", 17, "If there is associated", "support."),
        ("fix", 10, "The dual-rail qubit calibration", "Spectroscopically"),
        ("fix", 12, "The ancilla qubit’s frequency", "kHz."),
        ("fix", 13, "The second leakage mechanism", "by j11i"),
        ("fix", 18, "Rewriting in terms", " d "),
    ],
)
def test_continuous_body_members_are_not_split(name, page, prefix, tail):
    """Peer fragments, mathematical initial continuation lines, and short tails still belong to the original text paragraphs."""
    block = _starting(name, page, prefix)
    assert block["type"] == "text"
    assert tail in visible(block["content"])


def test_mixed_columns_do_not_turn_body_into_reference_rows():
    """The two-column text and three-column reference area on the same page each have their own range and line order."""
    prefixes = ["trials,", "We need to define", "The outcomes of drug studies", "Effective communication"]
    blocks = [_starting("dual_caption", 17, prefix) for prefix in prefixes]
    assert all(block["type"] == "text" for block in blocks)
    assert len({id(block) for block in blocks}) == 4
    page = _model("dual_caption")[16]
    assert page.index(_starting("dual_caption", 17, "Furthermore,")) < page.index(blocks[0])
    assert not any(block["type"] == "page_footnote" for block in _model("dual_caption")[16])


@pytest.mark.parametrize(
    "name,first,last,pattern,count",
    [
        ("dual_caption", 17, 20, r"(?<![\w])([0-9]{1,3})[.]\s*(?=[A-Z])", 154),
        ("fix", 19, 21, r"\[([0-9]{1,3})\]", 58),
    ],
)
def test_each_numbered_reference_has_one_entry(name, first, last, pattern, count):
    """The new number cannot be swallowed by the previous article, and the font change in the article cannot produce a false title."""
    numbers = []
    for page in _model(name)[first - 1 : last]:
        for block in page:
            text = visible(block["content"])
            found = [int(value) for value in re.findall(pattern, text)]
            if not found or not re.match(pattern, text):
                continue
            assert block["type"] == "text", text
            numbers.append(found[0])
    assert sorted(numbers) == list(range(1, count + 1))
    for page in _model(name)[first - 1 : last]:
        for block in page:
            if block["type"] == "paragraph_title":
                assert visible(block["content"]).strip().casefold() == "references"
    if name == "fix":
        assert [sum(block["type"] == "text" for block in page) for page in _model(name)[19:21]] == [30, 8]


@pytest.mark.parametrize(
    "page,region",
    [
        (9, (0.515, 0.258, 0.913, 0.318)),
        (9, (0.630, 0.788, 0.913, 0.816)),
        (13, (0.161, 0.612, 0.484, 0.647)),
        (13, (0.215, 0.692, 0.484, 0.721)),
        (14, (0.515, 0.873, 0.913, 0.904)),
        (15, (0.143, 0.872, 0.484, 0.904)),
        (16, (0.634, 0.146, 0.913, 0.172)),
        (17, (0.195, 0.456, 0.484, 0.487)),
        (18, (0.085, 0.336, 0.484, 0.388)),
    ],
)
def test_complete_display_equation_owns_number_and_fragments(page, region):
    """The mathematical region determined from the source page glyph is only claimed by a complete formula, and the text cannot cover the formula."""
    blocks = _model("fix")[page - 1]
    matches = [
        b
        for b in blocks
        if b["type"] == "equation"
        and all(b["bbox"][i] <= region[i] + 0.004 if i < 2 else b["bbox"][i] >= region[i] - 0.004 for i in range(4))
    ]
    assert len(matches) == 1, (page, region, [(b["type"], b["bbox"]) for b in blocks])
    for block in blocks:
        if block["type"] not in {"text", "paragraph_title"}:
            continue
        b = block["bbox"]
        overlap = max(0, min(b[2], region[2]) - max(b[0], region[0])) * max(0, min(b[3], region[3]) - max(b[1], region[1]))
        assert overlap < 0.0001, (page, visible(block["content"]), b)


def test_table_note_is_external_and_unique():
    """The explanatory text within the outer horizontal line is still a table note and does not enter the HTML cell or the projected table body."""
    page = _model("dual_caption")[4]
    table = next(b for b in page if b["type"] == "table")
    note = _starting("dual_caption", 5, "*There are several reports")
    assert note["type"] == "footnote"
    assert "There are several reports" not in visible(table["content"])
    assert table["bbox"][3] < note["bbox"][1]
    assert "Increased size" in visible(note["content"])


@pytest.mark.parametrize(
    "name,page,prefix,tail",
    [
        ("dual_caption", 4, "A simplified model", "CNTF"),
        ("dual_caption", 6, "The potential modalities", "site"),
        ("fix", 17, "FIG. 16.", "fit uncertainty"),
    ],
)
def test_full_caption_band_keeps_first_line_and_followers(name, page, prefix, tail):
    """The numbers on the figures and the descriptions below the figures are complete. The first line of bold text and formula subtitles will not cut off the figure notes."""
    block = _starting(name, page, prefix)
    assert block["type"] == "caption"
    assert tail in visible(block["content"])


@pytest.mark.parametrize(
    "prefix,markers",
    [
        ("Glucocorticoids", "F A"),
        ("Thalidomide", "A"),
        ("Branched-chain amino acids", "E F"),
        ("Eicosapentanoic acid", "D E A"),
        ("Hydrazine sulfate", "C"),
    ],
)
def test_legend_names_and_symbols_keep_same_row(prefix, markers):
    """The drug name and its corresponding letters are output as a figure comment line."""
    block = _starting("dual_caption", 6, prefix)
    assert block["type"] == "footnote"
    assert visible(block["content"]).endswith(markers)


def test_public_pages_keep_reference_boundaries_and_legend_ownership():
    """Public conversion consumes temporary evidence and retains segment boundaries, cross-column continuations, and complete figure annotation attribution."""
    pages = model_json_to_pages(
        ModelJson(
            pages=_model("dual_caption"),
            page_index_map=[],
            metadata={"file_suffix": "pdf", "producer": {"name": "docvortex", "version": "test"}},
        )
    )
    values = [page.model_dump(mode="json") for page in pages]
    encoded = json.dumps(values, ensure_ascii=False)
    assert "_reference_start" not in encoded and "_paragraph_boundary" not in encoded
    image = next(block for block in values[5]["blocks"] if block["type"] == "image")
    notes = [block for block in image["content"] if block["type"] == "image_footnote"]
    assert any(visible(block["content"]).startswith("Glucocorticoids") for block in notes)
    assert any(visible(block["content"]).endswith("D E A") for block in notes)
    labels = [visible(block["content"]) for block in notes]
    assert labels.index("Second-line treatments") < labels.index("Cannabinoids F")
    assert labels.index("Melatonin A") < labels.index("Thalidomide A")
    assert labels.index("Others") < labels.index("Anabolic steroids E")
    for page in values[16:]:
        for block in page["blocks"]:
            if re.match(r"^\d{1,3}[.]", visible(block.get("content"))):
                assert not block.get("continues_prev"), visible(block["content"])
    continuations = [block for page in values[17:] for block in page["blocks"] if block.get("continues_prev")]
    assert continuations
