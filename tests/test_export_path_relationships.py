"""冻结导出路径关系的词法契约、冲突优先级及公开写出入口的预检边界。"""

from __future__ import annotations

from itertools import permutations
from pathlib import Path, PurePosixPath, PureWindowsPath
import random

import pytest

from docvortex.export import middle


def _legacy_error(paths: list[str], path_type) -> str | None:
    """冻结优化前双循环，保留原始键去重及最早父路径、最早子路径的报错顺序。"""
    parts_by_path = {path: path_type(path).parts for path in paths}
    for path, parts in parts_by_path.items():
        for other, other_parts in parts_by_path.items():
            if path == other or len(parts) >= len(other_parts):
                continue
            if other_parts[: len(parts)] == parts:
                return f"Export file path conflicts with a required directory: {path} -> {other}"
    return None


def _actual_error(paths: list[str]) -> str | None:
    """只截取目标校验器的既有 ValueError，其他异常继续使测试失败。"""
    try:
        middle._validate_export_path_relationships(paths)
    except ValueError as error:
        return str(error)
    return None


@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath], ids=["posix", "windows"])
@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["document.md"],
        ["images/a.png", "images/b.png", "document.md"],
        ["a", "ab/image.png", "a.png", "aa/b"],
        ["one/a/b.png", "one/b.png", "two/a.png"],
        ["images/a.png", "images/a.png", "images/b.png"],
        ["a", "./a", "a/", "a//"],
        ["images/a.png", "images//a.png", "./images/a.png"],
        ["", ".", "./", "./."],
        ["A", "a/file.png"],
    ],
)
def test_nonconflicting_paths_preserve_lexical_contract(monkeypatch: pytest.MonkeyPatch, path_type, paths: list[str]) -> None:
    """同级、相似前缀、重复及等长规范化别名均不构成严格父子冲突，也不改输入。"""
    monkeypatch.setattr(middle, "Path", path_type)
    before = list(paths)
    assert _actual_error(paths) is None
    assert paths == before


@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath], ids=["posix", "windows"])
@pytest.mark.parametrize(
    ("paths", "parent", "child"),
    [
        (["images", "images/a.png"], "images", "images/a.png"),
        (["images/a.png", "images"], "images", "images/a.png"),
        (["tree", "tree/a/b/c.png"], "tree", "tree/a/b/c.png"),
        (["tree/a/b", "tree/a", "tree"], "tree/a", "tree/a/b"),
        (["z", "a", "a/first", "z/later"], "z", "z/later"),
        (["z/first", "a/first", "a", "z"], "a", "a/first"),
        (["tree", "tree/z", "tree/a"], "tree", "tree/z"),
        (["tree", "tree/x", "tree", "tree/y"], "tree", "tree/x"),
        (["tree/a", "./tree", "tree", "tree/z"], "./tree", "tree/a"),
        (["tree", "tree//a", "tree/a"], "tree", "tree//a"),
        (["", ".", "a"], "", "a"),
        (["a", ".", ""], ".", "a"),
        (["a/../x", "a"], "a", "a/../x"),
    ],
)
def test_conflict_message_preserves_parent_then_child_order(
    monkeypatch: pytest.MonkeyPatch, path_type, paths: list[str], parent: str, child: str
) -> None:
    """报错保留旧输入次序和原始字符串；不改为最短父路径、最早遇见子路径或排序优先。"""
    monkeypatch.setattr(middle, "Path", path_type)
    before = list(paths)
    expected = f"Export file path conflicts with a required directory: {parent} -> {child}"
    assert _actual_error(paths) == expected
    assert paths == before


