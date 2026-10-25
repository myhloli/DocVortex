"""核验实际安装wheel的多格式语义、完整重复输出、原生命中及可选分批回归。"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import html
from importlib import metadata
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import traceback
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
NONPDF_TESTS = (
    "tests/test_nonpdf_wheel_verification.py",
    "tests/test_native_spreadsheet_regions.py",
    "tests/test_native_rtf_groups.py",
    "tests/test_native_ofd_tables.py",
    "tests/test_native_ofd_text_geometry.py",
    "tests/test_ofd_glyph_dispatch.py",
    "tests/test_ofd_projection_reuse.py",
    "tests/test_ofd_font_bounds_cache.py",
    "tests/unittest/test_docx_preparse_reuse.py",
    "tests/unittest/test_odf_grid_build_width.py",
    "tests/unittest/test_rtf_prelude_index.py",
    "tests/unittest/test_spreadsheet_sparse_adapters.py",
    "tests/unittest/test_xls_record_views.py",
    "tests/unittest/test_xlsx_projection_cache.py",
)


def write_json(path: Path, value: object) -> None:
    """即时写出完整证据，失败也保留已完成的案例。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(payload: bytes) -> str:
    """按实际字节计算摘要，不忽略诊断、素材或坐标。"""
    return hashlib.sha256(payload).hexdigest()


def full_output(result) -> dict:
    """保留两层完整协议和素材摘要，重复解析比较不丢弃任何字段。"""
    return {
        "model": result.model_json.model_dump(mode="json"),
        "middle": result.middle_json.model_dump(mode="json"),
        "assets": {key: digest(value) for key, value in result.assets.items()},
        "diagnostics": [asdict(value) for value in result.diagnostics],
    }


def text_parts(value):
    """遍历文本和表格内容，跳过图片base64与元数据等无关字符串。"""
    if isinstance(value, dict):
        if isinstance(value.get("content"), str):
            yield html.unescape(re.sub(r"<[^>]+>", " ", value["content"]))
        for child in value.values():
            yield from text_parts(child)
    elif isinstance(value, list):
        for child in value:
            yield from text_parts(child)


def verify_output(output: dict, expected: dict) -> None:
    """固定语义锚点及页数必须匹配，空结构或正文丢失一律失败。"""
    assert len(output["model"]["pages"]) == len(output["middle"]["pages"]) == expected["page_count"]
    assert all(output["model"]["pages"]), "Empty model page"
    assert all(page["blocks"] for page in output["middle"]["pages"]), "Empty middle page"
    text = " ".join(" ".join(part.split()) for part in text_parts(output["model"]["pages"]))
    assert text, "Missing document text"
    for fragment in expected["required_text"]:
        assert fragment in text, f"Missing semantic anchor: {fragment!r}"


def installed_identity() -> dict:
    """核对真实安装路径、版本、协议、ABI标签、RECORD及全部非原生源码资源。"""
    import docvortex
    from docvortex._compute_backend import backend_info
    from tools.verify_native_wheel import expected_version

    package = Path(docvortex.__file__).resolve()
    prefix = Path(sys.prefix).resolve()
    distribution = metadata.distribution("docvortex")
    assert package.is_relative_to(prefix) and package == Path(distribution.locate_file("docvortex/__init__.py")).resolve()
    assert docvortex.__version__ == distribution.version == expected_version()
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    assert not direct.get("dir_info", {}).get("editable"), "Editable install is not wheel acceptance"
    wheel = distribution.read_text("WHEEL") or ""
    tags = [line.removeprefix("Tag: ") for line in wheel.splitlines() if line.startswith("Tag: ")]
    assert tags and all(tag.startswith("cp311-abi3-") for tag in tags) and "Root-Is-Purelib: false" in wheel
    info = backend_info()
    source = (ROOT / "src/docvortex/_compute_backend.py").read_text(encoding="utf-8")
    protocol = int(re.search(r"^_PROTOCOL_VERSION = (\d+)$", source, re.MULTILINE)[1])
    assert info["backend"] == "rust" and info["protocol"] == protocol
    assert Path(info["extension"]).resolve().parent == package.parent
    entries = list(csv.reader(StringIO(distribution.read_text("RECORD") or "")))
    assert entries and len({row[0] for row in entries}) == len(entries)
    checked = 0
    for name, declared, size in entries:
        if not name.startswith("docvortex/") or "__pycache__" in Path(name).parts:
            continue
        path = Path(distribution.locate_file(name)).resolve()
        assert path.is_relative_to(package.parent) and path.is_file()
        algorithm, expected = declared.split("=", 1)
        payload = path.read_bytes()
        assert base64.urlsafe_b64encode(hashlib.new(algorithm, payload).digest()).rstrip(b"=").decode() == expected
        assert len(payload) == int(size)
        if path.suffix not in {".so", ".pyd"}:
            assert payload == (ROOT / "src" / name).read_bytes(), f"Installed source/resource differs: {name}"
        checked += 1
    assert checked > 1
    return {
        "package": str(package),
        "python": sys.version,
        "prefix": str(prefix),
        "compute": info,
        "version": docvortex.__version__,
        "tags": tags,
        "record_package_files_verified": checked,
    }


