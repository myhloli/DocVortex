"""OFD resource file index and scope merge."""

from __future__ import annotations

from loguru import logger
from lxml import etree

from .constants import MAX_DRAW_PARAM_INHERITANCE, OFD_NAMESPACES
from .errors import OfdParseError, OfdResourceLimitError
from .geometry import parse_numbers
from .models import CompositeResource, FontResource, MediaResource, ResourceRegistry
from .package import OfdPackage, element_text, first_child, local_name, namespace_name, parse_int


def _bool_attr(value: str | None) -> bool:
    """Parse the OFD Boolean attribute into a definite value."""
    return (value or "").strip().casefold() in {"true", "1"}


def _resource_asset_part(package: OfdPackage, resource_part: str, base_loc: str, location: str) -> str | None:
    """Combine the resource BaseLoc and the relative path within the resource into a package member."""
    base_part = package.resolve_reference(resource_part, base_loc) if base_loc else posix_parent(resource_part)
    if base_part is None:
        return None
    synthetic_base = f"{base_part.rstrip('/')}/_resource.xml" if base_loc else resource_part
    return package.resolve_reference(synthetic_base, location)


def posix_parent(part_name: str) -> str:
    """Returns the POSIX parent directory of the package member."""
    return part_name.rsplit("/", 1)[0] if "/" in part_name else ""


def merge_drawing_attributes(base: dict[str, str], overlay: dict[str, str]) -> dict[str, str]:
    """Color sub-elements are covered as a whole to prevent new colors from inheriting old gradients or color space markers."""
    result = dict(base)
    for prefix in ("FillColor.", "StrokeColor."):
        if any(key.startswith(prefix) for key in overlay):
            result = {key: value for key, value in result.items() if not key.startswith(prefix)}
    result.update(overlay)
    return result


def drawing_attributes(element: etree._Element) -> dict[str, str]:
    """Retain drawing attributes and direct color sub-elements so that resource inheritance does not lose background and stroke colors."""
    attributes = {str(key): str(value) for key, value in element.attrib.items()}
    for child in element:
        name = local_name(child.tag)
        if name not in {"FillColor", "StrokeColor"}:
            continue
        if child.get("Value") is None or len(child):
            attributes[name + ".Unsupported"] = "true"
        for key, value in child.attrib.items():
            attributes[name + "." + key] = value
    return attributes


def parse_resource_part(package: OfdPackage, resource_part: str | None) -> ResourceRegistry:
    """Parse a single PublicRes, DocumentRes or PageRes."""
    registry = ResourceRegistry()
    if resource_part is None:
        return registry
    root = package.xml_part(resource_part, required=True)
    assert root is not None
    namespace = namespace_name(root.tag)
    if namespace not in OFD_NAMESPACES:
        raise OfdParseError(f"Malformed OFD package: unsupported namespace {namespace!r} in {resource_part!r}")
    base_loc = (root.get("BaseLoc") or "").strip()
    for element in root.iter():
        name = local_name(element.tag)
        resource_id = parse_int(element.get("ID"))
        if resource_id is None:
            continue
        if name == "Font":
            font_file = element_text(first_child(element, "FontFile"))
            font_part = _resource_asset_part(package, resource_part, base_loc, font_file) if font_file else None
            registry.fonts[resource_id] = FontResource(
                resource_id=resource_id,
                font_name=(element.get("FontName") or "").strip(),
                family_name=(element.get("FamilyName") or "").strip(),
                font_part=font_part,
                bold=_bool_attr(element.get("Bold")),
                italic=_bool_attr(element.get("Italic")),
            )
        elif name == "MultiMedia":
            media_file = element_text(first_child(element, "MediaFile"))
            media_part = _resource_asset_part(package, resource_part, base_loc, media_file) if media_file else None
            if media_part:
                registry.media[resource_id] = MediaResource(
                    resource_id=resource_id,
                    media_type=(element.get("Type") or "").strip(),
                    media_format=(element.get("Format") or "").strip(),
                    media_part=media_part,
                )
        elif name == "CompositeGraphicUnit":
            size = parse_numbers(f"{element.get('Width') or ''} {element.get('Height') or ''}", expected=2)
            registry.composites[resource_id] = CompositeResource(
                resource_id=resource_id,
                width=size[0] if size else 0.0,
                height=size[1] if size else 0.0,
                element=element,
            )
        elif name == "DrawParam":
            registry.draw_params[resource_id] = drawing_attributes(element)
    return registry


def merge_registries(*registries: ResourceRegistry) -> ResourceRegistry:
    """Merge resources in the order of Public→Document→Page and record duplicate ID."""
    merged = ResourceRegistry()
    for registry in registries:
        for field_name in ("fonts", "media", "composites", "draw_params"):
            target = getattr(merged, field_name)
            incoming = getattr(registry, field_name)
            for resource_id, value in incoming.items():
                if resource_id in target:
                    logger.warning(f"OFD_DUPLICATE_RESOURCE_ID: kind={field_name}, id={resource_id}; later scope wins")
                target[resource_id] = value
    return merged


def resolve_draw_param(registry: ResourceRegistry, resource_id: int | None) -> dict[str, str]:
    """Iteratively parse DrawParam Relative inheritance, and limit the depth and detection loop."""
    if resource_id is None:
        return {}
    inheritance_chain: list[dict[str, str]] = []
    visited: set[int] = set()
    current_id: int | None = resource_id
    while current_id is not None:
        if current_id in visited:
            raise ValueError(f"OFD DrawParam cycle detected at id={current_id}")
        current = registry.draw_params.get(current_id)
        if current is None:
            break
        if len(inheritance_chain) >= MAX_DRAW_PARAM_INHERITANCE:
            raise OfdResourceLimitError(f"OFD resource limit exceeded: max_draw_param_inheritance={MAX_DRAW_PARAM_INHERITANCE}")
        visited.add(current_id)
        inheritance_chain.append(current)
        current_id = parse_int(current.get("Relative"))

    result: dict[str, str] = {}
    for current in reversed(inheritance_chain):
        result = merge_drawing_attributes(
            result, {key: value for key, value in current.items() if key not in {"ID", "Relative"}}
        )
    return result


__all__ = ["merge_registries", "parse_resource_part", "resolve_draw_param"]
