"""DOCX table processing; sharing the current single document status of Converter."""

import base64
import re
from contextlib import ExitStack
from io import BytesIO
from typing import Any, Optional
from docx import Document
from docx.oxml.xmlchemy import BaseOxmlElement
from loguru import logger
from mammoth import docx as mammoth_docx, images as mammoth_images
from mammoth import read_embedded_style_map
from mammoth.conversion import convert_document_element_to_html
from mammoth.options import read_options
from mammoth.zips import open_zip
from .office_xml import read_str
from .....schema import BlockType

from .context import _DocxConstants
from ..image import is_valid_vector_image_payload, is_vector_image_part, serialize_office_image


@mammoth_images.img_element
def _convert_mammoth_table_image(image: Any) -> dict[str, str]:
    """Convert WMF/EMF in the table to an exportable image, and keep the original load of the ordinary bitmap."""
    with image.open() as stream:
        payload = stream.read()
    if is_vector_image_part(content_type=image.content_type) or is_valid_vector_image_payload(payload):
        source = serialize_office_image(payload, content_type=image.content_type)
    else:
        source = f"data:{image.content_type};base64,{base64.b64encode(payload).decode('ascii')}"
    return {"src": source}


class _DocxTables:
    """Maintain tables centrally, without creating your own documents or holding cross-document caches."""

    @staticmethod
    def _mammoth_top_level_table_document(document):
        """Only retain the top table node of the DOCX text to avoid Mammoth converting non-table text."""
        from mammoth import documents as _mammoth_documents

        return document.copy(children=[child for child in document.children if isinstance(child, _mammoth_documents.Table)])

    def _preparse_tables_with_mammoth(self, file_bytes: bytes) -> list:
        """
        Use mammoth to preparse HTML for all top-level tables in the full DOCX context.

        In isolated mode (only <w:tbl> XML fragment passed in), mammoth missing number definition
        (word/numbering.xml), style (word/styles.xml) and relationships
        (word/_rels/document.xml.rels) and other contexts, when encountering a list item or picture
        AttributeError is thrown for cells. Here let mammoth read the complete DOCX
        package context, but only converts top-level table nodes via transform_document to avoid
        Non-table text converted to huge HTML string.

        The image will be converted by mammoth to inline data-URI base64 format (<img src="data:...">).

        Note: mammoth does not support OMML, Equation XML or MTEF OLE formulas and will be silently discarded
        Formulas within table cells. After obtaining mammoth HTML, this method will synchronously traverse the original DOCX XML.
        Reinject the missing formula into the corresponding HTML cell.

        Returns:
            list[str | None]: A list of HTML aligned with the top-level table of the text; None represents the table
                No reliable Mammoth result found, follow the complete document context fallback analysis
        """
        try:
            import mammoth as _mammoth
            from bs4 import BeautifulSoup as _BeautifulSoup

            result = _mammoth.convert_to_html(
                BytesIO(file_bytes),
                transform_document=self._mammoth_top_level_table_document,
                convert_image=_convert_mammoth_table_image,
            )
            soup = _BeautifulSoup(result.value, "html.parser")

            # Keep only top-level tables, exclude subtables nested within other table cells
            all_tables = soup.find_all("table")
            top_level_tables = [t for t in all_tables if not t.find_parent("table")]

            # Synchronously load DOCX XML to obtain all top-level table elements for formula injection
            docx_obj = Document(BytesIO(file_bytes))
            xml_top_tables = [elem for elem in docx_obj.element.body if self._local_name(elem) == "tbl"]

            logger.debug(f"Pre-parsed {len(top_level_tables)} top-level tables via filtered mammoth conversion")

            result_tables = self._align_mammoth_tables_to_xml_tables(
                top_level_tables,
                xml_top_tables,
                docx_obj.part,
            )
            return result_tables
        except Exception as e:
            logger.debug(f"Could not pre-parse tables with filtered mammoth conversion: {e}")
            return []

    def _align_mammoth_tables_to_xml_tables(
        self,
        html_tables: Any,
        xml_tables: Any,
        source_part: Any,
    ) -> list:
        """
        Realign the Mammoth output table to the text top-level XML table.

        Some DOCX contain tables within text boxes, picture shapes, or compatible structures, Mammoth Complete
        During document conversion, these structural tables may also be output as top-level HTML table; but the text traversal
        _handle_tables will only be called on the real body/w:tbl. Here according to XML form
        Scan the Mammoth candidate table sequentially, skipping candidates that do not belong to the top table of the text to avoid subsequent
        _mammoth_table_idx Misalignment occurred during sequential consumption.
        """
        aligned_tables = []
        html_index = 0
        matched_count = 0

        for xml_table in xml_tables:
            matched_html_table = None
            scan_index = html_index
            while scan_index < len(html_tables):
                candidate = html_tables[scan_index]
                if self._mammoth_table_matches_xml_table(candidate, xml_table):
                    matched_html_table = candidate
                    html_index = scan_index + 1
                    matched_count += 1
                    break
                scan_index += 1

            if matched_html_table is None:
                aligned_tables.append(None)
                continue

            matched_html_table = self._inject_equations_into_table(
                matched_html_table,
                xml_table,
                source_part,
            )
            aligned_tables.append(str(matched_html_table))

        if len(html_tables) != len(xml_tables):
            logger.debug(f"Aligned {matched_count}/{len(xml_tables)} body tables from {len(html_tables)} mammoth tables")
        return aligned_tables

    @staticmethod
    def _mammoth_table_matches_xml_table(html_table, xml_table) -> bool:
        """
        Determine whether the Mammoth HTML table corresponds to the current text XML table.

        The text table preferentially compares the table text after removing the blanks to avoid image/text box tables that are both 1x1.
        Mistakenly occupy the position of the text table; use the structure and number of pictures to hide the table without text.
        """
        xml_signature = _DocxTables._xml_table_signature(xml_table)
        html_signature = _DocxTables._html_table_signature(html_table)

        if xml_signature["text"] or html_signature["text"]:
            if not _DocxTables._table_text_matches(xml_signature["text"], html_signature["text"]):
                return False
            return (
                xml_signature["cell_count"] == html_signature["cell_count"]
                or xml_signature["row_count"] == html_signature["row_count"]
            )

        return (
            xml_signature["row_count"] == html_signature["row_count"]
            and xml_signature["cell_count"] == html_signature["cell_count"]
            and xml_signature["image_count"] == html_signature["image_count"]
        )

    @staticmethod
    def _xml_table_char_fragment(node: Any) -> str:
        """Render character-level OOXML elements into table signature text according to Mammoth rules.

        Splicing only w:t will miss the non-breaking hyphen, soft hyphen and Symbol characters,
        This causes the XML signature to be inconsistent with the Mammoth HTML signature. Unmapped w:sym in
        Mammoth will be ignored, and an empty string will be returned here as well.
        """
        w_ns = _DocxConstants._BLIP_NAMESPACES["w"]
        if node.tag == f"{{{w_ns}}}t":
            return node.text or ""
        if node.tag == f"{{{w_ns}}}noBreakHyphen":
            # NON-BREAKING HYPHEN U+2011, consistent with HTML rendering of Mammoth.
            return "‑"
        if node.tag == f"{{{w_ns}}}softHyphen":
            # SOFT HYPHEN U+00AD, consistent with HTML rendering of Mammoth.
            return "­"
        if node.tag == f"{{{w_ns}}}sym":
            try:
                from mammoth.docx.dingbats import dingbats
            except ImportError:
                return ""

            font = node.get(f"{{{w_ns}}}font", "")
            char = node.get(f"{{{w_ns}}}char", "")
            try:
                code = dingbats.get((font, int(char, 16)))
                if code is None and re.match(r"^F0..", char):
                    code = dingbats.get((font, int(char[2:], 16)))
            except (TypeError, ValueError):
                return ""
            return chr(code) if code is not None else ""
        return ""

    @staticmethod
    def _xml_table_signature(xml_table) -> dict:
        """Extract the XML form lightweight signature consistent with the Mammoth rendering rules.

        Collect w:t and special character elements in document order, and only process WordprocessingML
        Namespace to exclude Mammoth OMML m:t formula text that is not rendered. vertically merged
        The continuation cell will be collapsed by Mammoth, so its entire subtree will not participate in the text signature.
        """
        w_ns = _DocxConstants._BLIP_NAMESPACES["w"]
        char_tags = (
            f"{{{w_ns}}}t",
            f"{{{w_ns}}}noBreakHyphen",
            f"{{{w_ns}}}softHyphen",
            f"{{{w_ns}}}sym",
        )
        continuation_nodes = set()
        for cell in xml_table.iter(f"{{{w_ns}}}tc"):
            properties = cell.find(f"{{{w_ns}}}tcPr")
            vmerge = properties.find(f"{{{w_ns}}}vMerge") if properties is not None else None
            if vmerge is None:
                continue
            # Mammoth Treat cells missing val or val=continue as continuation,
            # And discard the entire contents of the cell when calculating rowspan.
            if vmerge.get(f"{{{w_ns}}}val") in (None, "continue"):
                continuation_nodes.update(cell.iter())

        ignored_nodes = set(continuation_nodes)
        mc_ns = "http://schemas.openxmlformats.org/markup-compatibility/2006"
        for alternate in xml_table.iter(f"{{{mc_ns}}}AlternateContent"):
            # The Office XML reader for Mammoth only expands the Fallback branch.
            for branch in alternate:
                if branch.tag != f"{{{mc_ns}}}Fallback":
                    ignored_nodes.update(branch.iter())
        for simple_field in xml_table.iter(f"{{{w_ns}}}fldSimple"):
            # Mammoth does not parse simple fields, and its internal cached text does not participate in matching.
            ignored_nodes.update(simple_field.iter())

        wordml_ns = "http://schemas.microsoft.com/office/word/2010/wordml"
        for content_control in xml_table.iter(f"{{{w_ns}}}sdt"):
            properties = content_control.find(f"{{{w_ns}}}sdtPr")
            if properties is None or properties.find(f"{{{wordml_ns}}}checkbox") is None:
                continue
            content = content_control.find(f"{{{w_ns}}}sdtContent")
            if content is None:
                continue
            # Mammoth Replaces the first non-null Text node in the content control with a checkbox.
            first_text = next(
                (
                    node
                    for node in content.iter()
                    if node not in ignored_nodes and node.tag in char_tags and _DocxTables._xml_table_char_fragment(node)
                ),
                None,
            )
            if first_text is not None:
                ignored_nodes.add(first_text)

        text = "".join(
            _DocxTables._xml_table_char_fragment(node)
            for node in xml_table.iter()
            if node not in ignored_nodes and node.tag in char_tags
        )
        return {
            "row_count": len(xml_table.xpath('.//*[local-name()="tr"]')),
            "cell_count": len(xml_table.xpath('.//*[local-name()="tc"]')),
            "image_count": len(xml_table.xpath('.//*[local-name()="blip" or local-name()="imagedata"]')),
            "text": _DocxTables._normalize_table_match_text(text),
        }

    @staticmethod
    def _html_table_signature(html_table) -> dict:
        """Extract lightweight signature of HTML table, used to filter additional generated tables of Mammoth."""
        text_fragments = []
        for fragment in html_table.strings:
            anchor = fragment.find_parent("a")
            if anchor is not None and anchor.parent.name == "sup":
                marker = re.fullmatch(r"(footnote|endnote)-ref-(\d+)", anchor.get("id", ""))
                if marker and anchor.get("href") == f"#{marker.group(1)}-{marker.group(2)}":
                    # Only footnote references generated by Mammoth are ignored, leaving ordinary [1] in the text.
                    continue
            text_fragments.append(fragment.strip())
        return {
            "row_count": len(html_table.find_all("tr")),
            "cell_count": len(html_table.find_all(["td", "th"])),
            "image_count": len(html_table.find_all("img")),
            "text": _DocxTables._normalize_table_match_text("".join(text_fragments)),
        }

    @staticmethod
    def _normalize_table_match_text(text: str) -> str:
        """Unify table matching text to eliminate Word word splitting and Mammoth whitespace differences."""
        return re.sub(r"\s+", "", text or "")

    @staticmethod
    def _table_text_matches(xml_text: str, html_text: str) -> bool:
        """Compares whether the table text points to the same body table."""
        if not xml_text or not html_text:
            return False
        if xml_text == html_text:
            return True
        return xml_text.startswith(html_text) or html_text.startswith(xml_text)

    def _inject_equations_into_table(
        self,
        html_table: Any,
        xml_table: Any,
        source_part: Any,
    ) -> Any:
        """
        Inject the OMML/Equation XML/MTEF formula from the DOCX XML table into the mammoth HTML table.

        mammoth will silently discard OMML, Equation XML and MTEF OLE formulas, resulting in
        The table cell is empty in HTML. This method traverses the HTML table (BeautifulSoup object) in parallel
        and the XML table (lxml element), with the content containing formula placeholders for cells containing OMML formulas
        Replace the original empty content.

        Args:
            html_table: Tag object of BeautifulSoup, representing the <table> element generated by mammoth
            xml_table: Element object of lxml, representing the corresponding <w:tbl> element in the original DOCX

        Returns:
            BeautifulSoup Tag: <table> element after injecting formula (modify in place and return)
        """
        W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        # Quick check: does the table contain any formulas
        if not any(
            kind in _DocxConstants._FORMULA_TOKEN_KINDS for kind, _value in self._docx_formula_tokens(xml_table, source_part)
        ):
            return html_table

        from bs4 import BeautifulSoup

        html_rows = html_table.find_all("tr")
        xml_rows = xml_table.findall(f"{{{W_NS}}}tr")

        if len(html_rows) != len(xml_rows):
            logger.debug(f"Table row count mismatch when injecting equations: HTML {len(html_rows)} vs XML {len(xml_rows)}")
            return html_table

        for html_row, xml_row in zip(html_rows, xml_rows):
            html_cells = html_row.find_all(["td", "th"])
            xml_cells = xml_row.findall(f"{{{W_NS}}}tc")

            if len(html_cells) != len(xml_cells):
                continue

            for html_cell, xml_cell in zip(html_cells, xml_cells):
                if not any(
                    kind in _DocxConstants._FORMULA_TOKEN_KINDS
                    for kind, _value in self._docx_formula_tokens(
                        xml_cell,
                        source_part,
                    )
                ):
                    continue

                # This cell contains a formula, rebuild its HTML contents to preserve the formula
                new_content = self._build_cell_html_with_equations(
                    xml_cell,
                    source_part,
                )
                if new_content:
                    html_cell.clear()
                    new_soup = BeautifulSoup(new_content, "html.parser")
                    for child in list(new_soup.children):
                        html_cell.append(child)

        return html_table

    def _build_cell_html_with_equations(
        self,
        xml_cell: Any,
        source_part: Any,
    ) -> str:
        """
        Constructs a HTML content string for a table cell containing a OMML/Equation XML/MTEF formula.

        Iterate through the paragraphs in the cell and combine the normal text and OMML/Equation XML/MTEF formula
        Mixed together to produce a HTML clip consistent with the mammoth output style.

        Args:
            xml_cell: lxml Element, representing the <w:tc> element in DOCX

        Returns:
            str: HTML string of cell content, such as "<p>text<eq>latex</eq></p>";
                 If the cell is empty, returns an empty string
        """
        parts = []
        for child in xml_cell:
            child_tag = self._local_name(child)
            if child_tag is None:
                continue
            if child_tag == "p":
                para_html = self._build_paragraph_html_with_equations(
                    child,
                    source_part,
                )
                if para_html is not None:
                    parts.append(para_html)
            # Nested tables are not processed for the time being, and are handled by the outer logic.
        return "".join(parts)

    def _inject_equations_into_table_html(
        self,
        html: str,
        xml_table: Any,
        source_part: Any,
    ) -> str:
        """Wrap the isolated table HTML into Tag and reuse the unified formula injection logic."""

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        html_table = soup.find("table")
        if html_table is None:
            return html
        return str(
            self._inject_equations_into_table(
                html_table,
                xml_table,
                source_part,
            )
        )

    def _build_paragraph_html_with_equations(
        self,
        xml_para: Any,
        source_part: Any,
    ) -> Optional[str]:
        """
        Constructs a HTML string for a paragraph that may contain a OMML/Equation XML/MTEF formula.

        Use the same iteration logic as _handle_equations_in_text:
        - The text of ordinary <w:t> elements is collected directly
        - The <m:oMath> element is converted to LaTeX and wrapped as a formula placeholder <eq>...</eq>
        - <m:t> and other <t> elements under the math namespace are skipped because the tag contains "math".
          Avoid repeated extraction after oMath2Latex has processed the entire oMath subtree

        Args:
            xml_para: lxml Element, representing the <w:p> element in DOCX

        Returns:
            str | None: HTML string in the format "<p>...</p>"; returns None when the paragraph is empty
        """
        items: list[str] = []
        for token_kind, value in self._docx_formula_tokens(xml_para, source_part):
            if token_kind == "text":
                items.append(value)
            else:
                items.append(self.equation_bookends.format(EQ=value))

        if not items:
            return None
        return f"<p>{''.join(items)}</p>"

    def _close_mammoth_fallback_context(self) -> None:
        """Closes the ZIP resource held by the current document fallback parsing and clears its cache."""
        context = getattr(self, "_mammoth_fallback_context", None)
        self._mammoth_fallback_context = None
        if context is not None:
            context[0].close()

    def _mammoth_fallback_html(self, element: BaseOxmlElement) -> str:
        """Converts the current table within the style, numbering, relationships, and picture context of the original DOCX."""
        context = self._mammoth_fallback_context
        if context is None:
            file_bytes = self._fallback_docx_bytes
            if file_bytes is None:
                raise ValueError("DOCX fallback source is unavailable")
            resources = ExitStack()
            try:
                package = resources.enter_context(open_zip(BytesIO(file_bytes), "r"))
                paths = mammoth_docx._find_part_paths(package)
                read_part = mammoth_docx._part_with_body_reader(None, package, paths, False)
                embedded_style_map = read_embedded_style_map(BytesIO(file_bytes))
                conversion_options = read_options(
                    {"embedded_style_map": embedded_style_map, "convert_image": _convert_mammoth_table_image}
                ).value
                context = (resources, read_part, paths.main_document, conversion_options)
                self._mammoth_fallback_context = context
            except Exception:
                resources.close()
                raise

        _, read_part, main_part, conversion_options = context
        table = read_str(element.xml)

        def read_selected_table(_root: Any, body_reader: Any) -> Any:
            """Each time an independent reader is obtained, only the current XML table is parsed and the main document relationship is inherited."""
            return body_reader.read_all([table])

        result = read_part(main_part, read_selected_table)
        return convert_document_element_to_html(result.value[0], **conversion_options).value

    def _handle_tables(self, element: BaseOxmlElement) -> None:
        """Output tables in text order; use full DOCX context fallback if prematch fails."""
        table_index = self._mammoth_table_idx
        self._mammoth_table_idx += 1
        html = self._mammoth_tables_html[table_index] if table_index < len(self._mammoth_tables_html) else None
        stage = "full-context fallback"
        try:
            if html is None:
                html = self._mammoth_fallback_html(element)
            stage = "colspan normalization"
            html = self._normalize_table_colspans(html)
            if table_index >= len(self._mammoth_tables_html) or self._mammoth_tables_html[table_index] is None:
                stage = "equation injection"
                html = self._inject_equations_into_table_html(
                    html,
                    element,
                    self._require_document_part(),
                )
        except Exception as exc:
            raise RuntimeError(f"table #{table_index + 1}, {stage}: {exc}") from exc
        self.cur_page.append({"type": BlockType.TABLE, "content": html})

    def _normalize_table_colspans(self, html: str) -> str:
        """
        Fixed the colspan inconsistency issue caused by wireless table/less wire table in HTML table.

        In a borderless or borderless DOCX table, some rows have cells that contain w:gridSpan values.
        This value comes from the Word internal virtual grid and does not reflect the actual visual column number. mammoth will these
        The w:gridSpan value is converted directly to the HTML colspan attribute, resulting in a different number of effective columns for the row
        (The sum of all colspan) is inconsistent, resulting in misalignment of rows and columns.

        This method detects such inconsistencies and reduces the colspan for rows with too many valid columns to
        The most common target column number, thereby restoring the correct structure of the table.

        algorithm:
        1. Calculate the effective number of columns in each row (sum of all cells colspan in the row)
        2. Take the most common number of columns as the target number of columns
        3. For rows where the number of effective columns exceeds the target value, reduce starting from the first cell with colspan > 1

        Args:
            html: HTML string containing table

        Returns:
            str: Corrected HTML string
        """
        try:
            from collections import Counter

            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")
            modified = False

            for table in tables:
                rows = table.find_all("tr")
                if not rows:
                    continue

                # If there are cells in the table with rowspan > 1, the sum of explicit colspan in each row
                # Does not reflect true grid width (column occupied by rowspan does not appear in subsequent rows of td
                # list), the assumption of the algorithm does not hold at this time, skip this table to avoid accidentally modifying the legal
                # colspan。
                all_cells = table.find_all(["td", "th"])
                if any(int(c.get("rowspan", 1)) > 1 for c in all_cells):
                    continue

                # Calculate the effective number of columns per row (sum of colspan for all cells)
                row_col_counts = []
                for row in rows:
                    cells = row.find_all(["td", "th"])
                    total = sum(int(c.get("colspan", 1)) for c in cells)
                    row_col_counts.append(total)

                if not row_col_counts:
                    continue

                # Find the target column number (the column number that appears the most)
                count_freq = Counter(row_col_counts)
                if len(count_freq) == 1:
                    continue  # The numbers of rows and columns are consistent and no correction is needed.

                target = count_freq.most_common(1)[0][0]

                # Fix rows where the number of valid columns exceeds the target value: shrink cells with colspan > 1
                for row, col_count in zip(rows, row_col_counts):
                    if col_count <= target:
                        continue

                    excess = col_count - target
                    cells = row.find_all(["td", "th"])

                    for cell in cells:
                        if excess <= 0:
                            break
                        span = int(cell.get("colspan", 1))
                        if span > 1:
                            reduce_by = min(span - 1, excess)
                            new_span = span - reduce_by
                            if new_span == 1:
                                if "colspan" in cell.attrs:
                                    del cell["colspan"]
                            else:
                                cell["colspan"] = str(new_span)
                            excess -= reduce_by
                            modified = True

            if modified:
                return str(soup)
            return html
        except Exception as e:
            logger.debug(f"Failed to normalize table colspans: {e}")
            return html
