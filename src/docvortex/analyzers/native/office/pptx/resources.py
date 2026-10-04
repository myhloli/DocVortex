"""PPTX picture, chart and formula resources, reuse the single document state of the current converter."""

from typing import Any, Optional
from loguru import logger
from pptx.enum.shapes import MSO_SHAPE_TYPE
from ..ooxml_chart import extract_chart_html_from_ooxml
from ..image import serialize_office_image
from ..equation.ooxml import is_mathtype_equation_prog_id
from .....schema import BlockType

from .context import DRAWINGML_NS, RELATIONSHIP_NS, SVG_BLIP_NS, OMML_NS


class _PptxResources:
    """Centrally maintain pictures, charts and formula resources without changing the document life cycle and public access."""

    @staticmethod
    def _pptx_ole_format(shape: Any) -> Any | None:
        """Safe return of PPTX graphic-frame OLE format."""

        try:
            return shape.ole_format
        except (AttributeError, KeyError, ValueError):
            return None

    def _is_equation_ole_shape(self, shape: Any) -> bool:
        """Determine whether the ProgID of PPTX OLE shape is the MathType/Equation formula."""

        ole_format = self._pptx_ole_format(shape)
        return bool(ole_format is not None and is_mathtype_equation_prog_id(getattr(ole_format, "prog_id", None)))

    def _decode_pptx_ole_equation(self, shape: Any) -> str | None:
        """Decoding PPTX Inline formula OLE; links, icons, and bad objects return null."""

        ole_format = self._pptx_ole_format(shape)
        if ole_format is None:
            return None
        prog_id = getattr(ole_format, "prog_id", None)
        if not is_mathtype_equation_prog_id(prog_id):
            return None
        if bool(getattr(ole_format, "show_as_icon", False)):
            return None
        if self._safe_shape_type(shape) != MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT:
            return None
        try:
            blob = ole_format.blob
        except (AttributeError, KeyError, ValueError):
            blob = None
        latex = self._ooxml_equation_decoder.decode(
            blob,
            prog_id=prog_id,
        )
        if latex is not None:
            return latex

        warning_key = (
            str(getattr(getattr(shape, "part", None), "partname", "")),
            getattr(shape, "shape_id", None),
        )
        if warning_key not in self._mtef_warned_shapes:
            self._mtef_warned_shapes.add(warning_key)
            logger.warning(
                "PPTX_MTEF_FALLBACK: part={!r}, shape_id={!r} has an invalid or unsupported equation OLE object",
                warning_key[0],
                warning_key[1],
            )
        return None

    def _pptx_ole_shape_omml(self, shape: Any) -> list[str]:
        """Extract expressable OMML within the OLE compatible container as a high-priority branch of MTEF."""

        element = getattr(shape, "_element", None)
        if element is None:
            return []
        equations: list[str] = []
        for math in element.findall(f".//{{{OMML_NS}}}oMath"):
            latex = self._convert_math_node_to_latex(math)
            if latex:
                equations.append(latex)
        return equations

    def _handle_tables(self, shape):
        """Convert PowerPoint table to HTML format.

        Args:
            shape: Shape object containing a table.
            parent_slide: Parent slide group.
            slide_ind: Current slide index.
            doc: Document object (not used in this implementation).
            slide_size: Slide size.

        Returns:
            str: The HTML string of the table. If there is no table, None is returned.
        """
        if not shape.has_table:
            return None

        table = shape.table
        table_xml = shape._element

        # Start building the HTML table
        html_parts = ['<table border="1">']

        # Track positions already occupied by merged cells
        # Format: {(row, col): True}
        occupied_cells = {}

        for row_idx, row in enumerate(table.rows):
            html_parts.append("  <tr>")

            for col_idx, cell in enumerate(row.cells):
                # Skip merged cells
                if (row_idx, col_idx) in occupied_cells:
                    continue
                # Get cell XML to read span information
                cell_xml = table_xml.xpath(f".//a:tbl/a:tr[{row_idx + 1}]/a:tc[{col_idx + 1}]")

                if not cell_xml:
                    continue

                cell_xml = cell_xml[0]

                # Parse row spans and column spans
                row_span = cell_xml.get("rowSpan")
                col_span = cell_xml.get("gridSpan")

                row_span = int(row_span) if row_span else 1
                col_span = int(col_span) if col_span else 1

                # Mark the position occupied by this cell
                for r in range(row_idx, row_idx + row_span):
                    for c in range(col_idx, col_idx + col_span):
                        if (r, c) != (row_idx, col_idx):
                            occupied_cells[(r, c)] = True

                # Determine the tag type: use <th> for the first line, <td> for the others
                tag = "th" if row_idx == 0 else "td"

                # Build attributed string
                attrs = []
                if row_span > 1:
                    attrs.append(f'rowspan="{row_span}"')
                if col_span > 1:
                    attrs.append(f'colspan="{col_span}"')

                attr_str = " " + " ".join(attrs) if attrs else ""

                # Get cell text content
                cell_text = cell.text.strip() if cell.text else ""
                # Escape HTML special characters to prevent XSS
                cell_text = cell_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                html_parts.append(f"    <{tag}{attr_str}>{cell_text}</{tag}>")

            html_parts.append("  </tr>")

        html_parts.append("</table>")

        self.cur_page.append(
            {
                "type": BlockType.TABLE,
                "content": "\n".join(html_parts),
            }
        )

        return None

    def _handle_chart(self, shape) -> None:
        """Execute _handle_chart according to the original picture, chart and formula resource rules, maintaining the input order and degradation behavior."""
        try:
            chart_part = shape.chart.part
            chart_xml = chart_part.blob
        except Exception as e:
            logger.warning(f"Warning: chart XML cannot be loaded: {e}")
            return

        workbook_bytes = None
        try:
            chart_workbook = chart_part.chart_workbook
            xlsx_part = chart_workbook.xlsx_part
            if xlsx_part is not None:
                workbook_bytes = xlsx_part.blob
        except Exception as e:
            logger.warning(f"Warning: chart workbook cannot be loaded: {e}")

        try:
            chart_html = extract_chart_html_from_ooxml(chart_xml, workbook_bytes)
        except Exception as e:
            logger.warning(f"Warning: chart HTML cannot be extracted: {e}")
            return

        if not chart_html:
            return

        self.cur_page.append(
            {
                "type": BlockType.CHART,
                "content": chart_html,
            }
        )

    def _handle_pictures(self, shape):
        """Execute _handle_pictures according to the original picture, chart and formula resource rules, maintaining the input order and degradation behavior."""
        image_data = self._get_shape_image_data(shape)
        if image_data is None:
            return

        image_bytes, content_type = image_data

        latex = self._image_equation_decoder.decode(
            image_bytes,
            content_type=content_type,
        )
        if latex:
            self.cur_page.append(
                {
                    "type": BlockType.EQUATION,
                    "content": latex,
                }
            )
            return

        if content_type == "image/svg+xml":
            img_base64 = serialize_office_image(image_bytes, content_type=content_type)
            if img_base64 is None:
                # SVG falls back to PowerPoint when rasterization fails and blip natively stores the raster volume for SVG.
                fallback_data = self._get_shape_image_data(shape, include_svg=False)
                if fallback_data is not None and fallback_data[1] != "image/svg+xml":
                    img_base64 = serialize_office_image(fallback_data[0], content_type=fallback_data[1])
            if img_base64 is not None:
                self.cur_page.append(self._build_image_block(img_base64, shape))
            return

        img_base64 = serialize_office_image(
            image_bytes,
            content_type=content_type,
        )
        if img_base64 is None:
            return

        self.cur_page.append(self._build_image_block(img_base64, shape))

    def _build_image_block(self, img_base64: str, shape) -> dict:
        """Construct a picture block, descr (alternative text) of shape cNvPr and output it as the recognized content."""
        image_block = {
            "type": BlockType.IMAGE,
            "image_base64": img_base64,
        }
        alt_text = self._pptx_shape_alt_text(shape)
        if alt_text:
            image_block["content"] = alt_text
        return image_block

    @staticmethod
    def _pptx_shape_alt_text(shape) -> str:
        """Read the descr alternative text on the shape non-visual property cNvPr, compatible with each shape wrapper element."""
        element = getattr(shape, "_element", None)
        if element is None:
            element = getattr(shape, "element", None)
        if element is None:
            return ""
        for candidate in element.iter():
            tag = str(candidate.tag)
            if tag.endswith("}cNvPr") or tag == "cNvPr":
                descr = (candidate.get("descr") or "").strip()
                if descr:
                    return descr
        return ""

    def _decode_pptx_shape_image_equation(self, shape: Any) -> str | None:
        """Recover formulas from normal picture or OLE preview to WMF/GIF comment."""

        image_data = self._get_shape_image_data(shape)
        if image_data is None:
            return None
        image_bytes, content_type = image_data
        return self._image_equation_decoder.decode(
            image_bytes,
            content_type=content_type,
        )

    @staticmethod
    def _find_first_embedded_image_rid(shape, *, include_svg: bool = True) -> Optional[str]:
        """Execute _find_first_embedded_image_rid according to the original picture, chart and formula resource rules, maintaining the input order and degradation behavior."""
        if include_svg:
            svg_blips = shape._element.findall(f".//{{{SVG_BLIP_NS}}}svgBlip")
            for svg_blip in svg_blips:
                relationship_id = svg_blip.get(f"{{{RELATIONSHIP_NS}}}embed")
                if relationship_id:
                    return relationship_id

        blips = shape._element.findall(f".//{{{DRAWINGML_NS}}}blip")
        for blip in blips:
            relationship_id = blip.get(f"{{{RELATIONSHIP_NS}}}embed")
            if relationship_id:
                return relationship_id

        return None

    @staticmethod
    def _has_blip_without_relationship(shape) -> bool:
        """Determine whether the image node only has empty blip to avoid falsely reporting empty fallback as missing image resources."""
        if not hasattr(shape, "_element"):
            return False

        blips = [
            *shape._element.findall(f".//{{{SVG_BLIP_NS}}}svgBlip"),
            *shape._element.findall(f".//{{{DRAWINGML_NS}}}blip"),
        ]
        if not blips:
            return False

        for blip in blips:
            if blip.get(f"{{{RELATIONSHIP_NS}}}embed") or blip.get(f"{{{RELATIONSHIP_NS}}}link"):
                return False
        return True

    def _get_shape_image_data(self, shape, *, include_svg: bool = True) -> Optional[tuple[bytes, Optional[str]]]:
        """Execute _get_shape_image_data according to the original picture, chart and formula resource rules, maintaining the input order and degradation behavior."""
        relationship_id = None
        if hasattr(shape, "_element"):
            relationship_id = self._find_first_embedded_image_rid(shape, include_svg=include_svg)

        if relationship_id:
            try:
                image_part = shape.part.related_part(relationship_id)
                image_bytes = image_part.blob
            except Exception as e:
                logger.warning(f"Warning: embedded image relation {relationship_id} cannot be loaded: {e}")
            else:
                return image_bytes, getattr(image_part, "content_type", None)

        if self._has_blip_without_relationship(shape):
            logger.debug("Skipping PPTX picture with empty blip and no image relation")
            return None

        try:
            image = shape.image
        except KeyError as e:
            logger.warning(f"Warning: shape image relation cannot be loaded: {e}")
            return None
        except ValueError as e:
            logger.warning(f"Warning: shape image cannot be loaded: {e}")
            return None
        except AttributeError:
            return None

        return image.blob, None
