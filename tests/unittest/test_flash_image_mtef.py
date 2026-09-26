from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from _image_mtef_test_utils import (
    apps_mfcc_comment,
    apps_mfcc_comments,
    baseline_wmf_comment,
    build_baseline_only_gif,
    build_gif_with_extensions,
    build_gif_with_mtef,
    build_wmf,
    gif_baseline_extension,
    gif_mtef_extension,
    pre6_wmf_comment,
)
from _mtef_test_utils import formula_corpus
from _mtef_v5_test_utils import v5_equation, v5_formula_corpus, v5_text

from docvortex.analyzers.native.office.equation import image as image_equation_module
from docvortex.analyzers.native.office.equation.image import OfficeImageEquationDecoder, decode_image_embedded_equation
from docvortex.analyzers.native.office.errors import LegacyOfficeResourceLimitError


@pytest.mark.parametrize(
    ("mtef", "expected"),
    [
        (formula_corpus()[1][1], formula_corpus()[1][2]),
        (v5_formula_corpus()[1][1], v5_formula_corpus()[1][2]),
    ],
    ids=["v3", "v5"],
)
def test_pre6_wmf_comment_decodes_mtef_versions(
    mtef: bytes,
    expected: str,
) -> None:
    """验证带/不带 placeable header 的 pre-6 WMF 可恢复 v3/v5。"""

    comment = pre6_wmf_comment(mtef)

    assert (
        decode_image_embedded_equation(
            build_wmf([comment]),
            part_name="image.wmf",
        )
        == expected
    )
    assert (
        decode_image_embedded_equation(
            build_wmf([comment], placeable=True),
            content_type="image/x-wmf",
        )
        == expected
    )


@pytest.mark.parametrize(
    "signature",
    [
        "Design Science, Inc./MTEF",
        "Wiris/MTEF/MathType7",
        "Acme/MTEF",
        "Design Science, Inc.",
        "Wiris",
    ],
)
def test_apps_mfcc_single_and_multi_chunk_signatures(signature: str) -> None:
    """验证规范及历史 signature 的单/多 chunk AppsMFCC。"""

    _name, mtef, expected = v5_formula_corpus()[2]

    single = build_wmf(
        apps_mfcc_comments(
            mtef,
            chunk_size=len(mtef),
            signature=signature,
        )
    )
    multiple = build_wmf(
        apps_mfcc_comments(
            mtef,
            chunk_size=7,
            signature=signature,
        )
    )

    assert decode_image_embedded_equation(single) == expected
    assert decode_image_embedded_equation(multiple) == expected


def test_apps_mfcc_reassembles_mtef_larger_than_32k() -> None:
    """验证 AppsMFCC 可跨 WMF 单 comment 上限重组大型 MTEF。"""

    mtef = v5_equation(v5_text("x" * 7000))
    assert len(mtef) > 0x7FFE
    image = build_wmf(
        apps_mfcc_comments(
            mtef,
            chunk_size=30_000,
        )
    )

    assert decode_image_embedded_equation(image) == "x" * 7000


@pytest.mark.parametrize(
    ("mtef", "expected"),
    [
        (formula_corpus()[0][1], formula_corpus()[0][2]),
        (v5_formula_corpus()[0][1], v5_formula_corpus()[0][2]),
    ],
    ids=["v3", "v5"],
)
def test_gif_mathtype_001_decodes_across_subblocks(
    mtef: bytes,
    expected: str,
) -> None:
    """验证 GIF MathType/001 跨 sub-block 恢复 v3/v5。"""

    image = build_gif_with_mtef(
        mtef,
        chunk_size=5,
        include_baseline=True,
    )

    assert decode_image_embedded_equation(image) == expected


def test_wmf_gif_decode_full_v3_v5_formula_corpora() -> None:
    """验证两种图片载体复用全部既有 v3/v5 公式语料。"""

    for _name, mtef, expected in formula_corpus():
        assert decode_image_embedded_equation(build_wmf([pre6_wmf_comment(mtef)])) == expected
        assert decode_image_embedded_equation(build_gif_with_mtef(mtef, chunk_size=5)) == expected
    for _name, mtef, expected in v5_formula_corpus():
        assert decode_image_embedded_equation(build_wmf(apps_mfcc_comments(mtef, chunk_size=7))) == expected
        assert decode_image_embedded_equation(build_gif_with_mtef(mtef, chunk_size=5)) == expected


def test_baseline_comments_and_ordinary_images_are_ignored() -> None:
    """验证 WMF baseline、GIF/002 和普通图片不被误判为公式。"""

    assert decode_image_embedded_equation(build_wmf([baseline_wmf_comment(12)])) is None
    assert decode_image_embedded_equation(build_baseline_only_gif()) is None
    assert decode_image_embedded_equation(b"\x89PNG\r\n\x1a\n") is None


