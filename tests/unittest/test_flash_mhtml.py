"""MHTML 容器、资源隔离、正文和公共导出的行为回归。"""

from __future__ import annotations

from email import policy
from email.message import EmailMessage
import importlib
from pathlib import Path
import socket

from click.testing import CliRunner
from jsonschema import Draft202012Validator
import pytest

import docvortex
from docvortex.document import mhtml as archive_module
from docvortex.document.mhtml import MhtmlArchive, MhtmlParseError, MhtmlResourceLimitError
from docvortex.cli import main
from docvortex.document.contracts import HtmlSourceContext
from docvortex.document.detection import guess_suffix_by_bytes, guess_suffix_by_path
from docvortex.document.filetypes import file_type_for_extension, mime_type_for_extension
from docvortex.errors import InvalidRequestError
from docvortex.schema import MiddleJson, ModelJson
from _mhtml_test_utils import build_mhtml_fixture, image_bytes, mime_part, related


def markdown(data: bytes, **kwargs) -> tuple[str, docvortex.DocumentResult]:
    """经公开接口返回 Markdown 与原始结果，方便同时验证资源和元数据。"""
    result = docvortex.parse(data, **kwargs)
    return docvortex.render_artifact(result.middle_json, "markdown", assets=result.assets).content.decode(), result


@pytest.mark.parametrize("extension", ["mhtml", "mht", "MHTML"])
def test_detection_public_api_cli_and_schema(extension: str, tmp_path: Path) -> None:
    """文件名、字节、显式格式和 CLI 统一规范为 mhtml，JSON 符合公开协议。"""
    data = build_mhtml_fixture()
    source = tmp_path / f"sample.{extension}"
    source.write_bytes(data)
    assert guess_suffix_by_bytes(data) == guess_suffix_by_path(source) == "mhtml"
    assert file_type_for_extension(extension) == "mhtml"
    assert mime_type_for_extension(extension) == "multipart/related"
    result = docvortex.parse(source, keep_model_json=True)
    assert result.middle_json.metadata.file_suffix == "mhtml"
    assert len(result.middle_json.pages) == 1 and len(result.assets) == 1
    assert docvortex.parse(data, file_suffix="mhtml").to_dict() == result.to_dict()
    for document, kind in [(result.model_json, ModelJson), (result.middle_json, MiddleJson)]:
        Draft202012Validator(kind.model_json_schema()).validate(document.to_dict())
    output = tmp_path / "result.md"
    invoked = CliRunner().invoke(main, ["convert", str(source), "-o", str(output)])
    assert invoked.exit_code == 0, invoked.output
    assert "中文正文" in output.read_text(encoding="utf-8")
    with pytest.raises(InvalidRequestError, match="only for PDF"):
        docvortex.parse(source, page_range="1")


@pytest.mark.parametrize("encoding", ["base64", "quoted-printable", "8bit", "binary"])
@pytest.mark.parametrize("charset", ["utf-8", "gb18030"])
def test_transfer_encoding_and_charset(encoding: str, charset: str) -> None:
    """MIME 声明字符集正确传给 HTML，传输编码不污染中文。"""
    data = related(mime_part("<p>中文 café</p>".encode(charset), charset=charset, encoding=encoding)).as_bytes()
    text, _ = markdown(data)
    assert "中文 café" in text


def test_start_alternative_and_ignore_other_html() -> None:
    """start 可选择后置 alternative，并只解析最后一个受支持 HTML。"""
    alternative = EmailMessage(policy=policy.SMTP)
    alternative.set_type("multipart/alternative")
    alternative["Content-ID"] = "<root>"
    alternative.attach(mime_part(b"wrong", "text/plain"))
    alternative.attach(mime_part(b"<p>old html</p>"))
    alternative.attach(mime_part(b"<p>chosen html</p>"))
    message = related(mime_part(b"<p>advertisement</p>"), alternative, start="root")
    text, _ = markdown(message.as_bytes())
    assert "chosen html" in text and "old html" not in text and "advertisement" not in text


