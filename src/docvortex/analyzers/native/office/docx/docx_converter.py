from io import BytesIO
from typing import Any, BinaryIO, Optional

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.text.paragraph import Paragraph
from loguru import logger
from lxml import etree

from ..equation.ooxml import OoxmlEquationDecoder
from ..equation.image import OfficeImageEquationDecoder
from .....schema import RAW_CAPTION
from .equationxml import DocxEquationXmlDecoder
from .package_normalizer import normalize_docx_package
from .....schema import BlockType
from .....content.spans import (
    append_text_span,
    extend_inline_spans,
    inline_span_plain_text,
    text_spans,
)


from .context import (
    _DocxConstants,
    _DocxComplexFieldFrame as _DocxComplexFieldFrame,
    _ParagraphElement as _ParagraphElement,
    _ParagraphHyperlink as _ParagraphHyperlink,
)
from .resources import _DocxResources
from .styles import _DocxStyles
from .numbering import _DocxNumbering
from .fields import _DocxFields
from .tables import _DocxTables


class DocxConverter(_DocxConstants, _DocxResources, _DocxStyles, _DocxNumbering, _DocxFields, _DocxTables):
    """Arrange DOCX document life cycle, paragraph traversal and various responsibility processing."""

    def __init__(self):
        """Configure the fixed XML capability and establish an independent state for this document conversion."""
        self.XML_KEY = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
        self.xml_namespaces = {"w": "http://schemas.microsoft.com/office/word/2003/wordml"}
        self.picture_xpath_expr = etree.XPath(".//a:blip | .//v:imagedata", namespaces=DocxConverter._BLIP_NAMESPACES)
        self.equation_bookends: str = "<eq>{EQ}</eq>"  # Formula mark format
        self._reset_document_state()

    def _reset_document_state(self) -> None:
        """Each conversion re-creates the variable state and decoder to avoid failure or reuse instances leaving old documents."""
        self._close_mammoth_fallback_context()
        self._fallback_docx_bytes: Optional[bytes] = (
            None  # Only the original package after normalization is retained during conversion
        )
        self.docx_obj = None
        self.pages = []
        self.cur_page = []
        self._mammoth_tables_html: list = []  # mammoth pre-parsing HTML aligned with text top table, None represents fallback parsing
        self._mammoth_table_idx: int = 0  # Current pre-parsed table cursor
        self.pre_num_id: int = -1  # numId of the previous processing element
        self.pre_ilevel: int = (
            -1
        )  # The indentation level of the previous processing element is used to determine the list level
        self.list_block_stack: list = []  # List block stack
        self.list_counters: dict[tuple[int, int], int] = {}  # List counter (numId, ilvl) -> count
        self.index_block_stack: list = []  # Directory index block stack
        self.pre_index_ilevel: int = -1  # The indentation level of the previous directory entry
        self.plain_toc_base_level: Optional[int] = None  # The starting level of ordinary directory paragraphs
        self.heading_list_numids: set = set()  # List numId set used as chapter title
        self.processed_textbox_elements: list = []
        self.toc_anchor_set: set[str] = set()  # TOC hyperlink target anchor point set
        self.toc_anchor_aliases: dict[
            str, str
        ] = {}  # The mapping from TOC bookmark to the only public anchor in the same text paragraph
        self._numbering_root: Optional[BaseOxmlElement] = None
        self._numbering_root_loaded: bool = False
        self._numbering_level_cache: dict[tuple[int, int], Optional[BaseOxmlElement]] = {}
        self._numbering_start_cache: dict[tuple[int, int], int] = {}
        self._style_lookup_cache: dict[tuple[Any, Optional[str]], Any] = {}
        self._style_bool_cache: dict[tuple[int, str], Optional[bool]] = {}
        self._ooxml_equation_decoder = OoxmlEquationDecoder()
        self._mtef_warned_relations: set[tuple[str, str]] = set()
        self._equationxml_decoder = DocxEquationXmlDecoder()
        self._equationxml_warned_shapes: set[tuple[str, str, str]] = set()
        self._image_equation_decoder = OfficeImageEquationDecoder()

    @staticmethod
    def _local_name(element: Any) -> Optional[str]:
        """Safely obtain the local tag name of the XML element, and return None when encountering non-element nodes such as comments or processing instructions."""
        tag = getattr(element, "tag", None)
        if not isinstance(tag, str):
            return None
        try:
            return etree.QName(tag).localname
        except ValueError:
            return None

    def _reset_style_caches(self) -> None:
        """Reset the style query cache to avoid reusing old document styles when the same converter instance is converted multiple times."""
        self._style_lookup_cache = {}
        self._style_bool_cache = {}

    @staticmethod
    def _docx_part_key(part: Any) -> str:
        """Return the DOCX part name used for formula relationship caching and alarm deduplication."""

        return str(getattr(part, "partname", ""))

    def _require_document_part(self) -> Any:
        """Return the loaded main document part, and fail immediately when the life cycle is abnormal."""

        if self.docx_obj is None:
            raise ValueError("DOCX document part is not initialized")
        return self.docx_obj.part

    def _sanitize_missing_internal_relationships(self, file_bytes: bytes) -> bytes:
        """Standardize DOCX package to be compatible with missing internal relations and corrupted picture members."""
        return normalize_docx_package(file_bytes)

    def _start_new_page(self) -> None:
        self.cur_page = []
        self.pages.append(self.cur_page)

    def _is_layout_only_section_break(self, element: BaseOxmlElement) -> bool:
        w_ns = DocxConverter._BLIP_NAMESPACES["w"]
        p_pr = element.find(f"{{{w_ns}}}pPr")
        sect_pr = p_pr.find(f"{{{w_ns}}}sectPr") if p_pr is not None else None
        if sect_pr is None:
            return False

        paragraph = Paragraph(element, self.docx_obj)
        if self._get_paragraph_text(paragraph).strip():
            return False

        if self.picture_xpath_expr(element):
            return False

        sect_type = sect_pr.find(f"{{{w_ns}}}type")
        sect_val = sect_type.get(f"{{{w_ns}}}val", "continuous") if sect_type is not None else "continuous"
        if sect_val != "continuous":
            return False

        pg_mar = sect_pr.find(f"{{{w_ns}}}pgMar")
        if pg_mar is None:
            return False

        for attr in ("header", "footer", "top", "bottom", "left", "right"):
            if pg_mar.get(f"{{{w_ns}}}{attr}", "0") != "0":
                return False
        return True

    def convert(
        self,
        file_stream: BinaryIO,
    ):
        """After resetting the document status, read, pre-parse the table and traverse the text in the original order."""
        self._reset_document_state()
        # Read file bytes so that mammoth and python-docx each use independent read streams
        file_bytes = self._sanitize_missing_internal_relationships(file_stream.read())
        self._fallback_docx_bytes = file_bytes
        try:
            # Use the complete DOCX context to pre-parse the top-level table to avoid the waste of resources caused by converting non-table text
            self._mammoth_tables_html = self._preparse_tables_with_mammoth(file_bytes)
            self._mammoth_table_idx = 0
            self.docx_obj = Document(BytesIO(file_bytes))
            self.toc_anchor_set = self._collect_toc_anchor_set()
            self.toc_anchor_aliases = self._collect_toc_anchor_aliases(
                self.toc_anchor_set,
            )
            # Pre-scan document, identify list numId used as chapter title
            self.heading_list_numids = self._detect_heading_list_numids()
            self.pages.append(self.cur_page)
            self._walk_linear(self.docx_obj.element.body)
            self._add_header_footer(self.docx_obj)
        finally:
            self._close_mammoth_fallback_context()
            self._fallback_docx_bytes = None

    def _close_active_list(self) -> None:
        """Close the currently active list block but retain the consecutive number count for Word numId."""
        self.pre_num_id = -1
        self.pre_ilevel = -1
        self.list_block_stack = []

    def _reset_index_state(self) -> None:
        """Reset the directory index stack to prevent multiple separated directory blocks from being incorrectly merged."""
        self.index_block_stack = []
        self.pre_index_ilevel = -1
        self.plain_toc_base_level = None

    def _walk_linear(
        self,
        body: BaseOxmlElement,
    ):
        for element in body:
            # Get the tag name of the element (remove namespace prefix)
            tag_name = self._local_name(element)
            if tag_name is None:
                continue
            # Check if there is an inline image (blip element)
            picture_refs = self.picture_xpath_expr(element)

            # Find all drawing elements (for processing DrawingML)
            drawingml_els = element.findall(".//w:drawing", namespaces=DocxConverter._BLIP_NAMESPACES)
            if drawingml_els:
                self._handle_drawingml(drawingml_els)

            # Check text box content (supports multiple text box formats)
            # Process only if the element has not been processed before
            if element not in self.processed_textbox_elements:
                # Modern Word text box
                txbx_xpath = etree.XPath(
                    ".//w:txbxContent|.//v:textbox//w:p",
                    namespaces=DocxConverter._BLIP_NAMESPACES,
                )
                textbox_elements = txbx_xpath(element)

                # Modern text box not found, check alternative/legacy text box format
                if not textbox_elements and tag_name in ["drawing", "pict"]:
                    # Additional check of text boxes in DrawingML and VML formats
                    alt_txbx_xpath = etree.XPath(
                        ".//wps:txbx//w:p|.//w10:wrap//w:p|.//a:p//a:t",
                        namespaces=DocxConverter._BLIP_NAMESPACES,
                    )
                    textbox_elements = alt_txbx_xpath(element)

                    # Check for shape text that is not within a standard text box
                    if not textbox_elements:
                        shape_text_xpath = etree.XPath(
                            ".//a:bodyPr/ancestor::*//a:t|.//a:txBody//a:t",
                            namespaces=DocxConverter._BLIP_NAMESPACES,
                        )
                        shape_text_elements = shape_text_xpath(element)
                        if shape_text_elements:
                            # Create custom text elements from shape text
                            raw_text = " ".join([t.text for t in shape_text_elements if t.text])
                            text_content = self._normalize_text_block_content(text_spans(raw_text))
                            visible_text = inline_span_plain_text(text_content)
                            if visible_text:
                                logger.debug(f"Found shape text: {visible_text[:50]}...")
                                self.cur_page.append(
                                    {
                                        "type": BlockType.TEXT,
                                        "content": text_content,
                                    }
                                )
                if textbox_elements:
                    self.processed_textbox_elements.append(element)
                    for tb_element in textbox_elements:
                        self.processed_textbox_elements.append(tb_element)

                    logger.debug(f"Found textbox content with {len(textbox_elements)} elements")
                    self._handle_textbox_content(textbox_elements)

            if tag_name == "tbl":
                # Tables are top-level block-level elements that interrupt the context of the active list.
                # If the list status is not reset, subsequent list items will be appended to the list block created before the table.
                # As a result, the table appears after those list items in cur_page, causing the order to be disordered.
                if self.pre_num_id != -1:
                    self._close_active_list()
                try:
                    # Process table elements
                    self._handle_tables(element)
                except Exception as e:
                    # Failure in table parsing will result in the loss of the entire table, and the exception details need to be exposed at the warning level.
                    logger.warning(f"Could not parse a table, broken docx table: {e}")
            # Check picture elements
            elif picture_refs:
                # Determine whether the picture is an anchored (floating) picture
                is_anchored = bool(
                    element.findall(
                        ".//wp:anchor",
                        namespaces=DocxConverter._BLIP_NAMESPACES,
                    )
                )
                # The anchor picture is floating and positioned in the paragraph, and the paragraph text should appear before the picture.
                if is_anchored and tag_name == "p":
                    self._handle_text_elements(element)
                    self._handle_pictures(
                        picture_refs,
                        part=self._require_document_part(),
                    )
                else:
                    # Processing picture elements
                    self._handle_pictures(
                        picture_refs,
                        part=self._require_document_part(),
                    )
                    # If it is a paragraph element, the text content (such as descriptive text) is processed simultaneously
                    if tag_name == "p":
                        self._handle_text_elements(element)
            # Check sdt element
            elif tag_name == "sdt":
                sdt_content = element.find(".//w:sdtContent", namespaces=DocxConverter._BLIP_NAMESPACES)
                if sdt_content is not None:
                    if self._is_toc_sdt(element):
                        # Process directory SDT and convert to INDEX block
                        self._handle_sdt_as_index(sdt_content)
                    else:
                        # Other SDT elements are processed as ordinary text
                        paragraphs = sdt_content.findall(".//w:p", namespaces=DocxConverter._BLIP_NAMESPACES)
                        for p in paragraphs:
                            self._handle_text_elements(p)
            # Check text paragraph element
            elif tag_name == "p":
                # Process text elements (including paragraph attributes such as "tcPr", "sectPr", etc.)
                self._handle_text_elements(element)

            # Ignore other unknown elements and log
            else:
                logger.debug(f"Ignoring element in DOCX with tag: {tag_name}")

    def _handle_text_elements(
        self,
        element: BaseOxmlElement,
    ):
        """
        Process text elements.

        Args:
            element: element object
            doc: DoclingDocument object

        Returns:

        """
        is_section_end = False
        has_section_break = element.find(".//w:sectPr", namespaces=DocxConverter._BLIP_NAMESPACES) is not None
        if has_section_break and not self._is_layout_only_section_break(element):
            # If there is no text content
            if element.text == "":
                self._start_new_page()
            else:
                # Mark the end of this section, and then divide into sections after processing the text
                is_section_end = True
        paragraph = Paragraph(element, self.docx_obj)
        paragraph_elements = self._get_paragraph_elements(paragraph)
        paragraph_text = self._get_paragraph_text(paragraph)
        paragraph_anchor = self._extract_paragraph_bookmark(element)
        text, equations = self._handle_equations_in_text(
            element=element,
            text=paragraph_text,
            part=paragraph.part,
        )

        if text is None:
            return None
        text = text.strip()

        if self._handle_plain_toc_paragraph_as_index(
            paragraph=paragraph,
            paragraph_element=element,
            paragraph_elements=paragraph_elements,
            text=text,
            equations=equations,
        ):
            # Ordinary TOC is the list boundary to avoid subsequent merging of the same numId list items into the list block before the directory.
            if self.pre_num_id != -1:
                self._close_active_list()
            # After the ordinary TOC paragraph is converted into INDEX, the section and page semantics at the end of the paragraph must also be retained.
            if is_section_end:
                self._start_new_page()
            return None
        self._reset_index_state()

        # Common bulleted and numbered list styles.
        # "List Bullet", "List Number", "List Paragraph"
        # Identify whether a list is a numbered list
        p_style_id, p_level = self._get_label_and_level(paragraph)
        p_style_id = p_style_id or "Normal"
        numid, ilevel = self._get_numId_and_ilvl(paragraph)

        if numid == 0:
            numid = None

        # Process list
        if numid is not None and ilevel is not None and p_style_id not in ["Title", "Heading"]:
            # Confirm if this is actually a numbered list by checking numFmt
            is_numbered = self._is_numbered_list(numid, ilevel)

            if numid in self.heading_list_numids:
                # This list is used as a chapter title (with text interspersed between list items) and is converted directly to title block
                # Close any active normal lists first
                if self.pre_num_id != -1:
                    self._close_active_list()
                content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)
                if content_text:
                    title_block = {
                        "type": BlockType.PARAGRAPH_TITLE,
                        "level": ilevel + 2,
                        "is_numbered_style": is_numbered,
                        "content": content_text,
                    }
                    if paragraph_anchor:
                        title_block["anchor"] = paragraph_anchor
                    self.cur_page.append(title_block)
            else:
                self._add_list_item(
                    numid=numid,
                    ilevel=ilevel,
                    elements=paragraph_elements,
                    is_numbered=is_numbered,
                    text=text,
                    equations=equations,
                )
            # The list item has been processed and returned
            return None
        elif (  # End of list processing
            numid is None and self.pre_num_id != -1 and p_style_id not in ["Title", "Heading"]
        ):  # Close list
            # Reset list status
            self._close_active_list()

        if p_style_id in ["Title"]:
            # Construct text containing formulas and hyperlinks
            content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)
            if content_text:
                title_block = {
                    "type": BlockType.DOC_TITLE,
                    "level": 1,
                    "content": content_text,
                }
                if paragraph_anchor:
                    title_block["anchor"] = paragraph_anchor
                self.cur_page.append(title_block)

        elif "Heading" in p_style_id:
            is_numbered_style = numid is not None and ilevel is not None and self._is_numbered_list(numid, ilevel)
            # Construct text containing formulas and hyperlinks
            content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)
            if content_text:
                h_block = {
                    "type": BlockType.PARAGRAPH_TITLE,
                    "level": max(p_level + 1, 2) if p_level is not None else 2,
                    "is_numbered_style": is_numbered_style,
                    "content": content_text,
                }
                if paragraph_anchor:
                    h_block["anchor"] = paragraph_anchor
                self.cur_page.append(h_block)

        elif equations:
            equation_values = [value for kind, value in equations if kind == "equation"]
            if (paragraph_text is None or len(paragraph_text.strip()) == 0) and equation_values:
                # independent formula
                eq_block = {
                    "type": BlockType.EQUATION,
                    "content": equation_values[0] if len(equation_values) == 1 else "\n".join(equation_values),
                }
                self.cur_page.append(eq_block)
            else:
                # Text block containing inline formulas, also supports hyperlinks
                content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)
                content_text = self._normalize_text_block_content(content_text)
                if content_text:
                    text_with_inline_eq_block = {
                        "type": BlockType.TEXT,
                        "content": content_text,
                    }
                    if paragraph_anchor:
                        text_with_inline_eq_block["anchor"] = paragraph_anchor
                    self.cur_page.append(text_with_inline_eq_block)
        elif p_style_id in [
            "Paragraph",
            "Normal",
            "Subtitle",
            "Author",
            "DefaultText",
            "ListParagraph",
            "ListBullet",
            "Quote",
        ]:
            # Construct text containing formulas and hyperlinks
            content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)
            content_text = self._normalize_text_block_content(content_text)
            if content_text:
                text_block = {
                    "type": BlockType.TEXT,
                    "content": content_text,
                }
                if paragraph_anchor:
                    text_block["anchor"] = paragraph_anchor
                self.cur_page.append(text_block)
        # Determine whether it is Caption
        elif self._is_caption(element):
            # Construct text containing formulas and hyperlinks
            content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)
            if content_text:
                caption_block = {
                    "type": RAW_CAPTION,
                    "content": content_text,
                }
                self.cur_page.append(caption_block)
        else:
            # Text style names not only have default values, but may also have user-defined values
            # So we treat all other tags as plain text
            # Construct text containing formulas and hyperlinks
            content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)
            content_text = self._normalize_text_block_content(content_text)
            if content_text:
                text_block = {
                    "type": BlockType.TEXT,
                    "content": content_text,
                }
                if paragraph_anchor:
                    text_block["anchor"] = paragraph_anchor
                self.cur_page.append(text_block)

        if is_section_end:
            self._start_new_page()

    def _process_header_footer_paragraph(self, paragraph: Paragraph) -> list[dict[str, Any]]:
        """
        Works with individual paragraphs in header/footer, supports inline formulas and hyperlinks.

        Args:
            paragraph: paragraph object

        Returns:
            list[dict]: Processed structured Span
        """
        paragraph_elements = self._get_paragraph_elements(paragraph)
        paragraph_text = self._get_paragraph_text(paragraph)
        text, equations = self._handle_equations_in_text(
            element=paragraph._element,
            text=paragraph_text,
            part=paragraph.part,
        )

        text = text.strip()
        if not text and not equations:
            return []

        # Construct text containing formulas and hyperlinks
        content_text = self._build_text_with_equations_and_hyperlinks(paragraph_elements, text, equations)

        return content_text

    def _add_header_footer(self, docx_obj: DocxDocument) -> None:
        """
        Process headers and footers, add them to the pages list in section order, and filter out empty strings and pure numeric content
        It is divided into two situations: whether odd and even pages are enabled for the entire document and whether the home page is enabled for each section.
        Supports inline formulas and hyperlinks, and removes duplicates based on type
        """
        is_odd_even_different = docx_obj.settings.odd_and_even_pages_header_footer
        for sec_idx, section in enumerate(docx_obj.sections):
            # collection for deduplication
            added_headers = set()
            added_footers = set()

            hdrs = [section.header]
            if is_odd_even_different:
                hdrs.append(section.even_page_header)
            if section.different_first_page_header_footer:
                hdrs.append(section.first_page_header)
            for hdr in hdrs:
                # Work with each paragraph, supporting formulas and hyperlinks
                processed_parts: list[list[dict[str, Any]]] = []
                for par in hdr.paragraphs:
                    content = self._process_header_footer_paragraph(par)
                    if content:
                        processed_parts.append(content)
                spans: list[dict[str, Any]] = []
                for part in processed_parts:
                    if spans:
                        append_text_span(spans, " ")
                    extend_inline_spans(spans, part)
                visible = inline_span_plain_text(spans)
                if spans and not visible.isdigit() and visible not in added_headers:
                    added_headers.add(visible)
                    try:
                        self.pages[sec_idx].append(
                            {
                                "type": BlockType.HEADER,
                                "content": spans,
                            }
                        )
                    except IndexError:
                        logger.error("Section index out of range when adding header.")

            ftrs = [section.footer]
            if is_odd_even_different:
                ftrs.append(section.even_page_footer)
            if section.different_first_page_header_footer:
                ftrs.append(section.first_page_footer)
            for ftr in ftrs:
                # Work with each paragraph, supporting formulas and hyperlinks
                processed_parts = []
                for par in ftr.paragraphs:
                    content = self._process_header_footer_paragraph(par)
                    if content:
                        processed_parts.append(content)
                spans = []
                for part in processed_parts:
                    if spans:
                        append_text_span(spans, " ")
                    extend_inline_spans(spans, part)
                visible = inline_span_plain_text(spans)
                if spans and not visible.isdigit() and visible not in added_footers:
                    added_footers.add(visible)
                    try:
                        self.pages[sec_idx].append(
                            {
                                "type": BlockType.FOOTER,
                                "content": spans,
                            }
                        )
                    except IndexError:
                        logger.error("Section index out of range when adding footer.")

    def _is_caption(self, element: BaseOxmlElement) -> bool:
        """
        Determine whether it is caption based on whether there is a SEQ field in insertText.

        Args:
            element: paragraph element object

        Returns:
            bool: If it is a title, return True, otherwise return False
        """
        instr_texts = element.findall(".//w:instrText", namespaces=DocxConverter._BLIP_NAMESPACES)

        for instr in instr_texts:
            if instr.text and "SEQ" in instr.text:
                return True

        return False
