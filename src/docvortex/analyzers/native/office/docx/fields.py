"""DOCX field and catalog handling; sharing the current single-document status of Converter."""

import re
from pathlib import Path
from typing import Iterator, Optional, Union
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.text.hyperlink import Hyperlink
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from loguru import logger
from .....schema import BlockType
from .formatting_types import Formatting

from .context import _DocxConstants, _DocxComplexFieldFrame, _ParagraphElement, _ParagraphHyperlink


class _DocxFields:
    """Maintain fields and directories centrally, without creating documents yourself or holding cross-document caches."""

    def _collect_toc_anchor_set(self) -> set[str]:
        """TOC bookmark goal of collecting entire documents from real hyperlinks and complex domains."""
        anchor_attr = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}anchor"
        anchors: set[str] = set()
        for hl in self.docx_obj.element.body.findall(".//w:hyperlink", namespaces=_DocxConstants._BLIP_NAMESPACES):
            anchor = hl.get(anchor_attr, "").strip()
            if anchor and anchor.startswith("_Toc"):
                anchors.add(anchor)
        for paragraph in self.docx_obj.element.body.findall(
            ".//w:p",
            namespaces=_DocxConstants._BLIP_NAMESPACES,
        ):
            for instruction in self._complex_field_instructions(paragraph):
                target, is_internal = self._complex_field_hyperlink_target(instruction)
                anchor = target.removeprefix("#") if target and is_internal else ""
                if anchor.startswith("_Toc"):
                    anchors.add(anchor)
        return anchors

    @classmethod
    def _paragraph_bookmark_names(
        cls,
        paragraph_element: BaseOxmlElement,
    ) -> list[str]:
        """Returns the publicly available bookmark names within a paragraph in document order, excluding Word navigation tags."""

        bookmark_name_attr = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}name"
        names: list[str] = []
        for bookmark in paragraph_element.findall(
            ".//w:bookmarkStart",
            namespaces=cls._BLIP_NAMESPACES,
        ):
            name = bookmark.get(bookmark_name_attr, "").strip()
            if name and not name.startswith("_GoBack"):
                names.append(name)
        return names

    def _collect_toc_anchor_aliases(
        self,
        referenced_anchors: set[str],
    ) -> dict[str, str]:
        """Converg multiple TOC bookmark of the same paragraph into one Middle JSON canonical anchor."""

        aliases: dict[str, str] = {}
        for paragraph in self.docx_obj.element.body.findall(
            ".//w:p",
            namespaces=_DocxConstants._BLIP_NAMESPACES,
        ):
            toc_names = [name for name in self._paragraph_bookmark_names(paragraph) if name.startswith("_Toc")]
            if not toc_names:
                continue
            referenced_names = [name for name in toc_names if name in referenced_anchors]
            canonical = referenced_names[0] if referenced_names else toc_names[0]
            for name in toc_names:
                aliases.setdefault(name, canonical)
        return aliases

    def _canonical_toc_anchor(self, anchor: str) -> str:
        """Returns the only public anchor corresponding to bookmark alias, and the unknown name remains as it is."""

        return self.toc_anchor_aliases.get(anchor, anchor)

    @staticmethod
    def _complex_field_hyperlink_target(instruction: str) -> tuple[str | None, bool]:
        """Extract external URL or internal bookmark fragment from complex field instructions."""
        external_match = re.search(r'\bHYPERLINK\s+"([^"]+)"', instruction, re.IGNORECASE)
        bookmark_match = re.search(r'\\l\s+"([^"]+)"', instruction, re.IGNORECASE)
        address = external_match.group(1).strip() if external_match else ""
        bookmark = bookmark_match.group(1).strip() if bookmark_match else ""
        if address:
            return (f"{address}#{bookmark}" if bookmark else address), False
        if bookmark:
            return f"#{bookmark}", True
        return None, False

    @classmethod
    def _complex_field_instructions(
        cls,
        paragraph_element: BaseOxmlElement,
    ) -> list[str]:
        """Merge complex field directives split in paragraphs by field boundaries and nesting order."""

        word_namespace = cls._BLIP_NAMESPACES["w"]
        field_char_tag = f"{{{word_namespace}}}fldChar"
        instruction_tag = f"{{{word_namespace}}}instrText"
        field_type_attr = f"{{{word_namespace}}}fldCharType"
        field_stack: list[_DocxComplexFieldFrame] = []
        instructions: list[str] = []

        def append_instruction(frame: _DocxComplexFieldFrame) -> None:
            """Appends a field of accumulated non-null instructions to the output."""

            instruction = "".join(frame.instruction_parts).strip()
            if instruction:
                instructions.append(instruction)

        for element in paragraph_element.iter():
            if element.tag == field_char_tag:
                field_type = element.get(field_type_attr)
                if field_type == "begin":
                    field_stack.append(_DocxComplexFieldFrame())
                elif field_type == "separate" and field_stack:
                    frame = field_stack[-1]
                    if frame.phase == "instr":
                        append_instruction(frame)
                        frame.phase = "result"
                elif field_type == "end" and field_stack:
                    frame = field_stack.pop()
                    if frame.phase == "instr":
                        append_instruction(frame)
                continue
            if element.tag != instruction_tag:
                continue
            text = element.text or ""
            if field_stack and field_stack[-1].phase == "instr":
                field_stack[-1].instruction_parts.append(text)
            elif text.strip():
                # Compatible with non-canonical instructions that lack the fldChar package but used to be parsed node by node.
                instructions.append(text.strip())

        for frame in field_stack:
            if frame.phase == "instr":
                append_instruction(frame)
        return instructions

    @staticmethod
    def _python_docx_hyperlink_target(hyperlink: Hyperlink) -> _ParagraphHyperlink:
        """Converts the address of python-docx Hyperlink or fragment to an inline target."""
        address = hyperlink.address
        fragment = hyperlink.fragment
        if address and fragment:
            return f"{address}#{fragment}"
        if address and "://" in address:
            return address
        if address:
            return Path(address)
        if fragment:
            return f"#{fragment}"
        return Path(".")

    def _resolve_complex_field_elements(
        self,
        frame: _DocxComplexFieldFrame,
        *,
        suppress_internal_links: bool,
    ) -> list[_ParagraphElement]:
        """Closes a complex field and binds the field result to the parsed hyperlink target."""
        target, is_internal = self._complex_field_hyperlink_target("".join(frame.instruction_parts))
        if target is None or (is_internal and suppress_internal_links):
            return frame.result_elements
        return [(text, format_obj, existing_target or target) for text, format_obj, existing_target in frame.result_elements]

    def _flatten_paragraph_elements(
        self,
        paragraph: Paragraph,
        inner_contents: list[Union[Run, Hyperlink]],
    ) -> list[_ParagraphElement]:
        """Expand ordinary run, real hyperlinks and nestable complex fields in document order."""
        elements: list[_ParagraphElement] = []
        field_stack: list[_DocxComplexFieldFrame] = []
        suppress_internal_links = self._get_toc_item_level(paragraph) is not None
        word_namespace = _DocxConstants._BLIP_NAMESPACES["w"]

        for content_index, content in enumerate(inner_contents):
            if isinstance(content, Hyperlink):
                hyperlink_target = self._python_docx_hyperlink_target(content)
                if suppress_internal_links and isinstance(hyperlink_target, str) and hyperlink_target.startswith("#"):
                    hyperlink_target = None
                hyperlink_elements: list[_ParagraphElement] = []
                for hyperlink_run in content.runs:
                    if self._is_hidden_run(hyperlink_run):
                        continue
                    text = hyperlink_run.text or ""
                    format_obj = self._normalize_format_for_text(
                        self._get_format_from_run(hyperlink_run),
                        text,
                        preserve_blank_non_visible_style=True,
                    )
                    if text != "" or self._has_visible_style(format_obj):
                        hyperlink_elements.append((text, format_obj, hyperlink_target))
                if field_stack and field_stack[-1].phase == "result":
                    field_stack[-1].result_elements.extend(hyperlink_elements)
                else:
                    elements.extend(hyperlink_elements)
                continue

            if not isinstance(content, Run):
                continue

            field_char = content._element.find(f"{{{word_namespace}}}fldChar")
            if field_char is not None:
                field_type = field_char.get(f"{{{word_namespace}}}fldCharType")
                if field_type == "begin":
                    field_stack.append(_DocxComplexFieldFrame())
                elif field_type == "separate" and field_stack:
                    field_stack[-1].phase = "result"
                elif field_type == "end" and field_stack:
                    frame = field_stack.pop()
                    resolved = self._resolve_complex_field_elements(
                        frame,
                        suppress_internal_links=suppress_internal_links,
                    )
                    if field_stack and field_stack[-1].phase == "result":
                        field_stack[-1].result_elements.extend(resolved)
                    else:
                        elements.extend(resolved)
                continue

            instruction = content._element.find(f"{{{word_namespace}}}instrText")
            if instruction is not None and field_stack and field_stack[-1].phase == "instr":
                if instruction.text:
                    field_stack[-1].instruction_parts.append(instruction.text)
                continue

            text = content.text or ""
            raw_format = self._get_format_from_run(content)
            preserve_blank_non_visible_style = self._should_preserve_blank_non_visible_style(
                inner_contents,
                content_index,
                text,
                raw_format,
            )
            format_obj = self._normalize_format_for_text(
                raw_format,
                text,
                preserve_blank_non_visible_style=preserve_blank_non_visible_style,
            )
            element = (text, format_obj, None)
            if field_stack:
                if field_stack[-1].phase == "result":
                    field_stack[-1].result_elements.append(element)
                continue
            elements.append(element)

        while field_stack:
            frame = field_stack.pop()
            resolved = self._resolve_complex_field_elements(
                frame,
                suppress_internal_links=suppress_internal_links,
            )
            if field_stack and field_stack[-1].phase == "result":
                field_stack[-1].result_elements.extend(resolved)
            else:
                elements.extend(resolved)
        return elements

    def _get_paragraph_elements(self, paragraph: Paragraph) -> list[_ParagraphElement]:
        """
        Extract paragraph elements and their formatting and hyperlink information.

        Args:
            paragraph: paragraph object

        Returns:
            list[_ParagraphElement]:
            A list of paragraph elements, each containing text, formatting, and hyperlink information
        """

        inner_contents = list(self._iter_paragraph_inner_content(paragraph))
        paragraph_text = self._get_paragraph_text_from_contents(inner_contents)

        # Empty paragraphs are currently reserved for backward compatibility:
        if paragraph_text.strip() == "":
            # Check for the presence of blank text with visible styling (underline or strikethrough) run.
            # White text with visible styling (such as underlined spaces) is visually visible and should be preserved.
            # Therefore, the early return is skipped and handed over to the subsequent complete run processing flow.
            has_visible_style_run = any(
                isinstance(c, Run) and c.text and self._has_visible_style(self._get_format_from_run(c)) for c in inner_contents
            )
            if not has_visible_style_run:
                return [("", None, None)]

        paragraph_elements: list[_ParagraphElement] = []
        group_text = ""
        previous_format: Optional[Formatting] = None

        # Iterates through the expanded normal run, hyperlink and complex field results, grouped by format.
        flattened_elements = self._flatten_paragraph_elements(paragraph, inner_contents)
        for text, format_obj, hyperlink in flattened_elements:
            # Grouping is triggered when the new run has visible content (not empty or blank with visible style) and the format changes
            has_visible_content = len(text.strip()) > 0 or self._has_visible_style(format_obj)
            is_blank_text = bool(text) and not text.strip()
            format_changed = format_obj != previous_format
            has_visible_boundary = self._has_visible_style(previous_format) or self._has_visible_style(format_obj)
            should_split_blank_boundary = is_blank_text and bool(group_text) and format_changed and has_visible_boundary
            if (has_visible_content and format_changed) or should_split_blank_boundary or (hyperlink is not None):
                # Save only when the previous group has substantial content (not empty or whitespace with visible styles)
                preserve_plain_blank = (
                    bool(group_text)
                    and not group_text.strip()
                    and (self._has_visible_style(previous_format) or self._has_visible_style(format_obj))
                )
                prev_has_visible = self._should_keep_group_text(
                    group_text,
                    previous_format,
                    preserve_plain_blank=preserve_plain_blank,
                )
                if prev_has_visible:
                    paragraph_elements.append((group_text, previous_format, None))
                group_text = ""

                # If there is a hyperlink, add it now
                if hyperlink is not None:
                    self._append_paragraph_element(paragraph_elements, text, format_obj, hyperlink)
                    text = ""
                else:
                    previous_format = format_obj

            group_text += text

        # Format last group
        # NOTE: Use previous_format (the format of the current accumulation group), not format (the format of the last loop iteration).
        # The last iteration may be an empty run with no style, using format will cause the style to be lost.
        last_has_visible = self._should_keep_group_text(
            group_text,
            previous_format,
        )
        if last_has_visible:
            paragraph_elements.append((group_text, previous_format, None))

        return self._normalize_hyperlink_group_boundaries(paragraph_elements)

    def _iter_paragraph_inner_content(
        self,
        paragraph: Paragraph,
        container: Optional[BaseOxmlElement] = None,
    ) -> Iterator[Union[Run, Hyperlink]]:
        """Yield visible paragraph inline containers in document order.

        python-docx only walks direct ``w:r`` and ``w:hyperlink`` children of ``w:p``.
        Inline ``w:sdt`` content controls are skipped entirely, which drops their text
        from both ``paragraph.text`` and ``paragraph.iter_inner_content()``. This walker
        treats ``w:sdt`` and a few transparent wrapper nodes as pass-through containers
        and reuses the existing Run/Hyperlink wrappers for the actual visible content.
        """
        if container is None:
            container = paragraph._element

        _W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

        for child in container:
            tag_name = self._local_name(child)
            if tag_name is None:
                continue

            if tag_name == "r":
                yield Run(child, paragraph)
            elif tag_name == "hyperlink":
                yield Hyperlink(child, paragraph)
            elif tag_name == "sdt":
                sdt_content = child.find(f"{{{_W_NS}}}sdtContent")
                if sdt_content is not None:
                    yield from self._iter_paragraph_inner_content(paragraph, sdt_content)
            elif tag_name in self._PARAGRAPH_TRANSPARENT_INLINE_CONTAINERS:
                yield from self._iter_paragraph_inner_content(paragraph, child)

    def _is_toc_sdt(self, element: BaseOxmlElement) -> bool:
        """
        Check whether the SDT element is a directory (Table of Contents).

        Detection strategy:
        1. Check the docPartGallery or tag element in w:sdtPr
        2. Fall back to check whether the paragraph style in the content is in the "TOC N" format

        Args:
            element: SDT XML element

        Returns:
            bool: If it is a directory SDT returns True, otherwise returns False
        """
        # Method 1: Check docPartGallery in w:sdtPr
        sdt_pr = element.find("w:sdtPr", namespaces=_DocxConstants._BLIP_NAMESPACES)
        if sdt_pr is not None:
            doc_part_gallery = sdt_pr.find(".//w:docPartGallery", namespaces=_DocxConstants._BLIP_NAMESPACES)
            if doc_part_gallery is not None:
                val = doc_part_gallery.get(self.XML_KEY, "")
                if "Table of Contents" in val or "toc" in val.lower():
                    return True

            # Check the value of the tag element
            tag_elem = sdt_pr.find("w:tag", namespaces=_DocxConstants._BLIP_NAMESPACES)
            if tag_elem is not None:
                val = tag_elem.get(self.XML_KEY, "").lower().replace(" ", "")
                if "toc" in val or "contents" in val or "tableofcontents" in val:
                    return True

        # Method 2: Check whether the style of the content paragraph is in the "TOC N" format
        sdt_content = element.find("w:sdtContent", namespaces=_DocxConstants._BLIP_NAMESPACES)
        if sdt_content is not None:
            paragraphs = sdt_content.findall("w:p", namespaces=_DocxConstants._BLIP_NAMESPACES)
            for p in paragraphs[:5]:  # Just check the first 5 paragraphs to tell
                try:
                    p_obj = Paragraph(p, self.docx_obj)
                    paragraph_style = self._get_paragraph_style(p_obj)
                    if paragraph_style and paragraph_style.name:
                        style_name = paragraph_style.name
                        if re.match(r"^TOC\s*\d+$", style_name, re.IGNORECASE) or re.match(r"^目录\s*\d+$", style_name):
                            return True
                except Exception:
                    continue

        return False

    def _get_toc_item_level(self, paragraph: Paragraph) -> Optional[int]:
        """
        Get the level of table of contents items from the paragraph style (0-based).

        "TOC 1" -> 0
        "TOC 2" -> 1
        "Directory 1" -> 0

        Args:
            paragraph: paragraph object

        Returns:
            Optional[int]: Level (0-based), if it is not a directory style, return None
        """
        paragraph_style = self._get_paragraph_style(paragraph)
        if paragraph_style is None:
            return None
        style_name = paragraph_style.name
        if style_name:
            match = re.match(r"^(?:TOC|目录)\s*(\d+)$", style_name, re.IGNORECASE)
            if match:
                level = int(match.group(1))
                return level - 1  # Convert to 0-based
        return None

    def _is_flat_list_toc(self, items: list[tuple[int, str, list, list, Optional[str]]]) -> bool:
        """
        Check if the directory is a flat list (illustration list, list of lists, etc.),
        All entries in such directories should be at the same level and should not be nested.

        Strategy: Check if more than 50% of entries start with "Figure" or "Table".
        """
        match_count = 0
        total_count = 0
        for _level, text, _elements, _equations, _anchor in items:
            stripped = text.strip()
            if not stripped:
                continue
            total_count += 1
            if re.match(r"^[图表][\d\s.]", stripped) or re.match(r"^(Figure|Table)\s+\d", stripped, re.IGNORECASE):
                match_count += 1
        if total_count == 0:
            return False
        return match_count / total_count > 0.5

    def _correct_toc_level_by_text(self, toc_level: int, text: str) -> int:
        """
        Correct the hierarchy of table of contents entries by numbering depth in the text.

        Only entries with toc_level > 0 are corrected to avoid affecting top-level chapter titles.
        For example:
        - "1.1 LYSO..." (toc 3 → ilevel=2) → text depth 2 → return 1
        - "1.1.1 LYSO..." (toc 3 → ilevel=2) → text depth 3 → return 2
        - "Summary of this chapter" (toc 1 → ilevel=0) → return 0 (no correction)
        """
        if toc_level == 0:
            return 0
        stripped = text.strip()
        match = re.match(r"^(\d+(?:\.\d+)+)(?![\d.])", stripped)
        if match:
            parts = match.group(1).split(".")
            # Only clear multi-level chapter numbers are used to lighten the unusually dark TOC style to prevent ordinary list numbers from being upgraded.
            text_level = len(parts) - 1
            if text_level < toc_level:
                return text_level
        return toc_level

    def _add_index_item(
        self,
        *,
        ilevel: int,
        elements: list,
        text: str = "",
        equations: list = None,
        anchor: Optional[str] = None,
    ) -> None:
        """
        Add directory entries to index blocks.

        Generated index structure:
        {
            "type": "index",
            "ilevel": 0,
            "content": [
                {"type": "text", "content": "Directory entry text"},
                {"type": "index", "ilevel": 1, "content": [...]},
            ]
        }

        Args:
            ilevel: Indentation level (0-based)
            elements: element list
            text: Processed text (contains formula marks)
            equations: Formula list
        """
        if equations is None:
            equations = []
        if not elements:
            return

        content_text = self._build_text_with_equations_and_hyperlinks(elements, text, equations)
        content_text = self._normalize_text_block_content(content_text)
        if not content_text:
            return

        # Case 1: First directory entry, creating new top-level index block
        if self.pre_index_ilevel == -1:
            index_block = {
                "type": BlockType.INDEX,
                "content": [],
                "ilevel": ilevel,
            }
            self.cur_page.append(index_block)
            self.index_block_stack.append(index_block)

            index_item = {
                "type": BlockType.TEXT,
                "content": content_text,
            }
            if anchor:
                index_item["anchor"] = anchor
            index_block["content"].append(index_item)
            self.pre_index_ilevel = ilevel

        # Case 2: Increase indentation, open subindex block
        elif self.pre_index_ilevel < ilevel:
            if not self.index_block_stack:
                # Defense exception TOC status: When the stack is empty, restore according to the new directory block to avoid a single bad level blocking parsing.
                logger.debug(
                    "Recovering DOCX index stack before adding TOC item at level {}",
                    ilevel,
                )
                self.pre_index_ilevel = -1
                self._add_index_item(
                    ilevel=ilevel,
                    elements=elements,
                    text=text,
                    equations=equations,
                    anchor=anchor,
                )
                return

            child_index_block = {
                "type": BlockType.INDEX,
                "content": [],
                "ilevel": ilevel,
            }
            parent_index_block = self.index_block_stack[-1]
            parent_index_block["content"].append(child_index_block)
            self.index_block_stack.append(child_index_block)

            index_item = {
                "type": BlockType.TEXT,
                "content": content_text,
            }
            if anchor:
                index_item["anchor"] = anchor
            child_index_block["content"].append(index_item)
            self.pre_index_ilevel = ilevel

        # Case 3: Reduce indentation, close subindex block
        elif ilevel < self.pre_index_ilevel:
            while self.index_block_stack:
                top_block = self.index_block_stack[-1]
                if top_block["ilevel"] == ilevel:
                    break
                self.index_block_stack.pop()
            if self.index_block_stack:
                index_block = self.index_block_stack[-1]
                index_item = {
                    "type": BlockType.TEXT,
                    "content": content_text,
                }
                if anchor:
                    index_item["anchor"] = anchor
                index_block["content"].append(index_item)
            self.pre_index_ilevel = ilevel

        # Case 4: Sibling directory entries
        else:
            if self.index_block_stack:
                index_block = self.index_block_stack[-1]
                index_item = {
                    "type": BlockType.TEXT,
                    "content": content_text,
                }
                if anchor:
                    index_item["anchor"] = anchor
                index_block["content"].append(index_item)

    def _extract_paragraph_bookmark(self, paragraph_element: BaseOxmlElement) -> Optional[str]:
        """Extract a bookmark name from a paragraph, prioritizing TOC bookmarks."""
        names = self._paragraph_bookmark_names(paragraph_element)
        if not names:
            return None
        toc_names = [name for name in names if name.startswith("_Toc")]
        if toc_names:
            # Prefer anchors that are actually referenced by TOC hyperlinks.
            for name in toc_names:
                if name in self.toc_anchor_set:
                    return self._canonical_toc_anchor(name)
            return self._canonical_toc_anchor(toc_names[0])
        return names[0]

    def _extract_toc_target_anchor(self, paragraph_element: BaseOxmlElement) -> Optional[str]:
        """Extract the internal bookmark of a TOC paragraph from a real hyperlink or complex domain."""
        anchor_attr = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}anchor"
        anchors = []
        for hl in paragraph_element.findall(".//w:hyperlink", namespaces=_DocxConstants._BLIP_NAMESPACES):
            anchor = hl.get(anchor_attr, "").strip()
            if anchor:
                anchors.append(anchor)
        for anchor in anchors:
            if anchor.startswith("_Toc"):
                return self._canonical_toc_anchor(anchor)
        if anchors:
            return anchors[0]

        field_anchors: list[str] = []
        for instruction in self._complex_field_instructions(
            paragraph_element,
        ):
            target, is_internal = self._complex_field_hyperlink_target(instruction)
            anchor = target.removeprefix("#") if target and is_internal else ""
            if anchor:
                field_anchors.append(anchor)
        for anchor in field_anchors:
            if anchor.startswith("_Toc"):
                return self._canonical_toc_anchor(anchor)
        return field_anchors[0] if field_anchors else None

    def _handle_plain_toc_paragraph_as_index(
        self,
        *,
        paragraph: Paragraph,
        paragraph_element: BaseOxmlElement,
        paragraph_elements: list,
        text: str,
        equations: list,
    ) -> bool:
        """Convert ordinary table of contents paragraphs not wrapped in SDT to INDEX items."""
        toc_level = self._get_toc_item_level(paragraph)
        if toc_level is None:
            return False
        if not text:
            return True

        target_anchor = self._extract_toc_target_anchor(paragraph_element)
        # Only allow unanchored entries after entering the table of contents sequence to avoid accidentally reusing TOC style cover text.
        if not target_anchor and self.pre_index_ilevel == -1:
            return False
        if target_anchor and target_anchor.startswith("_Toc"):
            self.toc_anchor_set.add(target_anchor)

        if self.plain_toc_base_level is None:
            self.plain_toc_base_level = toc_level
        normalized_level = max(0, toc_level - self.plain_toc_base_level)
        corrected_level = self._correct_toc_level_by_text(normalized_level, text)
        self._add_index_item(
            ilevel=corrected_level,
            elements=paragraph_elements,
            text=text,
            equations=equations,
            anchor=target_anchor,
        )
        return True

    def _handle_sdt_as_index(self, sdt_content: BaseOxmlElement) -> None:
        """
        Process the contents of directory SDT and convert it into hierarchical INDEX blocks.

        Two-stage processing:
        1. Collect all paragraphs and their levels;
        2. Detect the directory type (regular directory vs flat list), correct the hierarchy and write the index block.

        Args:
            sdt_content: w:sdtContent XML element
        """
        paragraphs = sdt_content.findall(".//w:p", namespaces=_DocxConstants._BLIP_NAMESPACES)

        # --- Phase 1: Collect all entries ---
        toc_items: list[tuple[int, str, list, list, Optional[str]]] = []
        for p in paragraphs:
            try:
                p_obj = Paragraph(p, self.docx_obj)
                paragraph_elements = self._get_paragraph_elements(p_obj)
                text, equations = self._handle_equations_in_text(
                    element=p,
                    text=p_obj.text,
                    part=p_obj.part,
                )
                target_anchor = self._extract_toc_target_anchor(p)
                if target_anchor and target_anchor.startswith("_Toc"):
                    self.toc_anchor_set.add(target_anchor)
                if text is None:
                    continue
                text = text.strip()
                if not text:
                    continue

                toc_level = self._get_toc_item_level(p_obj)
                if toc_level is None:
                    toc_level = 0

                toc_items.append((toc_level, text, paragraph_elements, equations, target_anchor))
            except Exception as e:
                logger.debug(f"Error collecting TOC paragraph: {e}")
                continue

        # --- Phase 2: Correct the hierarchy and write the index block ---
        is_flat = self._is_flat_list_toc(toc_items)

        # Reset the index state and start a new directory block
        self._reset_index_state()

        for toc_level, text, elements, equations, target_anchor in toc_items:
            if is_flat:
                # Illustrations/lists: force all flattened (level 0)
                corrected_level = 0
            else:
                # General directory: Correct levels based on text number depth to solve docx level skipping problem
                corrected_level = self._correct_toc_level_by_text(toc_level, text)

            self._add_index_item(
                ilevel=corrected_level,
                elements=elements,
                text=text,
                equations=equations,
                anchor=target_anchor,
            )

        # Reset index status after processing is complete
        self._reset_index_state()

    def _get_heading_and_level(self, style_label: str) -> tuple[str, Optional[int]]:
        """
        Get title and hierarchy from style tag.

        Args:
            style_label: Style tag

        Returns:
            tuple[str, Optional[int]]: (tag string, level) tuple
        """
        parts = self._split_text_and_number(style_label)

        if len(parts) == 2:
            parts.sort()
            label_str: str = ""
            label_level: Optional[int] = 0
            if parts[0].strip().lower() == "heading":
                label_str = "Heading"
                label_level = self._str_to_int(parts[1], None)
            if parts[1].strip().lower() == "heading":
                label_str = "Heading"
                label_level = self._str_to_int(parts[0], None)
            return label_str, label_level

        return style_label, None

    def _split_text_and_number(self, input_string: str) -> list[str]:
        """
        Split the text and numeric parts of a string.

        Args:
            input_string: input string

        Returns:
            list[str]: Split partial list
        """
        match = re.match(r"(\D+)(\d+)$|^(\d+)(\D+)", input_string)
        if match:
            parts = list(filter(None, match.groups()))
            return parts
        else:
            return [input_string]

    def _str_to_int(self, s: Optional[str], default: Optional[int] = 0) -> Optional[int]:
        """
        Convert string to integer.

        Args:
            s: String to convert
            default: Default value, returned when conversion fails

        Returns:
            Optional[int]: converted integer, returning to the default value when the conversion fails
        """
        if s is None:
            return None
        try:
            return int(s)
        except ValueError:
            return default