def test_invalid_root_boundary_and_decode() -> None:
    """错误 start、缺少 HTML、破损边界和主文档编码均产生明确错误。"""
    invalid = [
        related(mime_part(b"<p>unused</p>"), start="missing").as_bytes(),
        related(mime_part(b"plain", "text/plain")).as_bytes(),
        b"MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=x\r\n\r\nbroken",
    ]
    root = mime_part(b"<p>body</p>")
    root.set_payload("not@@base64")
    invalid.append(related(root).as_bytes())
    for data in invalid:
        with pytest.raises(MhtmlParseError):
            docvortex.parse(data, file_suffix="mhtml")


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_BYTES", "MAX_DECODED_BYTES", "MAX_PARTS", "MAX_DEPTH"])
def test_archive_limits(limit: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """原始字节、累计解码、部件数和嵌套深度都有独立预算。"""
    # 转换器在导入时保存读取预算；先加载，避免 xdist 的冷进程把临时值
    # 固化到另一个模块，导致 monkeypatch 恢复后仍把后续归档截成两字节。
    importlib.import_module("docvortex.analyzers.native.mhtml.converter")
    monkeypatch.setattr(archive_module, limit, 1)
    with pytest.raises(MhtmlResourceLimitError):
        docvortex.parse(build_mhtml_fixture())


def test_locations_cid_css_base_and_picture() -> None:
    """归档 CSS 控制可见性，CID 与完整 URL 复用图片，picture 恢复实际保存的候选。"""
    image = image_bytes()
    body = b"""<html><head><base href="https://site.test/assets/"><link rel="stylesheet" href="cid:style"></head><body><p class="hidden">HIDDEN</p><p>VISIBLE</p><img src="one.png?v=1"><img src="cid:shared"><picture><source srcset="saved.png 1x"><img src="missing.png" alt="COVER"></picture></body></html>"""
    data = related(
        mime_part(body, location="https://site.test/article/index.html"),
        mime_part(b".hidden{display:none}", "text/css", location="cid:style"),
        mime_part(image, "image/png", location="https://site.test/assets/one.png?v=1", cid="shared"),
        mime_part(image_bytes("blue"), "image/png", location="https://site.test/assets/saved.png"),
    ).as_bytes()
    text, result = markdown(data)
    assert "VISIBLE" in text and "HIDDEN" not in text
    assert "COVER" in text and "missing.png" not in text
    values = list(result.assets.values())
    assert len(values) == 3 and len(set(values)) == 2 and not result.diagnostics
    assert values[0] is values[1]
    assert image in list(result.assets.values())


def test_uri_query_percent_encoding_and_fragments() -> None:
    """查询参数和编码差异不能误配，哈希路由子页也不能被误判为重复资源。"""
    message = related(
        mime_part(b"<p>body</p>", location="https://a.test/sub/index.html"),
        mime_part(image_bytes(), "image/png", location="a%2eb.png?v=1"),
        mime_part(b"<p>frame1</p>", location="https://frame.test/#/one"),
        mime_part(b"<p>frame2</p>", location="https://frame.test/#/two"),
    )
    archive = MhtmlArchive(message.as_bytes())
    part = archive.find("a%2eb.png?v=1#fragment")
    assert part is not None
    assert part.media_type == "image/png" and part.charset is None
    assert part.source_uri == "https://a.test/sub/a%2eb.png?v=1"
    assert archive.decode(part) == image_bytes()
    assert archive.find("a.b.png?v=1") is None
    assert archive.find("a%2eb.png?v=2") is None
    assert archive.find("https://frame.test/#/one") is not archive.find("https://frame.test/#/two")


def test_duplicate_resource_identifier_is_rejected() -> None:
    """同一容器中重复的资源地址或 ID 不允许随机覆盖。"""
    for key in ("cid", "location"):
        parts = [mime_part(image_bytes(), "image/png", **{key: "same"}) for _ in range(2)]
        with pytest.raises(MhtmlParseError, match="Duplicate"):
            MhtmlArchive(related(mime_part(b"<p>body</p>"), *parts).as_bytes())


def test_nested_scope_does_not_leak_child_resources() -> None:
    """外层正文不能引用内层 iframe 的图片，内层允许读取外层共享资源。"""
    child = related(mime_part(b"<p>frame</p>"), mime_part(image_bytes(), "image/png", cid="private"))
    child["Content-ID"] = "<child>"
    archive = MhtmlArchive(related(mime_part(b"<p>root</p>"), child).as_bytes())
    assert archive.find("cid:private") is None
    nested = related(child, mime_part(image_bytes("blue"), "image/png", cid="outer"), start="child")
    archive = MhtmlArchive(nested.as_bytes())
    assert archive.find("cid:private") is not None and archive.find("cid:outer") is not None


def test_optional_bad_resources_and_no_external_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失或损坏部件可诊断降级，即使传入本地根也不能读取归档外资源。"""
    invalid = mime_part(b"bad image", "image/png", cid="bad")
    broken = mime_part(b"broken", "text/css", cid="css")
    broken.set_payload("bad@@base64")
    data = related(
        mime_part(
            b'<link rel="stylesheet" href="cid:css"><p>content</p><img src="cid:bad" alt="BAD"><img src="local.png" alt="LOCAL"><img src="https://remote.test/a.png" alt="REMOTE">'
        ),
        invalid,
        broken,
    ).as_bytes()

    def forbidden(*args, **kwargs):
        """任何网络连接或路径资源读取都应使该离线测试失败。"""
        raise AssertionError("external IO")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    text, result = markdown(data, source_context=HtmlSourceContext(local_resource_root=tmp_path))
    assert "content" in text and "BAD" in text and "LOCAL" in text
    assert "https://remote.test/a.png" in text and not result.assets
    assert {d.code for d in result.diagnostics} == {"mhtml_resource_invalid", "mhtml_resource_missing"}


def test_metadata_subject_and_bundle(tmp_path: Path) -> None:
    """标题缺失使用 Subject，保存时间不冒充发布时间，结果包完整恢复素材。"""
    message = related(mime_part(b'<p>body</p><img src="cid:figure">'), mime_part(image_bytes(), "image/png", cid="figure"))
    message["Subject"] = "归档标题"
    message["Date"] = "Wed, 23 Sep 2026 03:00:00 +0000"
    data = message.as_bytes()
    props = docvortex.extract_metadata(data).metadata.document
    assert props.title == "归档标题" and props.created_at is None
    result = docvortex.parse(data, keep_model_json=True)
    assert result.middle_json.metadata.document == props
    path = tmp_path / "result.zip"
    result.save_bundle(path)
    loaded = docvortex.load_bundle(path)
    assert loaded.to_dict() == result.to_dict()
    assert set(loaded.assets.values()) == set(result.assets.values())
    assert all(loaded.assets[key] == value for key, value in result.assets.items())


def test_comments_pruned_only_for_independent_article() -> None:
    """独立正文旁的评论和头像被过滤，论坛帖子及正文相关单词仍保留。"""
    prose = "An article about comments and commentary. " * 10
    body = f'<main><img src="cid:cover" alt="COVER"><article><h1>Title</h1><p>{prose}</p></article><div class="Comments-container"><p>REMOVE COMMENT</p><img src="cid:avatar" alt="AVATAR"></div></main>'
    text, result = markdown(related(mime_part(body.encode()), mime_part(image_bytes(), "image/png", cid="cover")).as_bytes())
    assert prose.strip() in text and "COVER" in text
    assert "REMOVE COMMENT" not in text and "AVATAR" not in text
    assert len(result.assets) == 1 and not result.diagnostics
    forum = '<main><article class="comments"><p>FIRST POST</p></article><article class="comments"><p>SECOND POST</p></article></main>'
    text, _ = markdown(related(mime_part(forum.encode())).as_bytes())
    assert "FIRST POST" in text and "SECOND POST" in text


def test_nested_container_source_and_relative_locations() -> None:
    """主文档与资源的相对地址继承嵌套 MIME 容器基址。"""
    child = related(
        mime_part(b'<p>nested</p><img src="figure.png">', location="index.html"),
        mime_part(image_bytes(), "image/png", location="figure.png"),
        location="sub/",
    )
    message = related(child, location="https://site.test/root/")
    archive = MhtmlArchive(message.as_bytes())
    assert archive.source_context.source_uri == "https://site.test/root/sub/index.html"
    text, result = markdown(message.as_bytes())
    assert "nested" in text and len(result.assets) == 1 and not result.diagnostics


def test_relative_root_without_site_does_not_invent_remote_links() -> None:
    """无原站来源时保持相对链接，内部索引基址不得泄露到输出。"""
    message = related(
        mime_part(b'<a href="next.html">NEXT</a><img src="figure.png">', location="sub/index.html"),
        mime_part(image_bytes(), "image/png", location="figure.png"),
    )
    text, result = markdown(message.as_bytes())
    assert "next.html" in text and "mhtml.invalid" not in text
    assert len(result.assets) == 1 and not result.diagnostics


def test_source_context_and_snapshot_fallback() -> None:
    """容器来源优先于快照地址，快照优先于调用方来源。"""
    message = related(mime_part(b'<a href="next">NEXT</a>'))
    context = HtmlSourceContext(source_uri="https://caller.test/a/")
    assert MhtmlArchive(message.as_bytes(), context).source_context.source_uri == context.source_uri
    message["Snapshot-Content-Location"] = "https://snapshot.test/b/"
    assert MhtmlArchive(message.as_bytes(), context).source_context.source_uri == "https://snapshot.test/b/"
    message["Content-Location"] = "https://container.test/c/"
    assert MhtmlArchive(message.as_bytes(), context).source_context.source_uri == "https://container.test/c/"


def test_decode_cache_and_optional_resource_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """重复引用不重复消耗解码预算，不同资源累计超限则停止解析。"""
    archive = MhtmlArchive(
        related(mime_part(b"<p>body</p>"), mime_part(b"1234", cid="one"), mime_part(b"5678", cid="two")).as_bytes()
    )
    monkeypatch.setattr(archive_module, "MAX_DECODED_BYTES", len(archive.html) + 4)
    one = archive.find("cid:one")
    assert archive.decode(one) == archive.decode(one) == b"1234"
    with pytest.raises(MhtmlResourceLimitError):
        archive.decode(archive.find("cid:two"))


def test_archived_images_and_stylesheets_obey_html_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """归档图片与样式不能绕过已有 HTML 单资源限额。"""
    from docvortex.analyzers.native.mhtml import resources
    from docvortex.analyzers.native.html import resources as html_resources
    from docvortex.analyzers.native.html.errors import HtmlResourceLimitError

    data = related(
        mime_part(b'<p>body</p><img src="cid:image">'), mime_part(image_bytes(), "image/png", cid="image")
    ).as_bytes()
    monkeypatch.setattr(resources, "MAX_HTML_IMAGE_BYTES", 1)
    with pytest.raises(HtmlResourceLimitError):
        markdown(data)
    data = related(
        mime_part(b'<link rel="stylesheet" href="cid:css"><p>body</p>'), mime_part(b"p {color: red;}", "text/css", cid="css")
    ).as_bytes()
    monkeypatch.setattr(html_resources, "MAX_HTML_STYLESHEET_BYTES", 1)
    with pytest.raises(HtmlResourceLimitError):
        markdown(data)


def test_mhtml_reuses_table_formula_and_footnote_projection() -> None:
    """表格中的归档图片、数学公式和脚注沿用 HTML 投影而不丢失。"""
    body = b"""<html><body><h1>Document</h1><p>Formula <math data-tex="x^2"></math> and <a href="#note">note</a>.</p><table><tr><td>Cell</td><td><img src="cid:figure" alt="table figure"></td></tr></table><aside id="note" role="doc-footnote">Footnote content</aside></body></html>"""
    text, result = markdown(related(mime_part(body), mime_part(image_bytes(), "image/png", cid="figure")).as_bytes())
    assert "x^2" in text and "Cell" in text and "Footnote content" in text
    assert len(result.assets) == 1 and not result.diagnostics


@pytest.mark.parametrize("class_name", ["Comments-container", "CommentContent", "post_comments"])
def test_html_comment_filter_preserves_cover_and_forum(class_name: str) -> None:
    """共享筛选规则也作用于普通 HTML，同时保护多个独立论坛文章。"""
    prose = "Article discussion about commentary. " * 10
    body = f'<main><article><p>{prose}</p></article><div class="{class_name}"><p>NOISE</p><img src="https://site.test/avatar.png"></div></main>'
    text, _ = markdown(body.encode(), file_suffix="html")
    assert "NOISE" not in text and prose.strip() in text
    forum = f'<main><article><p>{prose}</p></article><article><p>{prose}</p></article><div class="{class_name}"><p>KEEP DISCUSSION</p><img src="https://site.test/avatar.png"></div></main>'
    text, _ = markdown(forum.encode(), file_suffix="html")
    assert "KEEP DISCUSSION" in text