def generated_inputs() -> list[tuple[str, str, bytes, dict]]:
    """构造跨平台可重放的CSV/TSV、区域发现、RTF扫描及OFD文字/表格输入。"""
    from tools.prepare_nonpdf_corpus import generate_pressure

    sys.path.insert(0, str(ROOT / "tests/unittest"))
    from _ofd_test_utils import build_ofd_package, page_xml, path_object, text_object

    cases = []
    for suffix in ("csv", "tsv", "ods"):
        payload = generate_pressure(suffix, {"rows": 3, "columns": 3})
        cases.append(("generated-" + suffix, suffix, payload, {"page_count": 1, "required_text": ["value_0_0", "value_2_2"]}))
    cases.append(
        (
            "generated-rtf",
            "rtf",
            generate_pressure("rtf", {"paragraphs": 8}),
            {"page_count": 1, "required_text": ["Paragraph 0", "Paragraph 7"]},
        )
    )
    paths = "".join(
        path_object(i, boundary="0 0 210 297", data=data)
        for i, data in enumerate(
            [
                "M 10 10 L 90 10",
                "M 10 40 L 90 40",
                "M 10 70 L 90 70",
                "M 10 10 L 10 70",
                "M 50 10 L 50 70",
                "M 90 10 L 90 70",
            ]
        )
    )
    body = "".join(
        text_object(100 + i, value, boundary=box, size=3, y=3)
        for i, (value, box) in enumerate([("A", "20 20 10 5"), ("B", "60 20 10 5"), ("C", "20 50 10 5"), ("D", "60 50 10 5")])
    )
    body += text_object(200, "NativeOFD" * 8, boundary="10 80 180 10", size=2, y=3, delta_x="g 71 1.5")
    payload = build_ofd_package([("Pages/Page_0/Content.xml", page_xml(paths + body, physical_box="0 0 210 297"))])
    cases.append(("generated-ofd", "ofd", payload, {"page_count": 1, "required_text": ["NativeOFD"]}))
    import docvortex

    font = (Path(docvortex.__file__).parent / "resources/fonts/DroidSansFallbackFull.ttf").read_bytes()
    resource = '<ofd:Res xmlns:ofd="http://www.ofdspec.org/2016" BaseLoc="Res"><ofd:Fonts><ofd:Font ID="1" FontName="Embedded"><ofd:FontFile>Embedded.ttf</ofd:FontFile></ofd:Font></ofd:Fonts></ofd:Res>'
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as archive, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for item in archive.infolist():
            target.writestr(item, resource.encode() if item.filename == "Doc_0/PublicRes.xml" else archive.read(item.filename))
        target.writestr("Doc_0/Res/Embedded.ttf", font)
    cases.append(("generated-embedded-ofd", "ofd", output.getvalue(), {"page_count": 1, "required_text": ["NativeOFD"]}))
    return cases