def test_conflicting_wmf_and_gif_candidates_fail_closed() -> None:
    """验证同一图片中互相冲突的公式 candidates 整体回退。"""

    v3 = formula_corpus()[0][1]
    v5 = v5_formula_corpus()[1][1]
    wmf = build_wmf(
        [
            pre6_wmf_comment(v3),
            apps_mfcc_comment(v5, total_length=len(v5)),
        ]
    )
    gif = build_gif_with_extensions(
        [
            gif_mtef_extension(v3),
            gif_mtef_extension(v5),
        ]
    )

    assert decode_image_embedded_equation(wmf) is None
    assert decode_image_embedded_equation(gif) is None


@pytest.mark.parametrize(
    "image",
    [
        build_wmf([pre6_wmf_comment(bytes([4, 1, 0, 3, 5, 0]))]),
        build_wmf(
            [
                apps_mfcc_comment(
                    b"truncated",
                    total_length=100,
                )
            ]
        ),
        build_gif_with_mtef(bytes([4, 1, 0, 3, 5, 0])),
        build_gif_with_mtef(v5_formula_corpus()[0][1])[:-1],
    ],
)
def test_unsupported_or_truncated_image_comments_fail_closed(
    image: bytes,
) -> None:
    """验证 v4、缺 chunk 和截断 GIF 不输出部分公式。"""

    assert decode_image_embedded_equation(image) is None


def test_reordered_apps_chunks_and_strict_image_prefixes_fail_closed() -> None:
    """验证 AppsMFCC 乱序及任意 WMF/GIF 截断不会输出部分公式。"""

    mtef = v5_formula_corpus()[1][1]
    comments = apps_mfcc_comments(mtef, chunk_size=7)
    reordered = build_wmf(list(reversed(comments)))
    valid_wmf = build_wmf(comments)
    valid_gif = build_gif_with_mtef(mtef, chunk_size=5)

    assert decode_image_embedded_equation(reordered) is None
    assert all(decode_image_embedded_equation(valid_wmf[:end]) is None for end in range(len(valid_wmf)))
    assert all(decode_image_embedded_equation(valid_gif[:end]) is None for end in range(len(valid_gif)))


def test_image_equation_decoder_cache_and_total_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证相同图片缓存不重复计费，唯一公式 candidate 受累计预算限制。"""

    first_mtef = v5_formula_corpus()[0][1]
    second_mtef = v5_formula_corpus()[1][1]
    first = build_gif_with_mtef(first_mtef)
    second = build_gif_with_mtef(second_mtef)
    monkeypatch.setattr(
        image_equation_module,
        "MAX_EQUATION_CANDIDATE_TOTAL_BYTES",
        len(first_mtef) + len(second_mtef) - 1,
    )
    decoder = OfficeImageEquationDecoder()

    assert decoder.decode(first) == v5_formula_corpus()[0][2]
    assert decoder.decode(first) == v5_formula_corpus()[0][2]
    with pytest.raises(LegacyOfficeResourceLimitError, match="max_equation_candidate_total_bytes"):
        decoder.decode(second)


def test_ordinary_gif_does_not_consume_equation_candidate_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 baseline/普通 GIF 不会占用 MTEF candidate 累计预算。"""

    monkeypatch.setattr(image_equation_module, "MAX_EQUATION_CANDIDATE_TOTAL_BYTES", 1)
    decoder = OfficeImageEquationDecoder()

    assert decoder.decode(build_baseline_only_gif()) is None
    assert decoder.candidate_total_bytes == 0


def test_image_equation_record_limit_raises_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 WMF/GIF record 超限抛稳定 resource-limit 错误。"""

    monkeypatch.setattr(image_equation_module, "MAX_PICTURE_RECORDS", 1)
    image = build_gif_with_mtef(v5_formula_corpus()[0][1], chunk_size=1)

    with pytest.raises(LegacyOfficeResourceLimitError, match="max_picture_records"):
        decode_image_embedded_equation(image)


def test_gif_subblocks_do_not_consume_picture_record_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证大量 GIF sub-block 只计为一个 extension record。"""

    monkeypatch.setattr(image_equation_module, "MAX_PICTURE_RECORDS", 4)
    image = build_gif_with_mtef(v5_formula_corpus()[0][1], chunk_size=1)

    assert decode_image_embedded_equation(image) == v5_formula_corpus()[0][2]


