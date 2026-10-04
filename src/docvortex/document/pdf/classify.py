from __future__ import annotations

import re
from collections import Counter
from ctypes import byref, c_float, c_int, create_string_buffer
from io import BytesIO
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from loguru import logger
from pypdf import PdfReader
from pypdf.generic import ContentStream

from .pdfium import PdfiumFontError, close_pdfium_child, pdfium_guard
from .classification_bridge import NativeClassificationError

MAX_SAMPLE_PAGES = 10
CHARS_THRESHOLD = 50
HIGH_IMAGE_COVERAGE_THRESHOLD = 0.8
TEXT_QUALITY_MIN_CHARS = 300
TEXT_QUALITY_BAD_THRESHOLD = 0.03
UNICODE_MAP_ERROR_RATIO_THRESHOLD = 0.04
CID_FONT_USAGE_RATIO_THRESHOLD = 0.01
CID_FONT_USAGE_COUNT_THRESHOLD = 30
LATIN_CJK_FONT_USAGE_RATIO_THRESHOLD = 0.01
LATIN_CJK_FONT_USAGE_COUNT_THRESHOLD = 30
LATIN_CJK_FONT_CJK_RATIO_THRESHOLD = 0.8
LATIN_CHARSET_MIN_LATIN_GLYPHS = 10
LATIN_CHARSET_MIN_LATIN_RATIO = 0.5
MAX_PAGE_ASPECT_RATIO = 10.0
SUSPICIOUS_CJK_72XX_START = 0x7280
SUSPICIOUS_CJK_72XX_END = 0x72DF
SUSPICIOUS_CJK_72XX_COUNT_THRESHOLD = 30
SUSPICIOUS_CJK_72XX_CJK_RATIO_THRESHOLD = 0.026
SUSPICIOUS_CJK_72XX_WHITELIST = set("犀犁犄犊犒犟犬犯状犷犹狂狄狈狐狗狙狞")
ASCII_PUNCT_CHARS = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
ASCII_PUNCT_RUN_MIN_LENGTH = 4
SUSPICIOUS_ASCII_PUNCT_MIN_TEXT_CHARS = 100
SUSPICIOUS_ASCII_PUNCT_RATIO_THRESHOLD = 0.25
SUSPICIOUS_ASCII_PUNCT_RUN_RATIO_THRESHOLD = 0.10
SUSPICIOUS_CROSS_SCRIPT_MIN_TEXT_CHARS = 300
SUSPICIOUS_CROSS_SCRIPT_MIN_CJK_CHARS = 100
SUSPICIOUS_CROSS_SCRIPT_MIN_OTHER_SCRIPT_CHARS = 120
SUSPICIOUS_CROSS_SCRIPT_OTHER_SCRIPT_RATIO = 0.18
SUSPICIOUS_CROSS_SCRIPT_MIN_DENSE_SCRIPTS = 3
SUSPICIOUS_CROSS_SCRIPT_DENSE_SCRIPT_CHARS = 5
SUSPICIOUS_CROSS_SCRIPT_RANGES = (
    (0x0370, 0x03FF, "Greek"),
    (0x0400, 0x052F, "Cyrillic"),
    (0x0600, 0x06FF, "Arabic"),
    (0x0700, 0x074F, "Syriac"),
    (0x0750, 0x077F, "Arabic Supplement"),
    (0x0780, 0x07BF, "Thaana"),
    (0x07C0, 0x07FF, "NKo"),
    (0x0800, 0x083F, "Samaritan"),
    (0x0840, 0x085F, "Mandaic"),
    (0x0860, 0x086F, "Syriac Supplement"),
    (0x0870, 0x089F, "Arabic Extended-B"),
    (0x0900, 0x097F, "Devanagari"),
    (0x0C80, 0x0CFF, "Kannada"),
    (0x0E00, 0x0E7F, "Thai"),
    (0x1000, 0x109F, "Myanmar"),
    (0x1100, 0x11FF, "Hangul Jamo"),
    (0x1200, 0x137F, "Ethiopic"),
    (0x13A0, 0x13FF, "Cherokee"),
    (0x1400, 0x167F, "Canadian Syllabics"),
    (0x1800, 0x18AF, "Mongolian"),
    (0x1A20, 0x1AAF, "Tai Tham"),
    (0x2C00, 0x2C5F, "Glagolitic"),
    (0xA000, 0xA48F, "Yi"),
)
CJK_TEXT_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2EBEF),
)

_ALLOWED_CONTROL_CODES = {9, 10, 13}
_PRIVATE_USE_AREA_START = 0xE000
_PRIVATE_USE_AREA_END = 0xF8FF


def _is_disallowed_control_unicode(unicode_code: int) -> bool:
    return (0 <= unicode_code < 32 or 127 <= unicode_code <= 159) and unicode_code not in _ALLOWED_CONTROL_CODES


