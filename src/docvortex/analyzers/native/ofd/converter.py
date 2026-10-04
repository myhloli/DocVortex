"""OFD binary stream to paged raw model-list conversion entry."""

from __future__ import annotations

from typing import BinaryIO
import base64

from .constants import MAX_TOTAL_BYTES
from .errors import OfdResourceLimitError
from .package import OfdPackage
from .reading_order import OfdReadingOrderProjector
from .scene import OfdSceneBuilder
from .models import ImageItem
from .vector import render_vector_page, UnsupportedVector


class OfdConverter:
    """Orchestrate OFD package reading, scene construction and reading sequence projection."""

    def __init__(self) -> None:
        """Initialize empty paged output."""
        self.pages: list[list[dict[str, object]]] = []
        self.diagnostics: list[dict[str, object]] = []

    def convert(self, file_binary: BinaryIO) -> None:
        """Read the entire OFD and update the paged model-list."""
        self.pages = []
        self.diagnostics = []
        file_bytes = file_binary.read(MAX_TOTAL_BYTES + 1)
        if len(file_bytes) > MAX_TOTAL_BYTES:
            raise OfdResourceLimitError(f"OFD resource limit exceeded: max_total_bytes={MAX_TOTAL_BYTES}")
        with OfdPackage(file_bytes) as package:
            scenes = OfdSceneBuilder(package).build()
            for scene in scenes:
                if (
                    not scene.text_object_count
                    and not scene.image_object_count
                    and scene.vector_paths
                    and not scene.vector_unsupported
                ):
                    try:
                        payload = render_vector_page(scene, package)
                    except OfdResourceLimitError:
                        raise
                    except (UnsupportedVector, ValueError, RuntimeError, OSError) as exc:
                        scene.diagnostics.append({"code": "ofd_vector_render_failed", "message": str(exc)})
                    else:
                        if payload is not None:
                            scene.images.append(
                                ImageItem(
                                    scene.physical_box,
                                    "data:image/png;base64," + base64.b64encode(payload).decode("ascii"),
                                    0,
                                    None,
                                    "Body",
                                    None,
                                )
                            )
                            scene.diagnostics.append(
                                {
                                    "code": "ofd_vector_rasterized",
                                    "message": "Vector-only page preserved as an image; no text recognition was performed",
                                }
                            )
                self.diagnostics.extend({**item, "page_index": scene.page_idx} for item in scene.diagnostics)
            self.pages = OfdReadingOrderProjector(scenes).project()


__all__ = ["OfdConverter"]
