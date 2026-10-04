"""DOCX formula and picture resource processing; share the current single document status of Converter."""

import hashlib
import re
from typing import Any
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.text.paragraph import Paragraph
from loguru import logger
from ..ooxml_chart import extract_chart_html_from_ooxml
from ..image import serialize_office_image
from ..equation.ooxml import is_mathtype_equation_prog_id
from ..equation.omml import oMath2Latex
from .....schema import BlockType

from .context import _DocxConstants


class _DocxResources:
    """Centrally maintain formulas and image resources without creating documents yourself or holding cross-document caches."""

    def _decode_docx_ole_equation(
        self,
        ole_element: Any,
        part: Any,
    ) -> str | None:
        """Decode the formula object from the current DOCX part internal OLE relationship."""

        prog_id = ole_element.get("ProgID") or ole_element.get("ProgId")
        if not is_mathtype_equation_prog_id(prog_id):
            return None
        object_type = (ole_element.get("Type") or "Embed").strip().casefold()
        if object_type != "embed":
            return None
        draw_aspect = (ole_element.get("DrawAspect") or "Content").strip().casefold()
        if draw_aspect == "icon":
            return None

        relationship_id = ole_element.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        relationships = getattr(part, "rels", None)
        relationship = relationships.get(relationship_id) if relationships is not None else None
        if relationship is None or getattr(relationship, "is_external", False):
            return None
        reltype = str(getattr(relationship, "reltype", ""))
        if not reltype.rstrip("/").casefold().endswith("/oleobject"):
            return None
        try:
            blob = relationship.target_part.blob
        except (AttributeError, KeyError, ValueError):
            blob = None
        latex = self._ooxml_equation_decoder.decode(
            blob,
            prog_id=prog_id,
        )
        if latex is not None:
            return latex

        warning_key = (self._docx_part_key(part), str(relationship_id))
        if warning_key not in self._mtef_warned_relations:
            self._mtef_warned_relations.add(warning_key)
            logger.warning(
                "DOCX_MTEF_FALLBACK: part={!r}, relationship={!r} has an invalid or unsupported equation OLE object",
                warning_key[0],
                relationship_id,
            )
        return None

    def _decode_docx_equationxml(
        self,
        shape_element: Any,
        part: Any,
    ) -> str | None:
        """Decode the ``equationxml`` of the current VML shape and deduplicate failed alarms."""

        vml_shape_tag = f"{{{_DocxConstants._BLIP_NAMESPACES['v']}}}shape"
        if getattr(shape_element, "tag", None) != vml_shape_tag:
            return None
        equation_xml = shape_element.get("equationxml")
        if equation_xml is None:
            return None

        latex = self._equationxml_decoder.decode(equation_xml)
        if latex is not None:
            return latex

        digest = hashlib.sha256(equation_xml.encode("utf-8", errors="replace")).hexdigest()[:16]
        warning_key = (
            self._docx_part_key(part),
            str(shape_element.get("id") or ""),
            digest,
        )
        if warning_key not in self._equationxml_warned_shapes:
            self._equationxml_warned_shapes.add(warning_key)
            logger.warning(
                "DOCX_EQUATIONXML_FALLBACK: part={!r}, shape_id={!r}, payload_sha256={!r} is malformed or unsupported",
                warning_key[0],
                warning_key[1],
                warning_key[2],
            )
        return None

    @staticmethod
    def _docx_image_relationship_id(image: Any) -> str | None:
        """Read the internal relationship id of the DrawingML/VML picture element."""

        relationship_id = image.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        if not relationship_id:
            relationship_id = image.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        return str(relationship_id) if relationship_id else None

    @classmethod
    def _docx_image_part(cls, image: Any, part: Any) -> Any | None:
        """Parses the image part through the internal relationships of the current part."""

        relationship_id = cls._docx_image_relationship_id(image)
        relationships = getattr(part, "rels", None)
        if not relationship_id or relationships is None:
            return None
        relationship = relationships.get(relationship_id)
        if relationship is None or getattr(relationship, "is_external", False):
            return None
        try:
            return relationship.target_part
        except (AttributeError, KeyError, ValueError):
            return None

    def _decode_docx_image_equation(
        self,
        image: Any,
        part: Any,
    ) -> str | None:
        """Decode MTEF from WMF/GIF comment of current picture part."""

        image_part = self._docx_image_part(image, part)
        if image_part is None:
            return None
        try:
            blob = image_part.blob
        except (AttributeError, KeyError, ValueError):
            return None
        return self._image_equation_decoder.decode(
            blob,
            part_name=getattr(image_part, "partname", None),
            content_type=getattr(image_part, "content_type", None),
        )

    @staticmethod
    def _select_docx_compatibility_tokens(
        token_groups: list[list[tuple[str, str]]],
    ) -> list[tuple[str, str]]:
        """Press OMML, Equation XML, OLE MTEF, picture MTEF to select the compatible branch."""

        for token_kind in _DocxConstants._FORMULA_SOURCE_PRIORITY:
            for tokens in token_groups:
                if any(kind == token_kind for kind, _value in tokens):
                    return tokens
        return token_groups[0] if token_groups else []

    def _docx_formula_tokens(
        self,
        element: Any,
        part: Any,
    ) -> list[tuple[str, str]]:
        """Extract text, OMML, Equation, XML and MTEF in document order."""

        tag_name = self._local_name(element)
        if tag_name is None:
            return []
        tag = str(getattr(element, "tag", ""))
        word_namespace = _DocxConstants._BLIP_NAMESPACES["w"]

        if tag_name == "AlternateContent":
            branch_tokens = [
                self._docx_formula_tokens(child, part) for child in element if self._local_name(child) in {"Choice", "Fallback"}
            ]
            return self._select_docx_compatibility_tokens(branch_tokens)

        if tag_name == "object" and tag == f"{{{_DocxConstants._BLIP_NAMESPACES['w']}}}object":
            child_tokens = [self._docx_formula_tokens(child, part) for child in element]
            return self._select_docx_compatibility_tokens(child_tokens)

        if tag_name == "txbxContent":
            # The outer paragraph will traverse the text box content separately to avoid repeated extraction of formulas and text here.
            return []

        if tag_name == "oMath" and "officeDocument/2006/math" in tag:
            try:
                latex = str(oMath2Latex(element)).strip()
            except Exception as exc:
                logger.debug(f"Failed to convert DOCX OMML equation to LaTeX: {exc}")
                return []
            return [("omml", latex)] if latex else []

        if tag_name == "shape" and tag == f"{{{_DocxConstants._BLIP_NAMESPACES['v']}}}shape":
            latex = self._decode_docx_equationxml(element, part)
            if latex is not None:
                return [("equationxml", latex)]

        if tag_name == "OLEObject":
            latex = self._decode_docx_ole_equation(element, part)
            return [("mtef", latex)] if latex else []

        if (tag_name == "blip" and tag == "{http://schemas.openxmlformats.org/drawingml/2006/main}blip") or (
            tag_name == "imagedata" and tag == f"{{{_DocxConstants._BLIP_NAMESPACES['v']}}}imagedata"
        ):
            latex = self._decode_docx_image_equation(element, part)
            return [("image_mtef", latex)] if latex else []

        if tag_name == "t" and "officeDocument/2006/math" not in tag:
            return [("text", element.text)] if isinstance(element.text, str) else []
        if tag in {f"{{{word_namespace}}}tab", f"{{{word_namespace}}}ptab"}:
            return [("text", "\t")]
        if tag == f"{{{word_namespace}}}cr":
            return [("text", "\n")]
        if tag == f"{{{word_namespace}}}br":
            break_type = element.get(f"{{{word_namespace}}}type")
            return [("text", "\n")] if break_type in {None, "textWrapping"} else []
        if tag == f"{{{word_namespace}}}noBreakHyphen":
            return [("text", "-")]

        tokens: list[tuple[str, str]] = []
        for child in element:
            tokens.extend(self._docx_formula_tokens(child, part))
        return tokens

    def _picture_is_equation_preview(self, image: Any, part: Any) -> bool:
        """Determine whether the picture belongs to the restored formula preview in the same container."""

        if self._decode_docx_image_equation(image, part):
            return True

        for ancestor in image.iterancestors():
            ancestor_name = self._local_name(ancestor)
            if ancestor_name == "shape":
                if self._decode_docx_equationxml(ancestor, part):
                    return True
                continue
            if ancestor_name in {"object", "AlternateContent"}:
                tokens = self._docx_formula_tokens(ancestor, part)
                if any(kind in _DocxConstants._FORMULA_TOKEN_KINDS for kind, _value in tokens):
                    return True
                continue
        return False

    def _handle_pictures(
        self,
        picture_refs: Any,
        *,
        part: Any | None = None,
    ) -> None:
        """
        Process images.

        Args:
            picture_refs: Picture reference element list

        Returns:

        """

        source_part = part or self._require_document_part()

        seen_rel_ids: set[str] = set()
        # Traverse all picture reference elements, support DrawingML blip and VML imagedata.
        for image in picture_refs:
            if self._picture_is_equation_preview(image, source_part):
                continue
            rel_id = self._docx_image_relationship_id(image)
            if rel_id and rel_id in seen_rel_ids:
                continue
            if rel_id:
                seen_rel_ids.add(rel_id)
            image_part = self._docx_image_part(image, source_part)
            if image_part is None:
                logger.warning("Warning: image cannot be found")
                continue

            img_base64 = serialize_office_image(
                image_part.blob,
                part_name=getattr(image_part, "partname", None),
                content_type=getattr(image_part, "content_type", None),
            )
            if img_base64 is None:
                continue

            image_block = {
                "type": BlockType.IMAGE,
                "image_base64": img_base64,
            }
            alt_text = self._docx_image_alt_text(image)
            if alt_text:
                image_block["content"] = alt_text
            self.cur_page.append(image_block)

    @staticmethod
    def _docx_image_alt_text(image: Any) -> str:
        """Read DrawingML descr for wp:docPr or VML alt for v:shape alternative text."""
        element = image
        while element is not None:
            tag = str(getattr(element, "tag", ""))
            local_name = tag.rsplit("}", 1)[-1]
            if local_name in {"inline", "anchor"}:
                for child in element:
                    child_tag = str(getattr(child, "tag", ""))
                    if child_tag.rsplit("}", 1)[-1] == "docPr":
                        descr = (child.get("descr") or "").strip()
                        if descr:
                            return descr
                return ""
            if local_name == "shape" and "urn:schemas-microsoft-com:vml" in tag:
                alt = (element.get("alt") or "").strip()
                if alt:
                    return alt
                return ""
            element = element.getparent()
        return ""

    def _handle_drawingml(self, elements: list[BaseOxmlElement]):
        """
        Process the DrawingML element. Currently, the chart element is processed first.

        Args:
            elements: List containing DrawingML elements

        Returns:

        """
        chart_rel_types = {
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart",
            "http://purl.oclc.org/ooxml/officeDocument/relationships/chart",
        }
        package_rel_types = {
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package",
            "http://purl.oclc.org/ooxml/officeDocument/relationships/package",
        }
        rel_id_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        for element in elements:
            chart = element.find(".//c:chart", namespaces=_DocxConstants._BLIP_NAMESPACES)
            if chart is None:
                continue

            chart_block = {
                "type": BlockType.CHART,
                "content": "",
            }
            self.cur_page.append(chart_block)

            rel_id = chart.get(rel_id_attr)
            if not rel_id:
                continue

            try:
                chart_rel = self.docx_obj.part.rels[rel_id]
            except KeyError:
                continue

            if chart_rel.reltype not in chart_rel_types:
                continue

            try:
                chart_part = chart_rel.target_part
                chart_xml = chart_part.blob
            except Exception as e:
                logger.warning(f"Warning: chart XML cannot be loaded: {e}")
                continue

            workbook_bytes = None
            try:
                for rel in chart_part.rels.values():
                    if rel.reltype in package_rel_types:
                        workbook_bytes = rel.target_part.blob
                        break
            except Exception as e:
                logger.warning(f"Warning: chart workbook cannot be loaded: {e}")

            try:
                chart_html = extract_chart_html_from_ooxml(chart_xml, workbook_bytes)
            except Exception as e:
                logger.warning(f"Warning: chart HTML cannot be extracted: {e}")
                continue
            if chart_html:
                chart_block["content"] = chart_html

    def _handle_textbox_content(
        self,
        textbox_elements: list,
    ):
        """
        Process the text box contents and add them to the document structure.
        """
        # Collect and organize paragraphs
        container_paragraphs = self._collect_textbox_paragraphs(textbox_elements)

        # process all paragraphs
        all_paragraphs = []

        # Sort the paragraphs within each container and process them in container order
        for paragraphs in container_paragraphs.values():
            # Sort by vertical position within container
            sorted_container_paragraphs = sorted(
                paragraphs,
                key=lambda x: (
                    x[1] is None,
                    x[1] if x[1] is not None else float("inf"),
                ),
            )

            # Add sorted paragraphs to the to-do list
            all_paragraphs.extend(sorted_container_paragraphs)

        # Track processed paragraphs to avoid duplication (same content and location)
        processed_paragraphs = set()

        # process all paragraphs
        for p, position in all_paragraphs:
            # Create Paragraph object to obtain text content
            paragraph = Paragraph(p, self.docx_obj)
            text_content = self._get_paragraph_text(paragraph)

            # Create unique identifiers based on content and location
            paragraph_id = (text_content, position)

            # If the paragraph (same content and position) has already been processed, skip
            if paragraph_id in processed_paragraphs:
                logger.debug(f"Skipping duplicate paragraph: content='{text_content[:50]}...', position={position}")
                continue

            # Mark this paragraph as processed
            processed_paragraphs.add(paragraph_id)

            self._handle_text_elements(p)
        return

    def _collect_textbox_paragraphs(self, textbox_elements):
        """
        Collect and organize paragraphs from text box elements.
        """
        processed_paragraphs = []
        container_paragraphs = {}

        for element in textbox_elements:
            element_id = id(element)
            # Skip if same element has already been processed
            if element_id in processed_paragraphs:
                continue

            tag_name = self._local_name(element)
            if tag_name is None:
                continue
            processed_paragraphs.append(element_id)

            # Process directly found paragraphs (VML text box)
            if tag_name == "p":
                # Find the text box or shape element that contains the paragraph
                container_id = None
                for ancestor in element.iterancestors():
                    if any(ns in ancestor.tag for ns in ["textbox", "shape", "txbx"]):
                        container_id = id(ancestor)
                        break

                if container_id not in container_paragraphs:
                    container_paragraphs[container_id] = []
                container_paragraphs[container_id].append((element, self._get_paragraph_position(element)))

            # Processing the txbxContent element (Word DrawingML text box)
            elif tag_name == "txbxContent":
                paragraphs = element.findall(".//w:p", namespaces=element.nsmap)
                container_id = id(element)
                if container_id not in container_paragraphs:
                    container_paragraphs[container_id] = []

                for p in paragraphs:
                    p_id = id(p)
                    if p_id not in processed_paragraphs:
                        processed_paragraphs.append(p_id)
                        container_paragraphs[container_id].append((p, self._get_paragraph_position(p)))
            else:
                # Try to extract any paragraph from unknown element
                paragraphs = element.findall(".//w:p", namespaces=element.nsmap)
                container_id = id(element)
                if container_id not in container_paragraphs:
                    container_paragraphs[container_id] = []

                for p in paragraphs:
                    p_id = id(p)
                    if p_id not in processed_paragraphs:
                        processed_paragraphs.append(p_id)
                        container_paragraphs[container_id].append((p, self._get_paragraph_position(p)))

        return container_paragraphs

    def _get_paragraph_position(self, paragraph_element):
        """
        Extracts vertical position information from paragraph elements.
        """
        # First try to get the index directly from the w:p element containing the order-related attributes
        if hasattr(paragraph_element, "getparent") and paragraph_element.getparent() is not None:
            parent = paragraph_element.getparent()
            # Get all paragraph sibling nodes
            paragraphs = [p for p in parent.getchildren() if self._local_name(p) == "p"]
            # Find the index of the current paragraph within its sibling nodes
            try:
                paragraph_index = paragraphs.index(paragraph_element)
                return paragraph_index  # Use index as location to guarantee consistent ordering
            except ValueError:
                pass

        # Find position hint attributes in elements and their ancestors
        for elem in (*[paragraph_element], *paragraph_element.iterancestors()):
            # Check the direct location attribute
            for attr_name in ["y", "top", "positionY", "y-position", "position"]:
                value = elem.get(attr_name)
                if value:
                    try:
                        # Remove any non-numeric characters (such as 'pt', 'px', etc.)
                        clean_value = re.sub(r"[^0-9.]", "", value)
                        if clean_value:
                            return float(clean_value)
                    except (ValueError, TypeError):
                        pass

            # Check the displacement information in the transform attribute
            transform = elem.get("transform")
            if transform:
                # Extract the second parameter of translate from the transform matrix
                match = re.search(r"translate\([^,]+,\s*([0-9.]+)", transform)
                if match:
                    try:
                        return float(match.group(1))
                    except ValueError:
                        pass

            # Check for anchor point or relative position indicator in Word format
            # 'dist' class attribute can represent relative position
            for attr_name in ["distT", "distB", "anchor", "relativeFrom"]:
                if elem.get(attr_name) is not None:
                    return elem.sourceline  # Use XML source line number as fallback

        # Find specific properties for the VML shape
        for ns_uri in paragraph_element.nsmap.values():
            if "vml" in ns_uri:
                # Try to extract the top value from the style property
                style = paragraph_element.get("style")
                if style:
                    match = re.search(r"top:([0-9.]+)pt", style)
                    if match:
                        try:
                            return float(match.group(1))
                        except ValueError:
                            pass

        # If there is no better indication of the location, use the XML source line number as a proxy for the sequence
        return paragraph_element.sourceline if hasattr(paragraph_element, "sourceline") else None