def classify(pdf_doc: pdfium.PdfDocument, pdf_bytes: bytes) -> str:
    """
    Fast PDF classification path.

    The path uses pdfium + pypdf to detect text PDFs and garbled PDFs.

    Returns:
        "txt" if the PDF can be parsed as text, otherwise "ocr".
    """

    try:
        with pdfium_guard():
            page_count = len(pdf_doc)
            if page_count == 0:
                return "ocr"

            page_indices = get_sample_page_indices(page_count, MAX_SAMPLE_PAGES)
            if not page_indices:
                return "ocr"

            extreme_page_index, extreme_ratio = get_extreme_aspect_ratio_page_pdfium(
                pdf_doc,
                page_indices,
            )
            if extreme_page_index is not None:
                logger.debug(
                    "Classify PDF as OCR due to extreme sampled-page aspect ratio: "
                    f"page={extreme_page_index + 1}, ratio={extreme_ratio:.2f}"
                )
                return "ocr"

            text_samples = _collect_pdfium_text_samples(pdf_doc, page_indices)
            avg_cleaned_chars_per_page = _get_avg_cleaned_chars_per_page_from_samples(text_samples)
            if avg_cleaned_chars_per_page < CHARS_THRESHOLD:
                return "ocr"

            unicode_map_error_signal = _get_unicode_map_error_signal_from_samples(text_samples)
            if unicode_map_error_signal["unicode_map_error_ratio"] >= UNICODE_MAP_ERROR_RATIO_THRESHOLD:
                logger.debug(
                    "Classify PDF as OCR due to PDFium Unicode map errors: "
                    f"errors={unicode_map_error_signal['unicode_map_error_count']}, "
                    f"total={unicode_map_error_signal['total_chars']}, "
                    f"ratio={unicode_map_error_signal['unicode_map_error_ratio']:.4f}"
                )
                return "ocr"

            font_resource_signals = _get_font_resource_signals_pypdf(
                pdf_bytes,
                page_indices,
            )
            cid_font_usage_signal = _get_cid_font_usage_signal_from_samples(
                text_samples,
                font_resource_signals["cid_without_to_unicode_usage"],
            )
            if cid_font_usage_signal["triggered"]:
                logger.debug(
                    "Classify PDF as OCR due to high CID font usage without ToUnicode: "
                    f"page={cid_font_usage_signal['page_index'] + 1}, "
                    f"fonts={cid_font_usage_signal['font_names']}, "
                    f"chars={cid_font_usage_signal['cid_font_char_count']}, "
                    f"total={cid_font_usage_signal['total_chars']}, "
                    f"ratio={cid_font_usage_signal['cid_font_usage_ratio']:.4f}"
                )
                return "ocr"

            latin_cjk_font_usage_signal = _get_latin_font_cjk_usage_signal_from_samples(
                text_samples,
                font_resource_signals["latin_charset_with_to_unicode"],
                count_threshold=LATIN_CJK_FONT_USAGE_COUNT_THRESHOLD,
                usage_ratio_threshold=LATIN_CJK_FONT_USAGE_RATIO_THRESHOLD,
                cjk_ratio_threshold=LATIN_CJK_FONT_CJK_RATIO_THRESHOLD,
            )
            if latin_cjk_font_usage_signal["triggered"]:
                logger.debug(
                    "Classify PDF as OCR due to Latin CharSet font decoding as CJK: "
                    f"page={latin_cjk_font_usage_signal['page_index'] + 1}, "
                    f"fonts={latin_cjk_font_usage_signal['font_names']}, "
                    f"chars={latin_cjk_font_usage_signal['font_char_count']}, "
                    f"cjk={latin_cjk_font_usage_signal['cjk_char_count']}, "
                    f"total={latin_cjk_font_usage_signal['total_chars']}, "
                    f"usage_ratio={latin_cjk_font_usage_signal['font_usage_ratio']:.4f}, "
                    f"cjk_ratio={latin_cjk_font_usage_signal['font_cjk_ratio']:.4f}"
                )
                return "ocr"

            text_quality_signal = _get_text_quality_signal_from_samples(text_samples)
            total_chars = text_quality_signal["total_chars"]
            abnormal_ratio = text_quality_signal["abnormal_ratio"]

            if total_chars >= TEXT_QUALITY_MIN_CHARS and abnormal_ratio >= TEXT_QUALITY_BAD_THRESHOLD:
                return "ocr"

            u72xx_signal = _get_u72xx_text_signal_from_samples(text_samples)
            if (
                u72xx_signal["u72xx_count"] >= SUSPICIOUS_CJK_72XX_COUNT_THRESHOLD
                and u72xx_signal["u72xx_cjk_ratio"] >= SUSPICIOUS_CJK_72XX_CJK_RATIO_THRESHOLD
            ):
                logger.debug(
                    "Classify PDF as OCR due to suspicious U+7280-U+72DF text: "
                    f"count={u72xx_signal['u72xx_count']}, "
                    f"cjk_ratio={u72xx_signal['u72xx_cjk_ratio']:.4f}"
                )
                return "ocr"

            cross_script_signal = _get_cross_script_text_signal_from_samples(text_samples)
            if cross_script_signal["triggered"]:
                logger.debug(
                    "Classify PDF as OCR due to suspicious cross-script text: "
                    f"chars={cross_script_signal['total_chars']}, "
                    f"cjk={cross_script_signal['cjk_chars']}, "
                    f"suspicious={cross_script_signal['suspicious_chars']}, "
                    f"ratio={cross_script_signal['suspicious_ratio']:.4f}, "
                    f"scripts={cross_script_signal['top_scripts']}"
                )
                return "ocr"

            ascii_punct_signal = _get_sampled_ascii_punct_signal_from_samples(text_samples)
            if ascii_punct_signal["triggered"]:
                logger.debug(
                    "Classify PDF as OCR due to suspicious sampled-page ASCII punctuation "
                    f"text: page={ascii_punct_signal['page_index'] + 1}, "
                    f"text_chars={ascii_punct_signal['cleaned_text_chars']}, "
                    f"ascii_punct_ratio="
                    f"{ascii_punct_signal['ascii_punct_ratio']:.4f}, "
                    f"punct_run_ratio={ascii_punct_signal['punct_run_ratio']:.4f}"
                )
                return "ocr"

            if _get_high_image_coverage_ratio_from_samples(text_samples) >= HIGH_IMAGE_COVERAGE_THRESHOLD:
                return "ocr"

    except (PdfiumFontError, NativeClassificationError):
        raise
    except Exception as e:
        logger.error(f"Failed to classify PDF: {e}")
        return "ocr"

    return "txt"


def get_sample_page_indices(page_count: int, max_pages: int = MAX_SAMPLE_PAGES) -> list[int]:
    if page_count <= 0 or max_pages <= 0:
        return []

    sample_count = min(page_count, max_pages)
    if sample_count == page_count:
        return list(range(page_count))
    if sample_count == 1:
        return [0]

    indices = []
    seen = set()
    for i in range(sample_count):
        page_index = round(i * (page_count - 1) / (sample_count - 1))
        page_index = max(0, min(page_count - 1, page_index))
        if page_index not in seen:
            indices.append(page_index)
            seen.add(page_index)

    if len(indices) < sample_count:
        for page_index in range(page_count):
            if page_index in seen:
                continue
            indices.append(page_index)
            seen.add(page_index)
            if len(indices) == sample_count:
                break

    return sorted(indices)


