"""PPTX list tag and number, reuse the single document status of the current converter."""

from typing import Optional
from lxml import etree
from pptx.enum.shapes import PP_PLACEHOLDER
from .....schema import BlockType


class _PptxLists:
    """Centrally maintain list tags and numbers without changing the document life cycle and public access."""

    def _get_paragraph_list_info(self, shape, paragraph) -> dict:
        """Paragraph list properties are parsed based on the Paragraph->TextBox->Layout->Master inheritance chain."""
        marker_info = self._get_effective_list_marker(shape, paragraph)
        p = paragraph._element
        level = marker_info.get("level", self._get_paragraph_level(p))
        kind = marker_info.get("kind")

        if marker_info.get("is_list") is False:
            return {
                "is_list": False,
                "attribute": "unordered",
                "level": level,
                "kind": kind,
                "start": None,
                "start_is_explicit_restart": False,
            }

        if kind == "buAutoNum":
            return {
                "is_list": True,
                "attribute": "ordered",
                "level": level,
                "kind": kind,
                "start": marker_info.get("start"),
                "start_is_explicit_restart": marker_info.get(
                    "start_is_explicit_restart",
                    False,
                ),
            }

        if kind in ("buChar", "buBlip"):
            return {
                "is_list": True,
                "attribute": "unordered",
                "level": level,
                "kind": kind,
                "start": None,
                "start_is_explicit_restart": False,
            }

        if marker_info.get("is_list") is True:
            return {
                "is_list": True,
                "attribute": "unordered",
                "level": level,
                "kind": kind,
                "start": None,
                "start_is_explicit_restart": False,
            }

        # Bottom line: paragraph-level markup + indentation level judgment
        bu_auto_num = p.find(".//a:buAutoNum", namespaces={"a": self.namespaces["a"]})
        if bu_auto_num is not None:
            start = self._parse_positive_int(bu_auto_num.get("startAt"))
            return {
                "is_list": True,
                "attribute": "ordered",
                "level": paragraph.level,
                "kind": "buAutoNum",
                "start": start,
                "start_is_explicit_restart": start is not None,
            }

        if p.find(".//a:buChar", namespaces={"a": self.namespaces["a"]}) is not None:
            return {
                "is_list": True,
                "attribute": "unordered",
                "level": paragraph.level,
                "kind": "buChar",
                "start": None,
                "start_is_explicit_restart": False,
            }

        if paragraph.level > 0:
            return {
                "is_list": True,
                "attribute": "unordered",
                "level": paragraph.level,
                "kind": None,
                "start": None,
                "start_is_explicit_restart": False,
            }

        return {
            "is_list": False,
            "attribute": "unordered",
            "level": 0,
            "kind": None,
            "start": None,
            "start_is_explicit_restart": False,
        }

    def _ensure_list_level(
        self,
        list_stack: list,
        level: int,
        attribute: str,
        start: Optional[int] = None,
        start_is_explicit_restart: bool = False,
    ):
        """Adjust the list stack to the target level and create sibling/child list blocks if necessary."""
        while len(list_stack) > level + 1:
            list_stack.pop()

        if len(list_stack) == level + 1 and list_stack[level].get("attribute") != attribute:
            list_stack.pop()

        if self._should_restart_ordered_list(
            list_stack,
            level,
            attribute,
            start,
            start_is_explicit_restart,
        ):
            list_stack.pop()

        while len(list_stack) < level + 1:
            ilevel = len(list_stack)
            new_list_block = {
                "type": BlockType.LIST,
                "attribute": attribute,
                "ilevel": ilevel,
                "content": [],
            }
            if attribute == "ordered" and start is not None and ilevel == level:
                new_list_block["start"] = start

            if list_stack:
                list_stack[-1]["content"].append(new_list_block)
            else:
                self.cur_page.append(new_list_block)

            list_stack.append(new_list_block)

    @staticmethod
    def _get_ordered_list_next_number(list_block: dict) -> int:
        """Computes the next number that should be used when appending sibling text items to the current ordered list."""
        try:
            start = int(list_block.get("start", 1))
        except (TypeError, ValueError):
            start = 1
        direct_item_count = sum(1 for item in list_block.get("content", []) if item.get("type") != BlockType.LIST)
        return start + direct_item_count

    def _should_restart_ordered_list(
        self,
        list_stack: list,
        level: int,
        attribute: str,
        start: Optional[int],
        start_is_explicit_restart: bool,
    ) -> bool:
        """Determine whether startAt indicates that the current peer ordered list needs to be restarted."""
        if not start_is_explicit_restart:
            return False
        if attribute != "ordered" or start is None:
            return False
        if len(list_stack) != level + 1:
            return False
        current_list = list_stack[level]
        if current_list.get("attribute") != "ordered":
            return False
        if not current_list.get("content"):
            return False
        return start != self._get_ordered_list_next_number(current_list)

    def _append_list_item(
        self,
        list_stack: list,
        level: int,
        attribute: str,
        content: str,
        start: Optional[int] = None,
        start_is_explicit_restart: bool = False,
    ):
        """Appends text items to the target hierarchy list."""
        self._ensure_list_level(
            list_stack,
            level,
            attribute,
            start,
            start_is_explicit_restart,
        )
        list_stack[-1]["content"].append(
            {
                "type": BlockType.TEXT,
                "content": content,
            }
        )

    @staticmethod
    def _normalize_contiguous_list_level(
        raw_level: int,
        base_level: int,
    ) -> int:
        """
        Normalize the first visible level of consecutive list segments to level 0 to avoid indenting the output code block when the parent is missing.
        """
        return max(0, raw_level - base_level)

    def _is_list_item(self, paragraph) -> tuple[bool, str]:
        """
        Determine whether the paragraph should be treated as a list item.
        The method first attempts to parse the list style information by owning the paragraph's shape.
        If this is not possible, fall back to simpler checks based on paragraph properties and levels.
        Args:
            paragraph: The 'python-pptx' paragraph object needs to be checked.

        Returns:
            Returns a 2-tuple (`is_list`, `bullet_type`) where:
            `is_list` - True if the paragraph is treated as a list item, False otherwise;
            `bullet_type` - is one of: 'Bullet' (bullet), 'Numbered' (number), or 'None',
            Describes the list tag type.
        """
        # Try to get the shape from the paragraph (the object containing the paragraph), if possible
        shape = None
        try:
            # This path is for the python-pptx paragraph object
            # First get the text frame (the parent object of the paragraph)
            text_frame = paragraph._parent
            # Then get the shape (the parent object of the text frame)
            shape = text_frame._parent
        except AttributeError:
            pass

        if shape is not None:
            list_info = self._get_paragraph_list_info(shape, paragraph)
            if not list_info["is_list"]:
                return (False, "None")

            if list_info["attribute"] == "ordered":
                return (True, "Numbered")
            return (True, "Bullet")

        # If the shape cannot be obtained, use a simpler check
        p = paragraph._element
        if p.find(".//a:buChar", namespaces={"a": self.namespaces["a"]}) is not None:
            return (True, "Bullet")
        elif p.find(".//a:buAutoNum", namespaces={"a": self.namespaces["a"]}) is not None:
            return (True, "Numbered")
        elif paragraph.level > 0:
            # Most likely sublist items (indentation indicates nesting)
            return (True, "None")
        else:
            return (False, "None")

    def _get_effective_list_marker(self, shape, paragraph) -> dict:
        """
        Returns a dictionary of valid list tokens describing paragraphs.
        List markup information can come from multiple sources: direct paragraph properties, shape-level list styles,
        Layout placeholder or master slide text style. This helper method parses all these layers and returns
        A unified view of effective markup.

        Args:
            shape: Shape object containing a paragraph.
            paragraph: The 'python-pptx' paragraph object needs to be checked.

        Returns:
            Returns a dictionary of list tag information, where:
            `is_list` - True/False/None, indicating whether this is a list item;
            `kind` - is one of the following: `buChar`, `buAutoNum`, `buBlip`, `buNone` or None, describing the mark type;
            `detail` - Bullet character or number type string, or None if not applicable;
            `start_is_explicit_restart` - True represents start from the paragraph-level explicit startAt;
            `level` - Paragraph level, range is (0, 8).
        """
        p = paragraph._element
        lvl = self._get_paragraph_level(p)

        # 1) Direct paragraph attributes
        pPr = p.find("a:pPr", namespaces=self.namespaces)
        is_list, kind, detail, start = self._parse_bullet_from_paragraph_properties(pPr)
        if is_list is not None:
            return {
                "is_list": is_list,
                "kind": kind,
                "detail": detail,
                "level": lvl,
                "start": start,
                # Only startAt declared in the paragraph itself represents an explicit restart.
                "start_is_explicit_restart": kind == "buAutoNum" and start is not None,
            }

        # 2) Shape level list style (txBody/a:lstStyle)
        txBody = shape._element.find(".//p:txBody", namespaces=self.namespaces)
        is_list, kind, detail, start = self._parse_bullet_from_text_body_list_style(txBody, lvl)
        if is_list is not None:
            return {
                "is_list": is_list,
                "kind": kind,
                "detail": detail,
                "level": lvl,
                "start": start,
                "start_is_explicit_restart": False,
            }

        # 3) Layout placeholder list style (if this is a placeholder)
        layout_result = None
        if shape.is_placeholder:
            layout_ph = self._resolve_layout_placeholder(shape)

            if layout_ph is not None:
                layout_tx = layout_ph._element.find(".//p:txBody", namespaces=self.namespaces)
                (
                    is_list,
                    kind,
                    detail,
                    start,
                ) = self._parse_bullet_from_text_body_list_style(layout_tx, lvl)

                # Only use layout results if is_list is explicitly True/False
                if is_list is not None:
                    layout_result = {
                        "is_list": is_list,
                        "kind": kind,
                        "detail": detail,
                        "level": lvl,
                        "start": start,
                        "start_is_explicit_restart": False,
                    }

                # 4) Parse the main text style
                ph_type = shape.placeholder_format.type
                master = shape.part.slide.slide_layout.slide_master
                (
                    is_list,
                    kind,
                    detail,
                    start,
                ) = self._parse_bullet_from_master_text_styles(master, ph_type, lvl)

                # Check if the main style has markup information
                if kind in ("buChar", "buAutoNum", "buBlip"):
                    return {
                        "is_list": True,
                        "kind": kind,
                        "detail": detail,
                        "level": lvl,
                        "start": start,
                        "start_is_explicit_restart": False,
                    }
                elif is_list is not None:
                    return {
                        "is_list": is_list,
                        "kind": kind,
                        "detail": detail,
                        "level": lvl,
                        "start": start,
                        "start_is_explicit_restart": False,
                    }

            # If layout has explicit is_list value but master didn't override it, use layout
            # If the layout has an explicit is_list value but the main style does not override it, the layout result is used
            if layout_result is not None:
                return layout_result

        return {
            "is_list": None,
            "kind": None,
            "detail": None,
            "level": lvl,
            "start": None,
            "start_is_explicit_restart": False,
        }

    def _get_paragraph_level(self, paragraph) -> int:
        """
        Returns the indent level of the paragraph XML element.
        Paragraphs can have different indentation levels (0-8). The level is stored in the 'lvl' attribute of the paragraph attribute XML element.

        Args:
            paragraph: Need to extract level paragraph XML element.

        Returns:
            Returns paragraph levels in the range (0, 8). When the 'a:pPr' element is not found and there is no 'lvl' attribute
            Or when the 'lvl' attribute value is invalid, 0 is returned.
        """
        pPr = paragraph.find("a:pPr", namespaces=self.namespaces)
        if pPr is not None and "lvl" in pPr.attrib:
            try:
                return int(pPr.get("lvl"))
            except ValueError:
                pass
        return 0

    @staticmethod
    def _parse_positive_int(value: Optional[str]) -> Optional[int]:
        """Parse positive integer attributes, return None when illegal or missing."""
        if value is None:
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _parse_bullet_from_paragraph_properties(
        self, pPr
    ) -> tuple[Optional[bool], Optional[str], Optional[str], Optional[int]]:
        """
        Parse bullet or numbering information from paragraph property nodes.
        Check the 'a:pPr' or 'a:lvlXpPr' element and extract information about bullet characters, autonumbering,
        Picture bullets or information marked explicitly with 'buNone'.

        Args:
            pPr: Paragraph attribute XML element ('a:pPr' or 'a:lvlXpPr').

        Returns:
            Returns a 4-tuple (`is_list`, `kind`, `detail`, `start`), where:
            `is_list` - is True/False/None, indicating whether this is a list item;
            `kind` - is one of the following: `buChar` (bullet character), `buAutoNum` (automatic numbering),
            `buBlip` (picture bullet), `buNone` (unmarked), or None, describing the mark type;
            `detail` - Bullet character, number type string, or None if not applicable.
            `start` - Automatic numbering starting value; when not declared, it is None.
        """
        if pPr is None:
            return (None, None, None, None)

        # Explicitly specify no bullets
        if pPr.find("a:buNone", namespaces=self.namespaces) is not None:
            return (False, "buNone", None, None)

        # bullet character
        buChar = pPr.find("a:buChar", namespaces=self.namespaces)
        if buChar is not None:
            return (True, "buChar", buChar.get("char"), None)

        # automatic numbering
        buAuto = pPr.find("a:buAutoNum", namespaces=self.namespaces)
        if buAuto is not None:
            return (
                True,
                "buAutoNum",
                buAuto.get("type"),
                self._parse_positive_int(buAuto.get("startAt")),
            )

        # Picture bullets
        buBlip = pPr.find("a:buBlip", namespaces=self.namespaces)
        if buBlip is not None:
            return (True, "buBlip", "image", None)

        return (None, None, None, None)

    def _parse_bullet_from_text_body_list_style(
        self, txBody, lvl: int
    ) -> tuple[Optional[bool], Optional[str], Optional[str], Optional[int]]:
        """
        Parse bullet or numbering information from a text body's list style.
        Search for 'a:lstStyle/a:lvl{lvl+1}pPr' under 'txBody' and use level specific paragraph properties
        Infer bulleted or numbered information.

        Args:
            txBody: Text body XML element 'p:txBody'.
            lvl: Paragraph level, range is (0, 8).
        Returns:
            Returns a 4-tuple (`is_list`, `kind`, `detail`, `start`), where:
            `is_list` - is True/False/None, indicating whether this is a list item;
            `kind` - is one of the following: `buChar`, `buAutoNum`, `buBlip`, `buNone` or None;
            `detail` - Bullet character, number type string, or None if not applicable.
            `start` - Automatic numbering starting value; when not declared, it is None.
        """
        if txBody is None:
            return (None, None, None, None)
        lstStyle = txBody.find("a:lstStyle", namespaces=self.namespaces)
        lvl_pPr = self._find_level_properties_in_list_style(lstStyle, lvl)
        return self._parse_bullet_from_paragraph_properties(lvl_pPr)

    def _parse_bullet_from_master_text_styles(
        self, slide_master, placeholder_type, lvl: int
    ) -> tuple[Optional[bool], Optional[str], Optional[str], Optional[int]]:
        """
        Parse bulleted or numbered information from the main slide's text style.
        Find the corresponding style bucket ('titleStyle', 'bodyStyle' or
        'otherStyle') and extracts bulleted or numbered information for a given level.

        Args:
            slide_master: The master slide object associated with the current slide.
            placeholder_type: Placeholder type enumeration from 'PP_PLACEHOLDER'.
            lvl: Paragraph level, range is (0, 8).

        Returns:
            Returns a 4-tuple (`is_list`, `kind`, `detail`, `start`), where:
            `is_list` - is True/False/None, indicating whether this is a list item;
            `kind` - is one of the following: `buChar`, `buAutoNum`, `buBlip`, `buNone` or None;
            `detail` - Bullet character, number type string, or None if not applicable.
            `start` - Automatic numbering starting value; when not declared, it is None.
        """
        style = self._get_master_text_style_node(slide_master, placeholder_type)
        if style is None:
            return (None, None, None, None)

        lvl_pPr = style.find(f".//a:lvl{lvl + 1}pPr", namespaces=self.namespaces)
        return self._parse_bullet_from_paragraph_properties(lvl_pPr)

    def _find_level_properties_in_list_style(self, lstStyle, lvl: int):
        """Find the level-specific paragraph properties node from a list style.
        Finds the paragraph attribute node at the specified level from the list style.

        This looks for an `a:lvl{lvl+1}pPr` node inside an `a:lstStyle` element, where
        Find the 'a:lvl{lvl+1}pPr' node within the 'a:lstStyle' element, where 'a:lvl1pPr' corresponds to level 0,
        `a:lvl1pPr` corresponds to level 0, `a:lvl2pPr` to level 1, and so on.
        'a:lvl2pPr' corresponds to level 1, and so on.

        Args:
            lstStyle: List style XML element `a:lstStyle`.
            lstStyle: List style XML element 'a:lstStyle'.
            lvl: Paragraph level in the range (0, 8).
            lvl: Paragraph level, range is (0, 8).

        Returns:
            Matching `a:lvl{lvl+1}pPr` XML element, or None if no matching element is
            Matching 'a:lvl{lvl+1}pPr'XML element, if no matching element is found None is returned.
                found.
        """
        if lstStyle is None:
            return None
        tag = f"a:lvl{lvl + 1}pPr"
        return lstStyle.find(tag, namespaces=self.namespaces)

    def _get_master_text_style_node(self, slide_master, placeholder_type) -> Optional[etree._Element]:
        """
        Gets the placeholder's corresponding main text style node.
        Most content placeholders (BODY/OBJECT) use 'p:bodyStyle', while titles use 'p:titleStyle'.
        All other placeholders default to 'p:otherStyle'.

        Args:
            slide_master: The master slide object associated with the current slide.
            placeholder_type: Placeholder type enumeration from 'PP_PLACEHOLDER'.

        Returns:
            Matching style node ('p:bodyStyle', 'p:titleStyle' or 'p:otherStyle') from 'p:txStyles' in the main slide, or returns None when no style is defined.
        """
        txStyles = slide_master._element.find(".//p:txStyles", namespaces=self.namespaces)
        if txStyles is None:
            return None

        if placeholder_type in (PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT):
            return txStyles.find("p:bodyStyle", namespaces=self.namespaces)

        if placeholder_type in (
            PP_PLACEHOLDER.TITLE,
            PP_PLACEHOLDER.CENTER_TITLE,
            PP_PLACEHOLDER.SUBTITLE,
        ):
            return txStyles.find("p:titleStyle", namespaces=self.namespaces)

        return txStyles.find("p:otherStyle", namespaces=self.namespaces)
