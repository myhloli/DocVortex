"""Convert the command line independently, and all business behaviors are entrusted to the public API."""

from __future__ import annotations

from pathlib import Path
import sys

import click
from loguru import logger

from .version import __version__


_LOG_LEVELS = ("trace", "debug", "info", "warning", "error", "critical")


def _configure_log_level(level: str) -> None:
    """Rebuild loguru standard error output by global parameters and filter logs below that level."""
    logger.remove()
    logger.add(sys.stderr, level=level.upper())


@click.group(help="DocVortex: native multi-format document parsing and conversion.")
@click.version_option(__version__)
@click.option(
    "--log-level",
    type=click.Choice(_LOG_LEVELS, case_sensitive=False),
    default="info",
    show_default=True,
    envvar="DOCVORTEX_LOG_LEVEL",
    help="Global loguru log level; place this root option before the command.",
)
def main(log_level: str) -> None:
    """Provides a command line entry for an independent document engine and applies global log levels."""
    _configure_log_level(log_level)


@main.command("convert")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", required=True, type=click.Path(path_type=Path))
@click.option(
    "--format",
    "output_format",
    default="markdown",
    show_default=True,
    type=click.Choice(["markdown", "html", "latex", "docx", "epub", "pdf", "structured_content"]),
)
@click.option("--pages", "page_range", default="", help="PDF page selection: 1-5, r1, all.")
@click.option("--overwrite", is_flag=True)
@click.option("--pdf-layout", type=click.Choice(["auto", "original", "reflow"]), default="auto", show_default=True)
def convert_command(source: Path, output: Path, output_format: str, page_range: str, overwrite: bool, pdf_layout: str) -> None:
    """Convert native documents and write out target files and required materials."""
    from .api import convert
    from .render import PdfLayout, PdfRenderOptions

    try:
        if output_format != "pdf" and pdf_layout != "auto":
            raise ValueError("--pdf-layout requires --format pdf")
        options = PdfRenderOptions(layout=PdfLayout(pdf_layout)) if output_format == "pdf" else None
        result = convert(
            source, output, output_format=output_format, page_range=page_range, overwrite=overwrite, options=options
        )
    except (ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(str(result.path))


@main.command("classify")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def classify_command(source: Path) -> None:
    """Explicitly determine whether PDF or OCR should use text parsing, without initiating any inference."""
    from .document.pdf import PDFDocument

    with PDFDocument(source.read_bytes()) as document:
        click.echo(document.classify())


__all__ = ["main"]

if __name__ == "__main__":
    main()