def get_extreme_aspect_ratio_page_pdfium(
    pdf_doc: pdfium.PdfDocument,
    page_indices: list[int],
    max_page_aspect_ratio: float = MAX_PAGE_ASPECT_RATIO,
) -> tuple[Any, Any]:
    with pdfium_guard():
        for page_index in page_indices:
            # FPDF_GetPageSizeByIndexF directly retrieves the MediaBox size by index without loading the page.
            # Content stream not parsed; vector-dense pages FPDF_LoadPage for several seconds at a time.
            page_width, page_height = pdf_doc.get_page_size(page_index)
            if page_width <= 0 or page_height <= 0:
                continue

            aspect_ratio = max(page_width / page_height, page_height / page_width)
            if aspect_ratio > max_page_aspect_ratio:
                return page_index, aspect_ratio

    return None, None


def _collect_pdfium_text_sample_from_page(page_index: int, page: Any) -> dict[str, Any]:
    """Extracts plain Python text statistics from a single page PDFium object and releases the child object on the caller."""
    text_page = None
    try:
        text_page = page.get_textpage()
        text = text_page.get_text_bounded()
        char_count = text_page.count_chars()
        # Classified consumption of original characters before deduplication, without canonical character count or font name decoding to replace old statistics.
        from .classification_bridge import read_classification_snapshot

        functions = (_is_disallowed_control_unicode, _is_cjk_unicode_code, _get_pdfium_char_font_name, _normalize_pdf_font_name)
        if all(value is original for value, original in zip(functions, _STANDARD_CLASSIFICATION_FUNCTIONS)):
            try:
                snapshot = read_classification_snapshot(
                    text_page,
                    char_count,
                    CJK_TEXT_RANGES,
                    _ALLOWED_CONTROL_CODES,
                    (_PRIVATE_USE_AREA_START, _PRIVATE_USE_AREA_END),
                    _normalize_pdf_font_name,
                )
                fields = snapshot.to_dict() if snapshot is not None else None
            except PdfiumFontError:
                raise
            except Exception as exc:
                raise NativeClassificationError("Native PDF classification statistics failed") from exc
            if fields is not None:
                return {
                    "page_index": page_index,
                    "text": text,
                    "cleaned_text": re.sub(r"\s+", "", text),
                    "char_count": char_count,
                    **fields,
                }
        null_char_count = 0
        replacement_char_count = 0
        control_char_count = 0
        private_use_char_count = 0
        unicode_map_error_count = 0
        font_name_counts = {}
        non_generated_char_count = 0
        font_non_generated_char_counts = {}
        font_non_generated_cjk_char_counts = {}

        for char_index in range(char_count):
            unicode_code = pdfium_c.FPDFText_GetUnicode(text_page, char_index)
            is_generated = pdfium_c.FPDFText_IsGenerated(text_page, char_index) == 1
            if not is_generated:
                non_generated_char_count += 1

            if unicode_code == 0:
                null_char_count += 1
            elif unicode_code == 0xFFFD:
                replacement_char_count += 1
            elif _is_disallowed_control_unicode(unicode_code):
                control_char_count += 1
            elif _PRIVATE_USE_AREA_START <= unicode_code <= _PRIVATE_USE_AREA_END:
                private_use_char_count += 1

            if pdfium_c.FPDFText_HasUnicodeMapError(text_page, char_index):
                unicode_map_error_count += 1

            font_name = _normalize_pdf_font_name(_get_pdfium_char_font_name(text_page, char_index))
            if font_name:
                font_name_counts[font_name] = font_name_counts.get(font_name, 0) + 1
                if not is_generated:
                    font_non_generated_char_counts[font_name] = font_non_generated_char_counts.get(font_name, 0) + 1
                    if _is_cjk_unicode_code(unicode_code):
                        font_non_generated_cjk_char_counts[font_name] = font_non_generated_cjk_char_counts.get(font_name, 0) + 1

        return {
            "page_index": page_index,
            "text": text,
            "cleaned_text": re.sub(r"\s+", "", text),
            "char_count": char_count,
            "null_char_count": null_char_count,
            "replacement_char_count": replacement_char_count,
            "control_char_count": control_char_count,
            "private_use_char_count": private_use_char_count,
            "unicode_map_error_count": unicode_map_error_count,
            "font_name_counts": font_name_counts,
            "non_generated_char_count": non_generated_char_count,
            "font_non_generated_char_counts": font_non_generated_char_counts,
            "font_non_generated_cjk_char_counts": font_non_generated_cjk_char_counts,
        }
    finally:
        close_pdfium_child(text_page)


def _collect_pdfium_text_samples(pdf_doc: pdfium.PdfDocument, page_indices: list[int]) -> list[dict[str, Any]]:
    """Collect sampling page text statistics at one time, return pure Python data, and avoid caching PDFium sub-objects."""
    text_samples = []

    with pdfium_guard():
        for page_index in page_indices:
            page = None
            try:
                page = pdf_doc[page_index]
                sample = _collect_pdfium_text_sample_from_page(page_index, page)
                # The same page load also counts image coverage, and the coverage check at the end no longer reloads the page.
                sample["image_coverage_ratio"] = _page_image_coverage_ratio(page)
                text_samples.append(sample)
            finally:
                close_pdfium_child(page)

    return text_samples


def _get_high_image_coverage_ratio_from_samples(text_samples: list[dict[str, Any]]) -> float:
    """The proportion of high-coverage pages is calculated based on the image coverage of cached sample pages."""

    if not text_samples:
        return 0.0
    high_image_coverage_pages = sum(
        sample.get("image_coverage_ratio", 0.0) >= HIGH_IMAGE_COVERAGE_THRESHOLD for sample in text_samples
    )
    return high_image_coverage_pages / len(text_samples)


