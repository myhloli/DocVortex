from typing import Any, BinaryIO

from ... import PptxModel


def convert_path(file_path: str) -> list[list[dict[str, Any]]]:
    """Call the unified model entry from the PPTX file path."""

    with open(file_path, "rb") as fh:
        return convert_binary(fh)


def convert_binary(file_binary: BinaryIO) -> list[list[dict[str, Any]]]:
    """Compatible with old binary conversion functions and forwarded to PptxModel."""

    return PptxModel().predict(file_binary)


if __name__ == "__main__":
    print(convert_path("powerpoint_sample.pptx"))
