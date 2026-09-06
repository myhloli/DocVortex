"""独立转换命令行，业务行为全部委托公开 API。"""

from __future__ import annotations

from pathlib import Path

import click

from .version import __version__


@click.group(help="DocVortex: native multi-format document parsing and conversion.")
@click.version_option(__version__)
def main() -> None:
    """提供独立文档引擎的命令行入口。"""


@main.command("convert")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", required=True, type=click.Path(path_type=Path))
@click.option(
    "--format",
    "output_format",
    default="markdown",
    show_default=True,
    type=click.Choice(
        ["markdown", "html", "latex", "docx", "epub", "pdf", "structured_content", "content_list", "content_list_v2"]
    ),
)
@click.option("--pages", "page_range", default="", help="PDF page selection: 1-5, r1, all.")
@click.option("--overwrite", is_flag=True)
def convert_command(source: Path, output: Path, output_format: str, page_range: str, overwrite: bool) -> None:
    """转换原生文档并写出目标文件及所需素材。"""
    from .api import convert

    try:
        result = convert(source, output, output_format=output_format, page_range=page_range, overwrite=overwrite)
    except (ValueError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(str(result.path))


@main.command("classify")
@click.argument("source", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def classify_command(source: Path) -> None:
    """显式判断 PDF 应使用文本解析还是 OCR，不启动任何推理。"""
    from .document.pdf import PDFDocument

    with PDFDocument(source.read_bytes()) as document:
        click.echo(document.classify())


__all__ = ["main"]

if __name__ == "__main__":
    main()