def _get_avg_cleaned_chars_per_page_from_samples(text_samples: list[dict[str, Any]]) -> float:
    """Calculates the average number of valid characters based on cached sample page text."""
    cleaned_total_chars = 0

    for text_sample in text_samples:
        cleaned_total_chars += len(text_sample["cleaned_text"])

    if not text_samples:
        return 0.0
    return cleaned_total_chars / len(text_samples)


def _get_text_quality_signal_from_samples(text_samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Abnormal character quality signals are counted based on cached sample page character counts."""
    total_chars = 0
    null_char_count = 0
    replacement_char_count = 0
    control_char_count = 0
    private_use_char_count = 0

    for text_sample in text_samples:
        total_chars += text_sample["char_count"]
        null_char_count += text_sample["null_char_count"]
        replacement_char_count += text_sample["replacement_char_count"]
        control_char_count += text_sample["control_char_count"]
        private_use_char_count += text_sample["private_use_char_count"]

    abnormal_chars = null_char_count + replacement_char_count + control_char_count + private_use_char_count

    abnormal_ratio = 0.0
    if total_chars > 0:
        abnormal_ratio = abnormal_chars / total_chars

    return {
        "total_chars": total_chars,
        "abnormal_ratio": abnormal_ratio,
        "null_char_count": null_char_count,
        "replacement_char_count": replacement_char_count,
        "control_char_count": control_char_count,
        "private_use_char_count": private_use_char_count,
    }


def _get_unicode_map_error_signal_from_samples(text_samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Statistics PDFium Character-level Unicode mapping failure ratio, used to identify garbled text that cannot be reliably extracted."""
    total_chars = 0
    unicode_map_error_count = 0

    for text_sample in text_samples:
        total_chars += text_sample["char_count"]
        unicode_map_error_count += text_sample["unicode_map_error_count"]

    unicode_map_error_ratio = 0.0
    if total_chars > 0:
        unicode_map_error_ratio = unicode_map_error_count / total_chars

    return {
        "total_chars": total_chars,
        "unicode_map_error_count": unicode_map_error_count,
        "unicode_map_error_ratio": unicode_map_error_ratio,
    }


def _is_cjk_unicode_code(unicode_code: int) -> bool:
    """Determine whether the Unicode code point belongs to the CJK text range recognized by the classifier."""
    return any(start <= unicode_code <= end for start, end in CJK_TEXT_RANGES)


def _get_cjk_glyph_name_code(glyph_name: str) -> int | None:
    """Parses CJK in the form uniXXXX/uXXXXX glyph name, other names return None."""
    match = re.fullmatch(
        r"(?:uni([0-9A-Fa-f]{4,6})|u([0-9A-Fa-f]{4,6}))",
        glyph_name,
    )
    if match is None:
        return None
    unicode_code = int(match.group(1) or match.group(2), 16)
    if not _is_cjk_unicode_code(unicode_code):
        return None
    return unicode_code


def _get_empty_latin_charset_with_to_unicode_signal() -> dict[str, Any]:
    """Constructs the untriggered Type1 Latin CharSet font candidate signal."""
    return {
        "triggered": False,
        "charset_glyph_count": 0,
        "latin_glyph_count": 0,
        "latin_glyph_ratio": 0.0,
        "cjk_charset_glyph_count": 0,
        "cjk_charset_glyph_ratio": 0.0,
    }


def _get_latin_charset_with_to_unicode_signal(font: Any) -> dict[str, Any]:
    """Identifies the Type1 font candidate with ToUnicode and CharSet is an obvious candidate for Latin."""
    signal = _get_empty_latin_charset_with_to_unicode_signal()
    if str(font.get("/Subtype")) != "/Type1":
        return signal

    descriptor = _resolve_pdf_object(font.get("/FontDescriptor"))
    to_unicode = _resolve_pdf_object(font.get("/ToUnicode"))
    if descriptor is None or to_unicode is None:
        return signal

    charset = descriptor.get("/CharSet")
    if charset is None:
        return signal

    glyph_names = set(re.findall(r"/([^/\s]+)", str(charset)))
    charset_glyph_count = len(glyph_names)
    latin_glyph_count = sum(1 for glyph_name in glyph_names if re.fullmatch(r"[A-Za-z]", glyph_name))
    cjk_charset_glyph_count = sum(1 for glyph_name in glyph_names if _get_cjk_glyph_name_code(glyph_name) is not None)
    latin_glyph_ratio = latin_glyph_count / charset_glyph_count if charset_glyph_count else 0.0
    cjk_charset_glyph_ratio = cjk_charset_glyph_count / charset_glyph_count if charset_glyph_count else 0.0

    signal.update(
        {
            "charset_glyph_count": charset_glyph_count,
            "latin_glyph_count": latin_glyph_count,
            "latin_glyph_ratio": latin_glyph_ratio,
            "cjk_charset_glyph_count": cjk_charset_glyph_count,
            "cjk_charset_glyph_ratio": cjk_charset_glyph_ratio,
        }
    )
    signal["triggered"] = (
        latin_glyph_count >= LATIN_CHARSET_MIN_LATIN_GLYPHS
        and latin_glyph_ratio >= LATIN_CHARSET_MIN_LATIN_RATIO
        and cjk_charset_glyph_count == 0
    )
    return signal


def _normalize_pdf_font_name(font_name: Any) -> str:
    """Standardize the PDF font name and unify the NameObject and PDFium return value formats of pypdf."""
    if font_name is None:
        return ""
    normalized_name = str(font_name).strip().lstrip("/")
    return re.sub(r"^[A-Z]{6}\+", "", normalized_name, count=1)


def _get_pdfium_char_font_name(text_page: Any, char_index: int) -> str:
    """Read the PDFium character-level font name to count the actual usage ratio of the suspicious CID font."""
    flags = c_int()
    buffer_length = pdfium_c.FPDFText_GetFontInfo(
        text_page,
        char_index,
        None,
        0,
        byref(flags),
    )
    if buffer_length <= 0:
        return ""

    font_name_buffer = create_string_buffer(buffer_length)
    actual_length = pdfium_c.FPDFText_GetFontInfo(
        text_page,
        char_index,
        font_name_buffer,
        buffer_length,
        byref(flags),
    )
    if actual_length <= 0:
        return ""

    return font_name_buffer.value.decode("utf-8", errors="ignore")


def _get_cid_font_usage_signal_from_samples(
    text_samples: list[dict[str, Any]], cid_font_usage: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    """Combining the content stream exact count with the PDFium total character count calculates the suspect CID font usage ratio."""
    best_signal = {
        "triggered": False,
        "page_index": None,
        "font_names": [],
        "cid_font_char_count": 0,
        "total_chars": 0,
        "cid_font_usage_ratio": 0.0,
    }

    for text_sample in text_samples:
        page_index = text_sample.get("page_index")
        total_chars = text_sample["char_count"]
        if total_chars <= 0:
            continue

        page_usage = cid_font_usage.get(page_index) or {}
        cid_font_char_count = int(page_usage.get("cid_font_char_count", 0))
        matched_font_names = sorted(page_usage.get("font_names") or [])

        cid_font_usage_ratio = cid_font_char_count / total_chars
        signal = {
            "triggered": False,
            "page_index": page_index,
            "font_names": matched_font_names,
            "cid_font_char_count": cid_font_char_count,
            "total_chars": total_chars,
            "cid_font_usage_ratio": cid_font_usage_ratio,
        }
        if cid_font_char_count >= CID_FONT_USAGE_COUNT_THRESHOLD and cid_font_usage_ratio >= CID_FONT_USAGE_RATIO_THRESHOLD:
            signal["triggered"] = True
            return signal

        if (
            signal["cid_font_usage_ratio"],
            signal["cid_font_char_count"],
        ) > (
            best_signal["cid_font_usage_ratio"],
            best_signal["cid_font_char_count"],
        ):
            best_signal = signal

    return best_signal


def _get_latin_font_cjk_usage_signal_from_samples(
    text_samples: list[dict[str, Any]],
    font_signal: dict[str, Any],
    count_threshold: int,
    usage_ratio_threshold: float,
    cjk_ratio_threshold: float,
) -> dict[str, Any]:
    """Statistics on actual usage of a single Latin candidate font and the proportion of CJK after decoding of PDFium."""
    best_signal = {
        "triggered": False,
        "page_index": None,
        "font_names": [],
        "font_char_count": 0,
        "cjk_char_count": 0,
        "total_chars": 0,
        "font_usage_ratio": 0.0,
        "font_cjk_ratio": 0.0,
    }
    if not font_signal or not font_signal.get("triggered"):
        return best_signal

    page_fonts = font_signal.get("page_fonts") or {}
    for text_sample in text_samples:
        page_index = text_sample.get("page_index")
        total_chars = text_sample.get("non_generated_char_count", 0)
        if total_chars <= 0:
            continue

        font_name_counts = text_sample.get("font_non_generated_char_counts") or {}
        font_cjk_char_counts = text_sample.get("font_non_generated_cjk_char_counts") or {}
        candidate_font_names = {_normalize_pdf_font_name(font_name) for font_name in page_fonts.get(page_index, set())}
        candidate_font_names.discard("")

        for font_name in sorted(candidate_font_names):
            font_char_count = font_name_counts.get(font_name, 0)
            cjk_char_count = font_cjk_char_counts.get(font_name, 0)
            font_usage_ratio = font_char_count / total_chars
            font_cjk_ratio = cjk_char_count / font_char_count if font_char_count else 0.0
            signal = {
                "triggered": False,
                "page_index": page_index,
                "font_names": [font_name] if font_char_count else [],
                "font_char_count": font_char_count,
                "cjk_char_count": cjk_char_count,
                "total_chars": total_chars,
                "font_usage_ratio": font_usage_ratio,
                "font_cjk_ratio": font_cjk_ratio,
            }
            if (
                font_char_count >= count_threshold
                and font_usage_ratio >= usage_ratio_threshold
                and font_cjk_ratio >= cjk_ratio_threshold
            ):
                signal["triggered"] = True
                return signal

            if (
                signal["font_cjk_ratio"],
                signal["font_usage_ratio"],
                signal["font_char_count"],
            ) > (
                best_signal["font_cjk_ratio"],
                best_signal["font_usage_ratio"],
                best_signal["font_char_count"],
            ):
                best_signal = signal

    return best_signal


def _get_u72xx_text_signal_from_samples(text_samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Based on the cached sample page text statistics, the U+7280-U+72DF character ratio after deducting common words."""
    cjk_chars = 0
    u72xx_count = 0

    for text_sample in text_samples:
        for char in text_sample["cleaned_text"]:
            unicode_code = ord(char)
            if 0x4E00 <= unicode_code <= 0x9FFF:
                cjk_chars += 1
            if (
                SUSPICIOUS_CJK_72XX_START <= unicode_code <= SUSPICIOUS_CJK_72XX_END
                and char not in SUSPICIOUS_CJK_72XX_WHITELIST
            ):
                u72xx_count += 1

    u72xx_cjk_ratio = 0.0
    if cjk_chars > 0:
        u72xx_cjk_ratio = u72xx_count / cjk_chars

    return {
        "cjk_chars": cjk_chars,
        "u72xx_count": u72xx_count,
        "u72xx_cjk_ratio": u72xx_cjk_ratio,
    }


def _get_sample_cleaned_text(text_sample: Any) -> str:
    """Compatible with dict and test double objects, reads the cleaned_text field of the sample page."""
    if isinstance(text_sample, dict):
        return str(text_sample.get("cleaned_text", ""))
    return str(getattr(text_sample, "cleaned_text", ""))


def _is_cjk_text_char(char: str) -> bool:
    """Determine whether the character belongs to the acceptable CJK text range in Chinese documents."""
    return _is_cjk_unicode_code(ord(char))


def _get_cross_script_name(char: str) -> str | None:
    """Identify common cross-script character block names in garbled Chinese documents."""
    unicode_code = ord(char)
    for start, end, script_name in SUSPICIOUS_CROSS_SCRIPT_RANGES:
        if start <= unicode_code <= end:
            return script_name
    return None


def _get_cross_script_text_signal_from_samples(text_samples: list[Any]) -> dict[str, Any]:
    """Statistics on a large proportion of cross-script mixed signals in the text layer of Chinese documents are used to identify legitimate Unicode error codes."""
    total_chars = 0
    cjk_chars = 0
    suspicious_chars = 0
    script_counts: dict[str, int] = {}

    for text_sample in text_samples:
        for char in _get_sample_cleaned_text(text_sample):
            total_chars += 1
            if _is_cjk_text_char(char):
                cjk_chars += 1

            script_name = _get_cross_script_name(char)
            if script_name is None:
                continue

            suspicious_chars += 1
            script_counts[script_name] = script_counts.get(script_name, 0) + 1

    suspicious_ratio = 0.0
    if total_chars > 0:
        suspicious_ratio = suspicious_chars / total_chars
    dense_script_count = sum(1 for count in script_counts.values() if count >= SUSPICIOUS_CROSS_SCRIPT_DENSE_SCRIPT_CHARS)
    top_scripts = sorted(
        script_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    triggered = (
        total_chars >= SUSPICIOUS_CROSS_SCRIPT_MIN_TEXT_CHARS
        and cjk_chars >= SUSPICIOUS_CROSS_SCRIPT_MIN_CJK_CHARS
        and suspicious_chars >= SUSPICIOUS_CROSS_SCRIPT_MIN_OTHER_SCRIPT_CHARS
        and suspicious_ratio >= SUSPICIOUS_CROSS_SCRIPT_OTHER_SCRIPT_RATIO
        and dense_script_count >= SUSPICIOUS_CROSS_SCRIPT_MIN_DENSE_SCRIPTS
    )

    return {
        "triggered": triggered,
        "total_chars": total_chars,
        "cjk_chars": cjk_chars,
        "suspicious_chars": suspicious_chars,
        "suspicious_ratio": suspicious_ratio,
        "script_counts": script_counts,
        "top_scripts": top_scripts,
        "dense_script_count": dense_script_count,
    }


def _count_ascii_punct_run_chars(text: str) -> int:
    """Count the number of consecutive ASCII punctuation characters, and only accumulate run whose length reaches the threshold."""
    run_chars = 0
    current_run = 0
    current_run_types: set[str] = set()

    for char in text:
        if char in ASCII_PUNCT_CHARS:
            current_run += 1
            current_run_types.add(char)
            continue

        if current_run >= ASCII_PUNCT_RUN_MIN_LENGTH and len(current_run_types) >= 2:
            run_chars += current_run
        current_run = 0
        current_run_types.clear()

    if current_run >= ASCII_PUNCT_RUN_MIN_LENGTH and len(current_run_types) >= 2:
        run_chars += current_run

    return run_chars


def _get_sampled_ascii_punct_signal_from_samples(text_samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Check the ASCII punctuation density of all sampled pages to identify garbled text without ToUnicode."""
    best_signal = {
        "triggered": False,
        "page_index": None,
        "cleaned_text_chars": 0,
        "ascii_punct_count": 0,
        "ascii_punct_ratio": 0.0,
        "ascii_punct_run_chars": 0,
        "punct_run_ratio": 0.0,
    }

    for text_sample in text_samples:
        page_index = text_sample.get("page_index")
        cleaned_text = text_sample["cleaned_text"]
        cleaned_text_chars = len(cleaned_text)
        ascii_punct_count = sum(1 for char in cleaned_text if char in ASCII_PUNCT_CHARS)
        ascii_punct_run_chars = _count_ascii_punct_run_chars(cleaned_text)

        ascii_punct_ratio = 0.0
        punct_run_ratio = 0.0
        if cleaned_text_chars > 0:
            ascii_punct_ratio = ascii_punct_count / cleaned_text_chars
            punct_run_ratio = ascii_punct_run_chars / cleaned_text_chars

        signal = {
            "triggered": False,
            "page_index": page_index,
            "cleaned_text_chars": cleaned_text_chars,
            "ascii_punct_count": ascii_punct_count,
            "ascii_punct_ratio": ascii_punct_ratio,
            "ascii_punct_run_chars": ascii_punct_run_chars,
            "punct_run_ratio": punct_run_ratio,
        }
        if (
            cleaned_text_chars >= SUSPICIOUS_ASCII_PUNCT_MIN_TEXT_CHARS
            and ascii_punct_ratio >= SUSPICIOUS_ASCII_PUNCT_RATIO_THRESHOLD
            and punct_run_ratio >= SUSPICIOUS_ASCII_PUNCT_RUN_RATIO_THRESHOLD
        ):
            signal["triggered"] = True
            return signal

        # When not triggered, the most suspicious sampling page indicators are retained to facilitate log expansion and subsequent troubleshooting of threshold boundaries.
        if (
            signal["punct_run_ratio"],
            signal["ascii_punct_ratio"],
            signal["cleaned_text_chars"],
        ) > (
            best_signal["punct_run_ratio"],
            best_signal["ascii_punct_ratio"],
            best_signal["cleaned_text_chars"],
        ):
            best_signal = signal

    return best_signal


def _get_pdf_object_cache_key(obj_ref: Any, obj: Any) -> tuple[Any, ...]:
    """Generates reusable identity keys for pypdf indirect or direct objects."""
    idnum = getattr(obj_ref, "idnum", None)
    generation = getattr(obj_ref, "generation", None)
    if idnum is not None:
        return "indirect", idnum, generation
    return "direct", id(obj)


def _get_font_resource_analysis(
    font_ref: Any,
    font_analysis_cache: dict[tuple[Any, ...], dict[str, bool]],
) -> tuple[Any, dict[str, bool]]:
    """Cache CID and Type1 font semantic analysis results by font resource object identity."""
    font = _resolve_pdf_object(font_ref)
    if not font:
        raise ValueError("Unable to resolve PDF font resource")

    cache_key = _get_pdf_object_cache_key(font_ref, font)
    analysis = font_analysis_cache.get(cache_key)
    if analysis is None:
        subtype = str(font.get("/Subtype"))
        encoding = str(font.get("/Encoding"))
        cid_without_to_unicode = (
            subtype == "/Type0"
            and encoding in ("/Identity-H", "/Identity-V")
            and "/DescendantFonts" in font
            and "/ToUnicode" not in font
        )
        latin_charset_signal = _get_latin_charset_with_to_unicode_signal(font)
        analysis = {
            "cid_without_to_unicode": cid_without_to_unicode,
            "latin_charset_with_to_unicode": latin_charset_signal["triggered"],
        }
        font_analysis_cache[cache_key] = analysis
    return font, analysis


def _get_pdf_string_raw_bytes(value: Any) -> bytes:
    """Read the raw bytes of a pypdf string object, disabling substitution with decoded text."""
    raw_bytes = getattr(value, "original_bytes", None)
    if isinstance(raw_bytes, bytes):
        return raw_bytes
    if isinstance(value, bytes):
        return value
    raise ValueError("PDF text string does not expose original bytes")


def _count_identity_cid_string(value: Any) -> int:
    """Counts the number of CIDs in a text string by double-byte encoding of Identity-H/V."""
    raw_bytes = _get_pdf_string_raw_bytes(value)
    if len(raw_bytes) % 2:
        raise ValueError("Identity CID text string has an odd byte length")
    return len(raw_bytes) // 2


def _resource_graph_has_cid_without_to_unicode(
    resources: Any,
    font_analysis_cache: dict[tuple[Any, ...], dict[str, bool]],
    visited_form_keys: set[tuple[Any, ...]] | None = None,
    visited_resource_keys: set[tuple[Any, ...]] | None = None,
) -> bool:
    """Recursively check the page and Form resource map for Identity CID fonts that are missing ToUnicode.

    The result is independent of the path. Form and the resource dictionary are each expanded once according to the object identity; this deduplication is only used for
    Resource reachability check does not affect actual drawing instances or glyph usage statistics.
    """
    resource_ref = resources
    resources = _resolve_pdf_object(resources)
    if not resources:
        return False

    if visited_form_keys is None:
        visited_form_keys = set()
    if visited_resource_keys is None:
        visited_resource_keys = set()
    resource_key = _get_pdf_object_cache_key(resource_ref, resources)
    if resource_key in visited_resource_keys:
        return False
    visited_resource_keys.add(resource_key)

    fonts = _resolve_pdf_object(resources.get("/Font")) or {}
    for font_ref in fonts.values():
        _font, analysis = _get_font_resource_analysis(
            font_ref,
            font_analysis_cache,
        )
        if analysis["cid_without_to_unicode"]:
            return True

    xobjects = _resolve_pdf_object(resources.get("/XObject")) or {}
    for xobject_ref in xobjects.values():
        xobject = _resolve_pdf_object(xobject_ref)
        if not xobject or str(xobject.get("/Subtype")) != "/Form":
            continue

        form_key = _get_pdf_object_cache_key(xobject_ref, xobject)
        if form_key in visited_form_keys:
            continue
        visited_form_keys.add(form_key)
        form_resources = xobject.get("/Resources")
        if form_resources is None:
            continue
        if _resource_graph_has_cid_without_to_unicode(
            form_resources,
            font_analysis_cache,
            visited_form_keys,
            visited_resource_keys,
        ):
            return True
    return False


def _count_cid_font_usage_in_content(
    reader: PdfReader,
    content: Any,
    resources: Any,
    font_analysis_cache: dict[tuple[Any, ...], dict[str, bool]],
    *,
    inherited_font: tuple[str, bool] | None = None,
    active_form_keys: frozenset[tuple[Any, ...]] = frozenset(),
) -> Counter[str]:
    """Identity CID glyphs for ToUnicode are missing from the content stream by recursive counting of actual Tf resources."""
    counts: Counter[str] = Counter()
    if content is None:
        return counts

    resources = _resolve_pdf_object(resources)
    if resources is None:
        raise ValueError("PDF content stream has no resolvable resources")

    fonts = _resolve_pdf_object(resources.get("/Font")) or {}
    xobjects = _resolve_pdf_object(resources.get("/XObject")) or {}
    current_font = inherited_font
    font_stack: list[tuple[str, bool] | None] = []

    for operands, operator in ContentStream(content, reader).operations:
        if operator == b"q":
            font_stack.append(current_font)
            continue
        if operator == b"Q":
            current_font = font_stack.pop() if font_stack else inherited_font
            continue
        if operator == b"Tf":
            if not operands:
                raise ValueError("PDF Tf operator has no font resource name")
            font_key = operands[0]
            font_ref = fonts.get(font_key)
            if font_ref is None:
                raise ValueError(f"Unable to resolve PDF font resource {font_key}")
            font, analysis = _get_font_resource_analysis(
                font_ref,
                font_analysis_cache,
            )
            font_name = _normalize_pdf_font_name(font.get("/BaseFont") or font_key)
            current_font = (
                font_name,
                analysis["cid_without_to_unicode"],
            )
            continue

        if operator in (b"Tj", b"'", b'"'):
            if current_font is None:
                raise ValueError("PDF text is shown before selecting a font")
            if current_font[1]:
                counts[current_font[0]] += _count_identity_cid_string(operands[-1])
            continue
        if operator == b"TJ":
            if current_font is None:
                raise ValueError("PDF text is shown before selecting a font")
            if current_font[1]:
                for value in operands[0]:
                    if isinstance(value, (int, float)):
                        continue
                    counts[current_font[0]] += _count_identity_cid_string(value)
            continue
        if operator != b"Do":
            continue

        if not operands:
            raise ValueError("PDF Do operator has no XObject resource name")
        xobject_key = operands[0]
        xobject_ref = xobjects.get(xobject_key)
        if xobject_ref is None:
            raise ValueError(f"Unable to resolve PDF XObject resource {xobject_key}")
        xobject = _resolve_pdf_object(xobject_ref)
        if not xobject:
            raise ValueError(f"Unable to resolve PDF XObject {xobject_key}")
        if str(xobject.get("/Subtype")) != "/Form":
            continue

        form_key = _get_pdf_object_cache_key(xobject_ref, xobject)
        if form_key in active_form_keys:
            raise ValueError(f"Cyclic PDF Form XObject reference {xobject_key}")
        form_resources = xobject.get("/Resources")
        child_resources = resources if form_resources is None else form_resources
        counts.update(
            _count_cid_font_usage_in_content(
                reader,
                xobject,
                child_resources,
                font_analysis_cache,
                inherited_font=current_font,
                active_form_keys=active_form_keys | {form_key},
            )
        )
    return counts


def _get_font_resource_signals_pypdf(
    pdf_bytes: bytes,
    page_indices: list[int],
) -> dict[str, Any]:
    """Scan the sample page font resources in one pass to collect CID missing mappings and Type1 Latin candidate fonts."""
    reader = PdfReader(BytesIO(pdf_bytes))
    cid_page_fonts: dict[int, set[str]] = {}
    cid_page_usage: dict[int, dict[str, Any]] = {}
    latin_charset_page_fonts: dict[int, set[str]] = {}
    font_analysis_cache: dict[tuple[Any, ...], dict[str, bool]] = {}

    for page_index in page_indices:
        page = reader.pages[page_index]
        resources = _resolve_pdf_object(page.get("/Resources"))
        if not resources:
            continue

        fonts = _resolve_pdf_object(resources.get("/Font")) or {}

        # Type1 Latin signals still use PDFium statistics according to font names; CID usage will be accurately calculated according to resource objects in the future.
        page_latin_font_resources: dict[str, dict[tuple[Any, ...], bool]] = {}
        for font_key, font_ref in fonts.items():
            font, analysis = _get_font_resource_analysis(
                font_ref,
                font_analysis_cache,
            )
            font_name = _normalize_pdf_font_name(font.get("/BaseFont") or font_key)
            if not font_name:
                continue

            cache_key = _get_pdf_object_cache_key(font_ref, font)

            if analysis["cid_without_to_unicode"]:
                cid_page_fonts.setdefault(page_index, set()).add(font_name)

            page_latin_font_resources.setdefault(font_name, {})[cache_key] = analysis["latin_charset_with_to_unicode"]

        for font_name, resource_states in page_latin_font_resources.items():
            if len(resource_states) == 1 and set(resource_states.values()) == {True}:
                latin_charset_page_fonts.setdefault(page_index, set()).add(font_name)

        if _resource_graph_has_cid_without_to_unicode(
            resources,
            font_analysis_cache,
        ):
            usage_counts = _count_cid_font_usage_in_content(
                reader,
                page.get_contents(),
                resources,
                font_analysis_cache,
            )
            cid_page_usage[page_index] = {
                "font_names": sorted(font_name for font_name, char_count in usage_counts.items() if char_count > 0),
                "cid_font_char_count": sum(usage_counts.values()),
            }

    return {
        "cid_without_to_unicode": {
            "triggered": bool(cid_page_fonts),
            "page_fonts": cid_page_fonts,
        },
        "cid_without_to_unicode_usage": cid_page_usage,
        "latin_charset_with_to_unicode": {
            "triggered": bool(latin_charset_page_fonts),
            "page_fonts": latin_charset_page_fonts,
        },
    }


def _resolve_pdf_object(obj: Any) -> Any:
    if hasattr(obj, "get_object"):
        return obj.get_object()
    return obj


def _page_image_coverage_ratio(page: Any) -> float:
    """Statistical single page image coverage: Type-filtered native walk, no Python wrapper built per object."""
    from .native_objects import _clipped_objects_of_type

    page_bbox = page.get_bbox()
    page_area = abs((page_bbox[2] - page_bbox[0]) * (page_bbox[3] - page_bbox[1]))
    if page_area <= 0:
        return 0.0

    image_area = 0.0
    # Depth 3 is equivalent to old page.get_objects (max_depth=3): page with two levels of nested images within Form.
    for member in _clipped_objects_of_type(page, pdfium_c.FPDF_PAGEOBJ_IMAGE, 3):
        values = [c_float() for _ in range(4)]
        if not pdfium_c.FPDFPageObj_GetBounds(member.raw, *(byref(value) for value in values)):
            continue
        left, bottom, right, top = (value.value for value in values)
        image_area += max(0.0, right - left) * max(0.0, top - bottom)

    return min(image_area / page_area, 1.0)


def get_high_image_coverage_ratio_pdfium(pdf_doc: pdfium.PdfDocument, page_indices: list[int]) -> float:
    high_image_coverage_pages = 0

    with pdfium_guard():
        for page_index in page_indices:
            page = None
            try:
                page = pdf_doc[page_index]
                coverage_ratio = _page_image_coverage_ratio(page)
                if coverage_ratio >= HIGH_IMAGE_COVERAGE_THRESHOLD:
                    high_image_coverage_pages += 1
            finally:
                close_pdfium_child(page)

    if not page_indices:
        return 0.0
    return high_image_coverage_pages / len(page_indices)


if __name__ == "__main__":
    from ._document import PDFDocument

    with open("/Users/myhloli/pdf/luanma2x10.pdf", "rb") as f:
        p_bytes = f.read()
        pdf_doc = PDFDocument(p_bytes)
        logger.info(f"PDF classify result: {pdf_doc.classify()}")

# Custom statistical rules retain the original calling order and exception semantics and are not quietly overwritten by the kernel.
_STANDARD_CLASSIFICATION_FUNCTIONS = (
    _is_disallowed_control_unicode,
    _is_cjk_unicode_code,
    _get_pdfium_char_font_name,
    _normalize_pdf_font_name,
)