def test_non_equation_gif_subblocks_do_not_consume_candidate_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证普通 GIF application extension 的 sub-block 不计入公式预算。"""

    monkeypatch.setattr(image_equation_module, "MAX_PICTURE_RECORDS", 4)
    monkeypatch.setattr(image_equation_module, "MAX_EQUATION_CANDIDATE_TOTAL_BYTES", 1)
    image = build_gif_with_extensions([gif_baseline_extension(b"baseline" * 32, chunk_size=1)])

    decoder = OfficeImageEquationDecoder()
    assert decoder.decode(image) is None
    assert decoder.candidate_total_bytes == 0


def test_gif_subblocks_have_an_independent_structural_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 GIF sub-block 使用独立结构上限，不复用 picture record 配额。"""

    monkeypatch.setattr(image_equation_module, "MAX_RECORDS", 1)
    image = build_gif_with_mtef(v5_formula_corpus()[0][1], chunk_size=1)

    with pytest.raises(LegacyOfficeResourceLimitError, match="GIF sub-block count"):
        decode_image_embedded_equation(image)


def test_large_ordinary_gif_frame_ignores_equation_byte_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证普通帧数据超过公式单候选预算时，仍可遍历且不消耗公式预算。"""
    output = BytesIO()
    Image.frombytes("L", (16, 16), bytes(range(256))).save(output, format="GIF")
    image = output.getvalue()
    monkeypatch.setattr(image_equation_module, "MAX_ENTRY_BYTES", 1)
    monkeypatch.setattr(image_equation_module, "MAX_EQUATION_CANDIDATE_TOTAL_BYTES", 0)
    decoder = OfficeImageEquationDecoder()

    assert decoder.decode(image) is None
    assert decoder.candidate_total_bytes == 0


@pytest.mark.parametrize("with_equation", [False, True])
def test_large_gif_extensions_preserve_small_equations(
    monkeypatch: pytest.MonkeyPatch, with_equation: bool
) -> None:
    """验证超限非公式扩展不占预算，大 GIF 内的小公式仍可识别且缓存不重复计费。"""
    _name, mtef, expected = v5_formula_corpus()[0]
    monkeypatch.setattr(image_equation_module, "MAX_ENTRY_BYTES", len(mtef))
    monkeypatch.setattr(image_equation_module, "MAX_EQUATION_CANDIDATE_TOTAL_BYTES", len(mtef))
    extensions = [gif_baseline_extension(b"baseline" * len(mtef))]
    if with_equation:
        extensions.append(gif_mtef_extension(mtef))
    image = build_gif_with_extensions(extensions)
    decoder = OfficeImageEquationDecoder()

    assert len(image) > image_equation_module.MAX_ENTRY_BYTES
    assert decoder.decode(image) == (expected if with_equation else None)
    assert decoder.decode(image) == (expected if with_equation else None)
    assert decoder.candidate_total_bytes == (len(mtef) if with_equation else 0)


@pytest.mark.parametrize("budget_scope", ["entry", "same_image", "previous_image"])
def test_gif_equation_budget_checked_before_payload_copy(
    monkeypatch: pytest.MonkeyPatch, budget_scope: str
) -> None:
    """验证单候选、同图和跨图累计超限都在复制公式子块之前失败。"""
    _name, mtef, expected = v5_formula_corpus()[0]
    assert len(mtef) <= 255
    decoder = OfficeImageEquationDecoder()
    extension = gif_mtef_extension(mtef)
    extensions = [extension]
    previous_bytes = 0
    if budget_scope == "entry":
        monkeypatch.setattr(image_equation_module, "MAX_ENTRY_BYTES", len(mtef) - 1)
        message = "max_entry_bytes"
    else:
        monkeypatch.setattr(image_equation_module, "MAX_EQUATION_CANDIDATE_TOTAL_BYTES", 2 * len(mtef) - 1)
        message = "max_equation_candidate_total_bytes"
        if budget_scope == "same_image":
            extensions.append(extension)
        else:
            assert decoder.decode(build_gif_with_extensions(extensions)) == expected
            previous_bytes = len(mtef)
            extensions.insert(0, gif_baseline_extension(b"different image"))
    image = build_gif_with_extensions(extensions)
    forbidden_start = image.rindex(mtef)

    class GuardedBytes(bytes):
        """禁止读取已超出预算的子块，证明检查发生于载荷复制之前。"""

        def __getitem__(self, key: int | slice) -> int | bytes:
            """拦截超限载荷切片，其他字节访问沿用原有行为。"""
            if isinstance(key, slice) and key.start == forbidden_start and key.stop == forbidden_start + len(mtef):
                pytest.fail("超限公式载荷不应被复制")
            return super().__getitem__(key)

    with pytest.raises(LegacyOfficeResourceLimitError, match=message) as caught:
        decoder.decode(GuardedBytes(image))
    assert caught.value.code == "resource_limit"
    assert decoder.candidate_total_bytes == previous_bytes


def test_wmf_whole_image_limit_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 GIF 限制调整不会放宽 WMF 整图资源上限。"""
    image = build_wmf([baseline_wmf_comment()])
    monkeypatch.setattr(image_equation_module, "MAX_ENTRY_BYTES", len(image) - 1)
    with pytest.raises(LegacyOfficeResourceLimitError, match="office image exceeds max_entry_bytes"):
        decode_image_embedded_equation(image)
