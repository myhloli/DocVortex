"""DOCX list and number processing; shares the single document status of the current Converter."""

from typing import Optional
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.text.paragraph import Paragraph
from loguru import logger
from .....schema import BlockType

from .context import _DocxConstants


class _DocxNumbering:
    """Maintain lists and numbers centrally, without creating documents yourself or holding cross-document caches."""

    def _get_numId_and_ilvl(self, paragraph: Paragraph) -> tuple[Optional[int], Optional[int]]:
        """
        Get the list number ID and level of the paragraph.

        Args:
            paragraph: paragraph object

        Returns:
            tuple[Optional[int], Optional[int]]: (numId, ilvl) tuple
        """
        numPr = self._get_effective_numPr(paragraph)

        if numPr is not None:
            # Get the numId element and extract the value
            namespaces = getattr(numPr, "nsmap", None) or _DocxConstants._BLIP_NAMESPACES
            numId_elem = numPr.find("w:numId", namespaces=namespaces)
            ilvl_elem = numPr.find("w:ilvl", namespaces=namespaces)
            numId = numId_elem.get(self.XML_KEY) if numId_elem is not None else None
            ilvl = ilvl_elem.get(self.XML_KEY) if ilvl_elem is not None else None

            numId_int = self._str_to_int(numId, None)
            ilvl_int = self._str_to_int(ilvl, None)
            if numId_int == 0:
                # numId=0 is a signal in Word to explicitly cancel numbering and the numbering hierarchy can no longer be inherited from the style.
                return numId_int, ilvl_int
            if numId_int is not None and ilvl_int is None:
                ilvl_int = self._infer_numbering_ilvl_from_style(numId_int, paragraph)

            return numId_int, ilvl_int

        return None, None  # if paragraph is not part of list

    def _get_numbering_num_element(self, numId: int) -> Optional[BaseOxmlElement]:
        """Get the num definition in word/numbering.xml based on numId."""
        numbering_root = self._get_numbering_root()
        if numbering_root is None:
            return None

        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        return numbering_root.find(
            f".//w:num[@w:numId='{numId}']",
            namespaces=namespaces,
        )

    def _get_abstract_numbering_element(self, numId: int) -> Optional[BaseOxmlElement]:
        """Obtain the corresponding abstractNum definition based on numId, which is used to reuse the numbering level parsing logic."""
        numbering_root = self._get_numbering_root()
        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        if numbering_root is None:
            return None

        num_element = self._get_numbering_num_element(numId)
        if num_element is None:
            return None

        abstract_num_id_elem = num_element.find(".//w:abstractNumId", namespaces=namespaces)
        if abstract_num_id_elem is None:
            return None

        abstract_num_id = abstract_num_id_elem.get(self.XML_KEY)
        if abstract_num_id is None:
            return None

        abstract_num_xpath = f".//w:abstractNum[@w:abstractNumId='{abstract_num_id}']"
        return numbering_root.find(abstract_num_xpath, namespaces=namespaces)

    def _infer_numbering_ilvl_from_style(self, numId: int, paragraph: Paragraph) -> Optional[int]:
        """When numPr only has numId, the numbering hierarchy is checked based on pStyle in numbering.xml."""
        abstract_num_element = self._get_abstract_numbering_element(numId)
        if abstract_num_element is None:
            return None

        style_ids = {
            str(getattr(style, "style_id", "") or "") for style in self._iter_style_chain(self._get_paragraph_style(paragraph))
        }
        style_ids.discard("")
        if not style_ids:
            return None

        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        ilvl_attr = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}ilvl"
        for lvl_element in abstract_num_element.findall(".//w:lvl", namespaces=namespaces):
            p_style = lvl_element.find("w:pStyle", namespaces=namespaces)
            if p_style is None:
                continue
            if p_style.get(self.XML_KEY) in style_ids:
                return self._str_to_int(lvl_element.get(ilvl_attr), None)
        return None

    def _get_numbering_root(self) -> Optional[BaseOxmlElement]:
        """Load and cache word/numbering.xml once per conversion."""
        if self._numbering_root_loaded:
            return self._numbering_root

        self._numbering_root_loaded = True

        if not hasattr(self.docx_obj, "part") or not hasattr(self.docx_obj.part, "package"):
            return None

        for part in self.docx_obj.part.package.parts:
            if "numbering" in part.partname:
                self._numbering_root = part.element
                break

        return self._numbering_root

    def _get_numbering_level_definition(self, numId: int, ilvl: int) -> Optional[BaseOxmlElement]:
        """Resolve and cache the numbering level definition for a numId/ilvl pair."""
        cache_key = (numId, ilvl)
        if cache_key in self._numbering_level_cache:
            return self._numbering_level_cache[cache_key]

        numbering_root = self._get_numbering_root()
        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        lvl_element: Optional[BaseOxmlElement] = None

        abstract_num_element = self._get_abstract_numbering_element(numId)
        if numbering_root is not None and abstract_num_element is not None:
            lvl_xpath = f".//w:lvl[@w:ilvl='{ilvl}']"
            lvl_element = abstract_num_element.find(lvl_xpath, namespaces=namespaces)

        self._numbering_level_cache[cache_key] = lvl_element
        return lvl_element

    def _get_numbering_level_start(self, numId: int, ilvl: int) -> int:
        """To resolve the starting value of the numbering level, num/lvlOverride is used first, followed by abstractNum/lvl/start."""
        cache_key = (numId, ilvl)
        if cache_key in self._numbering_start_cache:
            return self._numbering_start_cache[cache_key]

        namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        start = 1
        num_element = self._get_numbering_num_element(numId)
        if num_element is not None:
            override = num_element.find(
                f"w:lvlOverride[@w:ilvl='{ilvl}']",
                namespaces=namespaces,
            )
            if override is not None:
                start_override = override.find(
                    "w:startOverride",
                    namespaces=namespaces,
                )
                if start_override is not None:
                    start = self._str_to_int(start_override.get(self.XML_KEY), start)
                    self._numbering_start_cache[cache_key] = start
                    return start

        lvl_element = self._get_numbering_level_definition(numId, ilvl)
        if lvl_element is not None:
            start_element = lvl_element.find("w:start", namespaces=namespaces)
            if start_element is not None:
                start = self._str_to_int(start_element.get(self.XML_KEY), start)

        self._numbering_start_cache[cache_key] = start
        return start

    def _advance_list_counter(self, numId: int, ilvl: int) -> int:
        """Advance the Word number count and return the actual sequence number that the current list item should display."""
        counter_key = (numId, ilvl)
        if counter_key not in self.list_counters:
            current_number = self._get_numbering_level_start(numId, ilvl)
        else:
            current_number = self.list_counters[counter_key] + 1
        self.list_counters[counter_key] = current_number

        # After the parent numbering is advanced, the child numbering should restart from the defined starting value the next time it occurs.
        for key in list(self.list_counters.keys()):
            counter_num_id, counter_ilevel = key
            if counter_num_id == numId and counter_ilevel > ilvl:
                self.list_counters.pop(key, None)

        return current_number

    def _is_numbered_list(self, numId: int, ilvl: int) -> bool:
        """
        Check whether the list is a numbered list based on the numFmt value.

        Args:
            numId: List number ID
            ilvl: List level

        Returns:
            bool: If it is a numbered list, return True, otherwise return False
        """
        try:
            lvl_element = self._get_numbering_level_definition(numId, ilvl)
            if lvl_element is None:
                return False
            namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}

            # Get the numFmt element
            num_fmt_element = lvl_element.find(".//w:numFmt", namespaces=namespaces)
            if num_fmt_element is None:
                return False

            num_fmt = num_fmt_element.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")

            # Numbering formats include: decimal, lowerRoman, upperRoman, lowerLetter, upperLetter
            # Bullet formats include: bullet
            numbered_formats = {
                "decimal",
                "lowerRoman",
                "upperRoman",
                "lowerLetter",
                "upperLetter",
                "decimalZero",
            }

            return num_fmt in numbered_formats

        except Exception as e:
            logger.debug(f"Error determining if list is numbered: {e}")
            return False

    def _add_list_item(
        self,
        *,
        numid: int,
        ilevel: int,
        elements: list,
        is_numbered: bool = False,
        text: str = "",
        equations: list = None,
    ) -> list:
        """
        Add list item.

        Generated list structure:
        {
            "type": "list",
            "attribute": "ordered" / "unordered",
            "ilevel": 0,
            "content": [
                {"type": "text", "content": "List item text"},
                {"type": "list", "attribute": "...", "ilevel": 1, "content": [...]},
                {"type": "text", "content": "Another list item"}
            ]
        }

        Args:
            numid: ListID
            ilevel: Indentation level
            elements: element list
            is_numbered: Is it numbered?
            text: Processed text (contains formula marks)
            equations: Formula list

        Returns:
            list[RefItem]: element reference list
        """
        if equations is None:
            equations = []
        if not elements:
            return None

        # Build content_text to handle inline formulas and hyperlinks
        content_text = self._build_text_with_equations_and_hyperlinks(elements, text, equations)
        content_text = self._normalize_text_block_content(content_text)
        if not content_text:
            return None

        # Determine list properties
        list_attribute = "ordered" if is_numbered else "unordered"
        list_start = self._advance_list_counter(numid, ilevel) if is_numbered else None

        # Case 1: The previous list ID does not exist, or a new list with a different numId is encountered, create a new top-level list
        if self.pre_num_id == -1 or self.pre_num_id != numid:
            # When switching to a different list, reset the old list state first
            if self.pre_num_id != -1:
                self._close_active_list()

            list_block = {
                "type": BlockType.LIST,
                "attribute": list_attribute,
                "content": [],
                "ilevel": ilevel,
            }
            if list_start is not None:
                list_block["start"] = list_start
            self.cur_page.append(list_block)
            # Push onto the stack, record the current list block
            self.list_block_stack.append(list_block)

            list_item = {
                "type": BlockType.TEXT,
                "content": content_text,
            }

            list_block["content"].append(list_item)
            self.pre_num_id = numid
            self.pre_ilevel = ilevel

        # Case 2: Increase indentation, open sublist
        elif (
            self.pre_num_id == numid  # same list
            and self.pre_ilevel != -1  # The previous indentation level is known
            and self.pre_ilevel < ilevel  # The current level is more indented than before
        ):
            # Create new sublist block
            child_list_block = {
                "type": BlockType.LIST,
                "attribute": list_attribute,
                "content": [],
                "ilevel": ilevel,
            }
            if list_start is not None:
                child_list_block["start"] = list_start

            if not self.list_block_stack:
                logger.warning(
                    f"Missing DOCX list parent for increased indent; numid={numid}, ilevel={ilevel}. Starting a new list block."
                )
                self.cur_page.append(child_list_block)
                self.list_block_stack.append(child_list_block)
                child_list_block["content"].append(
                    {
                        "type": BlockType.TEXT,
                        "content": content_text,
                    }
                )
                self.pre_ilevel = ilevel
                return None

            # Get the list block on top of the stack and add the sublist directly to its content
            parent_list_block = self.list_block_stack[-1]
            parent_list_block["content"].append(child_list_block)

            # Push onto the stack, record the current list block
            self.list_block_stack.append(child_list_block)

            # Add current list item to sublist
            list_item = {
                "type": BlockType.TEXT,
                "content": content_text,
            }
            child_list_block["content"].append(list_item)

            # Update current indent
            self.pre_ilevel = ilevel

        # Case 3: Reduce indentation, close sublist
        elif (
            self.pre_num_id == numid  # same list
            and self.pre_ilevel != -1  # The previous indentation level is known
            and ilevel < self.pre_ilevel  # The current level is less indented than before
        ):
            # Pop the stack until a matching ilevel is found
            while self.list_block_stack:
                top_list_block = self.list_block_stack[-1]
                if top_list_block["ilevel"] == ilevel:
                    break
                self.list_block_stack.pop()
            if not self.list_block_stack:
                logger.warning(f"Malformed DOCX list nesting; numid={numid}, ilevel={ilevel}. Starting a new list block.")
                list_block = {
                    "type": BlockType.LIST,
                    "attribute": list_attribute,
                    "content": [],
                    "ilevel": ilevel,
                }
                if list_start is not None:
                    list_block["start"] = list_start
                self.cur_page.append(list_block)
                self.list_block_stack.append(list_block)
            else:
                list_block = self.list_block_stack[-1]

            list_item = {
                "type": BlockType.TEXT,
                "content": content_text,
            }
            list_block["content"].append(list_item)
            self.pre_ilevel = ilevel

        # Case 4: Sibling list items (same indentation)
        elif self.pre_num_id == numid and self.pre_ilevel == ilevel:
            if not self.list_block_stack:
                logger.warning(
                    f"Missing DOCX list block for same indent; numid={numid}, ilevel={ilevel}. Starting a new list block."
                )
                list_block = {
                    "type": BlockType.LIST,
                    "attribute": list_attribute,
                    "content": [],
                    "ilevel": ilevel,
                }
                if list_start is not None:
                    list_block["start"] = list_start
                self.cur_page.append(list_block)
                self.list_block_stack.append(list_block)
            else:
                # Get the list block at the top of the stack
                list_block = self.list_block_stack[-1]

            list_item = {
                "type": BlockType.TEXT,
                "content": content_text,
            }
            list_block["content"].append(list_item)

        else:
            logger.warning(
                "Unexpected DOCX list state in _add_list_item: "
                f"pre_num_id={self.pre_num_id}, numid={numid}, "
                f"pre_ilevel={self.pre_ilevel}, ilevel={ilevel}, "
                f"stack_depth={len(self.list_block_stack)}. "
            )

    def _detect_heading_list_numids(self) -> set:
        """
        Prescan the document, detecting the list numId used as chapter headings.

        Judgment basis (two conditions need to be met at the same time):
        1. The list items of numId are interspersed with non-list text content (paragraphs/tables, etc.);
        2. The list item of numId appears in **multiple different indentation levels** (ilevel > 1 type),
           That is a true multi-level list structure, rather than an ordinary single-level content item list.

        This can avoid misjudgment of a single-level list with "small tags interspersed between multiple content items" as a title list.

        Returns:
            set: A list of numId collections that should be converted to title blocks
        """
        heading_numids = set()
        # Collect document element sequence: ("list", numid, ilevel) or ("content",)
        items = []
        # Record all ilevel that appear in each numId to determine whether it is a real multi-level list
        numid_ilvels: dict[int, set] = {}

        for element in self.docx_obj.element.body:
            tag_name = self._local_name(element)
            if tag_name is None:
                continue
            if tag_name == "p":
                try:
                    paragraph = Paragraph(element, self.docx_obj)
                    p_style_id, _ = self._get_label_and_level(paragraph)
                    numid, ilevel = self._get_numId_and_ilvl(paragraph)
                    if numid == 0:
                        numid = None
                    text = self._get_paragraph_text(paragraph).strip()
                except Exception:
                    continue

                if numid is not None and ilevel is not None and p_style_id not in ["Title", "Heading"] and text:
                    items.append(("list", numid, ilevel))
                    if numid not in numid_ilvels:
                        numid_ilvels[numid] = set()
                    numid_ilvels[numid].add(ilevel)
                elif p_style_id not in ["Title", "Heading"] and text:
                    items.append(("content", None, None))
            elif tag_name == "tbl":
                items.append(("content", None, None))

        # For each numId, check whether there is text content interspersed between the list items.
        # seen_numids[numid] = True means that the text content appears after the last list item of numId
        seen_numids: dict[int, bool] = {}

        for item_type, numid, ilevel in items:
            if item_type == "list":
                if numid in seen_numids and seen_numids[numid]:
                    # The text content appears after the last list item, satisfying condition 1
                    heading_numids.add(numid)
                seen_numids[numid] = False  # Reset: Log the numId and a new list item appears
            elif item_type == "content":
                # Mark all seen numId as "Text content appears after"
                for nid in seen_numids:
                    seen_numids[nid] = True

        # Condition 2: Only keep true multi-level lists (numId with more than 1 ilevel appearing)
        # Single-level lists (such as content entry lists with only ilevel=0) should not be converted into titles even if they are interspersed with text paragraphs.
        heading_numids = {nid for nid in heading_numids if len(numid_ilvels.get(nid, set())) > 1}

        if heading_numids:
            logger.debug(f"Detected heading-style list numIds (will convert to title blocks): {heading_numids}")

        return heading_numids
