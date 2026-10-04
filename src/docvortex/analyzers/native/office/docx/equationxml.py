"""DOCX compatibility mode VML ``equationxml`` formula decoder."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib

from lxml import etree  # type: ignore[reportAttributeAccessIssue]

from ..equation.omml import oMath2Latex
from ..errors import LegacyOfficeResourceLimitError
from ..limits import MAX_ASSET_TOTAL_BYTES, MAX_ENTRY_BYTES

WORD_2003_XML_NAMESPACE = "http://schemas.microsoft.com/office/word/2003/wordml"
OFFICE_MATH_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_WORD_DOCUMENT_TAG = f"{{{WORD_2003_XML_NAMESPACE}}}wordDocument"
_WORD_PARAGRAPH_TAG = f"{{{WORD_2003_XML_NAMESPACE}}}p"
_MATH_PARAGRAPH_TAG = f"{{{OFFICE_MATH_NAMESPACE}}}oMathPara"
_MATH_TAG = f"{{{OFFICE_MATH_NAMESPACE}}}oMath"
_FORBIDDEN_EQUATIONXML_ELEMENTS = frozenset(
    {
        "annotation",
        "endnote",
        "footnote",
        "ftr",
        "hdr",
        "pict",
        "textbox",
        "txbxContent",
    }
)


def _has_forbidden_equationxml_content(root: etree._Element) -> bool:
    """Determine whether Equation XML contains Word 2003 nodes that are prohibited by the specification or cannot be reliably restored."""

    if root.getroottree().xpath("boolean(//comment())"):
        return True
    for element in root.iter():
        tag = getattr(element, "tag", None)
        if not isinstance(tag, str):
            continue
        qname = etree.QName(tag)
        if qname.localname in _FORBIDDEN_EQUATIONXML_ELEMENTS:
            return True
    return False


def _decode_equationxml_document(payload: bytes) -> str | None:
    """Safely parse complete Word 2003 XML documents and convert unique OMML formulas to LaTeX."""

    parser = etree.XMLParser(
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
        recover=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(payload, parser=parser)
    except (etree.XMLSyntaxError, TypeError, ValueError):
        return None

    if root.tag != _WORD_DOCUMENT_TAG:
        return None
    if root.getroottree().docinfo.doctype:
        return None
    if _has_forbidden_equationxml_content(root):
        return None

    paragraphs = root.findall(f".//{_WORD_PARAGRAPH_TAG}")
    if len(paragraphs) != 1:
        return None
    math_paragraphs = paragraphs[0].findall(f"./{_MATH_PARAGRAPH_TAG}")
    if len(math_paragraphs) != 1:
        return None
    equations = math_paragraphs[0].findall(f"./{_MATH_TAG}")
    if len(equations) != 1 or len(root.findall(f".//{_MATH_TAG}")) != 1:
        return None

    try:
        latex = str(oMath2Latex(equations[0])).strip()
    except Exception:
        return None
    return latex or None


@dataclass(slots=True)
class DocxEquationXmlDecoder:
    """Cache and decode DOCX VML ``equationxml`` per shared resource limit."""

    total_bytes: int = 0
    _cache: dict[bytes, str | None] = field(default_factory=dict)

    def decode(self, equation_xml: object | None) -> str | None:
        """LaTeX is returned after verifying the attribute type, resource budget, and Word 2003 XML structure."""

        if not isinstance(equation_xml, str) or not equation_xml.strip():
            return None
        try:
            payload = equation_xml.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if len(payload) > MAX_ENTRY_BYTES:
            raise LegacyOfficeResourceLimitError(f"DOCX equationxml exceeds max_entry_bytes={MAX_ENTRY_BYTES}")

        digest = hashlib.sha256(payload).digest()
        if digest in self._cache:
            return self._cache[digest]
        if self.total_bytes + len(payload) > MAX_ASSET_TOTAL_BYTES:
            raise LegacyOfficeResourceLimitError(
                f"DOCX equationxml payloads exceed max_asset_total_bytes={MAX_ASSET_TOTAL_BYTES}"
            )

        self.total_bytes += len(payload)
        latex = _decode_equationxml_document(payload)
        self._cache[digest] = latex
        return latex
