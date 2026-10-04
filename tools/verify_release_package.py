"""Smoke-test an installed DocVortex wheel before it is published."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import docvortex


def verify_release_package(pdf_path: Path, release_tag: str) -> None:
    """Verify the version, source, and true PDF metadata parsing capabilities of the installed wheel."""
    version = release_tag.removeprefix("v")
    assert docvortex.__version__ == version, (docvortex.__version__, version)

    package_path = Path(docvortex.__file__).resolve()
    environment_path = Path(sys.prefix).resolve()
    assert package_path.is_relative_to(environment_path), package_path

    metadata = docvortex.extract_metadata(pdf_path).metadata
    assert metadata.producer.name == "docvortex", metadata.producer
    assert metadata.producer.version == version, metadata.producer
    assert metadata.document is not None
    assert (metadata.document.page_count or 0) > 0, metadata.document


def main() -> None:
    """Parse smoke input and give success results explicitly for CI log reading."""
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: {Path(sys.argv[0]).name} PDF_PATH")
    pdf_path = Path(sys.argv[1])
    release_tag = os.environ["RELEASE_TAG"]
    verify_release_package(pdf_path, release_tag)
    print(f"Installed DocVortex {docvortex.__version__} package verified with {pdf_path}")


if __name__ == "__main__":
    main()