def verify_exports(result, folder: Path) -> dict:
    """实际写出七格式并回读bundle，错误或缺失旁文件不计成功。"""
    from docvortex import api, load_bundle
    from docvortex.render.contracts import EpubRenderOptions, RenderFormat

    suffixes = {
        "markdown": "md",
        "html": "html",
        "latex": "tex",
        "docx": "docx",
        "epub": "epub",
        "structured_content": "json",
        "pdf": "pdf",
    }
    records = {}
    for target in RenderFormat:
        name = target.value
        options = EpubRenderOptions(modified_at=datetime(2000, 1, 1, tzinfo=timezone.utc)) if name == "epub" else None
        artifact = api.render(result.middle_json, target, assets=result.assets, options=options)
        main = folder / name / ("result." + suffixes[name])
        artifact.write(main)
        expected = {main.name: artifact.content, **dict(artifact.assets)}
        actual = {
            path.relative_to(main.parent).as_posix(): path.read_bytes() for path in main.parent.rglob("*") if path.is_file()
        }
        assert actual == expected and artifact.content, f"Incomplete actual export: {name}"
        records[name] = {
            "files": {key: digest(value) for key, value in actual.items()},
            "diagnostics": [asdict(value) for value in artifact.diagnostics],
        }
    assert len(records) == 7
    result.save_bundle(folder / "bundle")
    restored = load_bundle(folder / "bundle")
    assert restored.middle_json == result.middle_json and restored.diagnostics == result.diagnostics
    assert all(restored.assets[name] == value for name, value in result.assets.items())
    return {"formats": records, "bundle_roundtrip": True}


def main() -> int:
    """先核验安装身份和真实入口，再按需运行同一安装的分批回归并返回失败退出码。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-tests", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"status": "running", "cases": [], "performance_measured": False}
    try:
        sys.path.insert(0, str(ROOT))
        report["identity"] = installed_identity()
        import docvortex
        from docvortex._compute_backend import backend_info
        from docvortex.document.contracts import HtmlSourceContext
        import onnxruntime
        from loguru import logger

        onnxruntime.disable_telemetry_events()
        logger.disable("docvortex")
        expectations = json.loads((ROOT / "tests/fixtures/nonpdf_wheel_expectations.json").read_text(encoding="utf-8"))
        for case in expectations["cases"]:
            payload = (ROOT / case["path"]).read_bytes()
            assert digest(payload) == case["sha256"], f"Changed input bytes: {case['name']}"
            options = {"file_suffix": case["file_suffix"], "keep_model_json": True}
            if case["source_context"] is not None:
                options["source_context"] = HtmlSourceContext(**case["source_context"])
            captured = full_output(docvortex.parse(payload, **options))
            verify_output(captured, case)
            write_json(output / case["name"] / "output.json", captured)
            report["cases"].append(
                {
                    "name": case["name"],
                    "input_sha256": digest(payload),
                    "full_output_sha256": digest(json.dumps(captured, ensure_ascii=False, sort_keys=True).encode()),
                }
            )
            write_json(output / "report.json", report)
        for name, suffix, payload, expected in generated_inputs():
            result = docvortex.parse(payload, file_suffix=suffix, keep_model_json=True)
            captured = full_output(result)
            verify_output(captured, expected)
            assert captured == full_output(docvortex.parse(payload, file_suffix=suffix, keep_model_json=True))
            write_json(output / name / "output.json", captured)
            row = {"name": name, "input_sha256": digest(payload), "repeat_full_output_equal": True}
            if name in {"generated-ods", "generated-embedded-ofd"}:
                row["exports"] = verify_exports(result, output / name / "exports")
            report["cases"].append(row)
            write_json(output / "report.json", report)
        info = backend_info()
        assert sum(info["native_spreadsheet_region_batches"][:2]) > 0
        assert info["native_rtf_group_scans"][0] > 0
        assert info["native_rtf_group_scans"][3] == 0
        assert all(value > 0 for value in info["native_ofd_table_batches"][:2])
        assert info["native_ofd_table_batches"][4] == 0
        assert all(value > 0 for value in info["native_ofd_glyph_geometry_batches"][:2])
        report["compute_after_public_parse"] = info
        if args.run_tests:
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            command = [
                sys.executable,
                "-I",
                "-m",
                "pytest",
                "-q",
                *[str(ROOT / name) for name in NONPDF_TESTS],
                "--junitxml=" + str(output / "tests.xml"),
            ]
            with (output / "tests.log").open("w", encoding="utf-8") as log:
                completed = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
            report["tests"] = {"command": command, "returncode": completed.returncode}
            assert completed.returncode == 0, "Non-PDF regression failed; see tests.log"
        report["status"] = "passed"
        return 0
    except Exception as exc:
        report.update(
            status="failed", error={"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
        )
        traceback.print_exc()
        return 1
    finally:
        write_json(output / "report.json", report)
        print(
            json.dumps({key: value for key, value in report.items() if key in {"status", "error"}}, ensure_ascii=False),
            flush=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
