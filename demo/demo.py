"""Use the DocVortex public interface to complete native parsing, Markdown export and result package saving."""

from __future__ import annotations

import argparse
from pathlib import Path

from docvortex import parse


def run_demo(input_path: Path, output_dir: Path, *, page_range: str = "", overwrite: bool = False) -> None:
    """After parsing once, the multiplexed result is exported, and the result package can be restored in an environment without source files."""
    result = parse(input_path, page_range=page_range, keep_model_json=True)
    markdown = result.export(output_dir / f"{input_path.stem}.md", overwrite=overwrite)
    bundle = result.save_bundle(output_dir / f"{input_path.stem}.docvortex", overwrite=overwrite)
    print(f"Markdown: {markdown.path}")
    print(f"Bundle: {bundle.path}")


def main() -> None:
    """Runs the default text PDF example, also allowing other supported native documents to be passed in."""
    demo_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=demo_dir / "pdfs/demo1.pdf")
    parser.add_argument("--output-dir", type=Path, default=demo_dir / "output")
    parser.add_argument("--page-range", default="", help="PDF 页码范围，例如 1-5、r1 或 all")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    run_demo(args.input, args.output_dir, page_range=args.page_range, overwrite=args.overwrite)


if __name__ == "__main__":
    main()