@pytest.mark.parametrize(
    ("paths", "posix_pair", "windows_pair"),
    [
        ([r"a\b", "a/b/c"], None, (r"a\b", "a/b/c")),
        ([r"a\b", "a/b"], None, None),
        (["C:", "C:images/a.png"], None, ("C:", "C:images/a.png")),
        (["C:/images", "C:/images/a.png"], ("C:/images", "C:/images/a.png"), ("C:/images", "C:/images/a.png")),
        (["C:/images", "c:/images/a.png"], None, None),
        (["C:/images", "C:images/a.png"], None, None),
        ([r"\\server\share", r"\\server\share\image.png"], None, (r"\\server\share", r"\\server\share\image.png")),
        ([r"\\SERVER\share", r"\\server\share\image.png"], None, None),
    ],
)
@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath], ids=["posix", "windows"])
def test_path_parts_flavors_preserve_existing_case_and_anchor_rules(
    monkeypatch: pytest.MonkeyPatch, path_type, paths: list[str], posix_pair, windows_pair
) -> None:
    """直接比较 parts 元组，保持反斜线、盘符和 UNC 根的既有大小写与锚点行为。"""
    monkeypatch.setattr(middle, "Path", path_type)
    pair = posix_pair if path_type is PurePosixPath else windows_pair
    expected = None if pair is None else f"Export file path conflicts with a required directory: {pair[0]} -> {pair[1]}"
    assert _actual_error(paths) == expected


@pytest.mark.parametrize("path_type", [PurePosixPath, PureWindowsPath], ids=["posix", "windows"])
def test_small_path_sets_match_frozen_legacy_oracle(monkeypatch: pytest.MonkeyPatch, path_type) -> None:
    """用小规模排列及确定性混合输入验证父子优先级，不依赖被测算法构造预期。"""
    monkeypatch.setattr(middle, "Path", path_type)
    cases = [list(items) for items in permutations(["a", "./a", "a/x", "b", "b/y"])]
    randomizer = random.Random(20261024)
    candidates = [
        "",
        ".",
        "./",
        "a",
        "./a",
        "a//",
        "a/x",
        "a/x/y",
        "a/../z",
        "ab/x",
        "A",
        "A/x",
        "b",
        "b/y",
        "中文",
        "中文/图片.png",
        r"a\x",
        r"a\x\y",
        "C:",
        "C:/a",
        "c:/a/x",
        r"\\server\share",
        r"\\server\share\image.png",
        "/",
        "//",
        "/a",
        "//a/x",
    ]
    for _ in range(80):
        cases.append([randomizer.choice(candidates) for _ in range(randomizer.randrange(10))])
    for paths in cases:
        original = list(paths)
        assert _actual_error(paths) == _legacy_error(paths, path_type), (path_type.__name__, paths)
        assert paths == original


@pytest.mark.parametrize("entry", ["write_artifact", "save_bundle"])
@pytest.mark.parametrize("existing_output", [False, True])
def test_public_exports_reject_parent_file_conflict_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, existing_output: bool
) -> None:
    """补齐产物和 bundle 入口的主文件占目录冲突，失败前不得创建目录或进入提交事务。"""
    from docvortex.assets import AssetStore
    from docvortex.export import bundle, files
    from docvortex.render.contracts import RenderFormat
    from docvortex.result import DocumentResult, RenderArtifact
    from docvortex.schema import MiddleJson

    output = tmp_path / "output"
    if existing_output:
        output.mkdir()
        (output / "keep.txt").write_bytes(b"untouched")
    main_name = "document.md" if entry == "write_artifact" else "manifest.json"
    assets = AssetStore({f"{main_name}/sidecar.png": b"image-payload"})

    def forbid_commit(*args, **kwargs) -> None:
        """路径关系冲突必须在文件事务前被发现，不能以回滚结果代替预检。"""
        raise AssertionError("File commit must not run after a path relationship conflict")

    owner = files if entry == "write_artifact" else bundle
    monkeypatch.setattr(owner, "_commit_export_files", forbid_commit)
    expected = f"Export file path conflicts with a required directory: {main_name} -> {main_name}/sidecar.png"
    with pytest.raises(ValueError) as caught:
        if entry == "write_artifact":
            RenderArtifact(b"document", RenderFormat.MARKDOWN, "text/markdown", assets).write(output / main_name)
        else:
            document = MiddleJson(
                pages=[],
                is_full_document=True,
                metadata={"file_suffix": "html", "producer": {"name": "test", "version": "1"}},
            )
            DocumentResult(document, assets).save_bundle(output)
    assert str(caught.value) == expected
    if existing_output:
        assert {path.name: path.read_bytes() for path in output.iterdir()} == {"keep.txt": b"untouched"}
    else:
        assert not output.exists()
