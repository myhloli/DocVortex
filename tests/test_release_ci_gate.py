"""Verify the release access control: the own green light passes directly; the borrowed green light ancestor must be on main, only the version file is different, and only the top-level __version__ literal is changed."""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess

import pytest

from tools.verify_release_ci_gate import (
    ANCESTOR_LIMIT,
    changed_files,
    first_parent_chain,
    successful_push_shas,
    verify,
)

_VERSION_MODULE = '__version__ = "0.4.7"\n\n__all__ = ["__version__"]\n'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "commit", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _write(repo: Path, relative: str, content: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)


def _bump(repo: Path, version: str = "0.4.8") -> None:
    _write(repo, "src/docvortex/version.py", f'__version__ = "{version}"\n\n__all__ = ["__version__"]\n')


def _push_main(repo: Path) -> None:
    """Simulate pushing main and current HEAD to the remote end and synchronize the reference of origin/main."""
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    _write(repo, "src/docvortex/version.py", _VERSION_MODULE)
    _commit(repo, "initial")
    return repo


def test_release_commit_green_passes(repo: Path) -> None:
    release = _commit(repo, "release")
    passed, _ = verify(str(repo), release, {release})
    assert passed


def test_version_only_bump_borrows_green_ancestor(repo: Path) -> None:
    green = _commit(repo, "merge pull request")
    _bump(repo)
    release = _commit(repo, "Prepare release")
    _push_main(repo)
    passed, message = verify(str(repo), release, {green})
    assert passed
    assert green[:12] in message


def test_changes_beyond_version_file_are_rejected(repo: Path) -> None:
    green = _commit(repo, "merge pull request")
    _bump(repo)
    _write(repo, "src/docvortex/parser.py", "x = 1\n")
    release = _commit(repo, "Prepare release with extra change")
    _push_main(repo)
    passed, message = verify(str(repo), release, {green})
    assert not passed
    assert "src/docvortex/parser.py" in message


def test_smuggled_statement_in_version_file_is_rejected(repo: Path) -> None:
    green = _commit(repo, "merge pull request")
    _write(
        repo,
        "src/docvortex/version.py",
        '__version__ = "0.4.8"\n\n__all__ = ["__version__"]\n\nimport os\nos.system("echo arbitrary code")\n',
    )
    release = _commit(repo, "Prepare release")
    _push_main(repo)
    passed, message = verify(str(repo), release, {green})
    assert not passed
    assert "__version__" in message


def test_collateral_change_in_version_file_is_rejected(repo: Path) -> None:
    green = _commit(repo, "merge pull request")
    _write(repo, "src/docvortex/version.py", '__version__ = "0.4.8"\n\n__all__ = ["__version__", "extra"]\n')
    release = _commit(repo, "Prepare release")
    _push_main(repo)
    passed, message = verify(str(repo), release, {green})
    assert not passed
    assert "__version__" in message


def test_non_literal_version_assignment_is_rejected(repo: Path) -> None:
    green = _commit(repo, "merge pull request")
    _write(repo, "src/docvortex/version.py", '__version__ = compute_version()\n\n__all__ = ["__version__"]\n')
    release = _commit(repo, "Prepare release")
    _push_main(repo)
    passed, _ = verify(str(repo), release, {green})
    assert not passed


def test_stale_green_ancestor_is_rejected(repo: Path) -> None:
    green = _commit(repo, "old green")
    _write(repo, "src/docvortex/parser.py", "x = 1\n")
    _commit(repo, "unverified change")
    _bump(repo)
    release = _commit(repo, "Prepare release")
    _push_main(repo)
    passed, message = verify(str(repo), release, {green})
    assert not passed
    assert "src/docvortex/parser.py" in message


def test_no_green_ancestor_fails(repo: Path) -> None:
    _commit(repo, "merge pull request")
    _bump(repo)
    release = _commit(repo, "Prepare release")
    passed, _ = verify(str(repo), release, set())
    assert not passed


def test_green_off_first_parent_chain_is_not_borrowed(repo: Path) -> None:
    _commit(repo, "base")
    _git(repo, "checkout", "-b", "feature")
    feature_green = _commit(repo, "feature head with green PR run")
    _git(repo, "checkout", "main")
    _bump(repo)
    release = _commit(repo, "Prepare release")
    passed, _ = verify(str(repo), release, {feature_green})
    assert not passed


def test_side_branch_release_is_rejected(repo: Path) -> None:
    green = _commit(repo, "green on main")
    _git(repo, "checkout", "-b", "side")
    _bump(repo)
    release = _commit(repo, "side-branch bump never pushed to main")
    _git(repo, "checkout", "main")
    _push_main(repo)
    passed, message = verify(str(repo), release, {green})
    assert not passed
    assert "origin/main" in message


def test_first_parent_chain_is_bounded(repo: Path) -> None:
    for index in range(ANCESTOR_LIMIT + 5):
        _commit(repo, f"commit {index}")
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert len(first_parent_chain(str(repo), head, ANCESTOR_LIMIT)) == ANCESTOR_LIMIT
    assert first_parent_chain(str(repo), head, ANCESTOR_LIMIT)[0] == head


def test_changed_files_lists_relative_paths(repo: Path) -> None:
    base = _commit(repo, "base")
    _bump(repo)
    _write(repo, "docs/note.md", "# Note\n")
    tip = _commit(repo, "two files")
    assert changed_files(str(repo), base, tip) == ["docs/note.md", "src/docvortex/version.py"]


def test_successful_push_shas_filters_run_state(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "workflow_runs": [
            {"head_sha": "a" * 40, "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "success"},
            {"head_sha": "b" * 40, "path": ".github/workflows/ci.yml", "status": "completed", "conclusion": "failure"},
            {"head_sha": "c" * 40, "path": ".github/workflows/ci.yml", "status": "in_progress", "conclusion": None},
            {"head_sha": "d" * 40, "path": ".github/workflows/publish.yml", "status": "completed", "conclusion": "success"},
        ]
    }
    captured = {}

    def _fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr("tools.verify_release_ci_gate.urllib.request.urlopen", _fake_urlopen)
    shas = successful_push_shas("https://api.github.com", "owner/repo", "token-value")
    assert shas == {"a" * 40}
    assert captured["url"] == "https://api.github.com/repos/owner/repo/actions/runs?branch=main&event=push&per_page=30"
    assert captured["auth"] == "Bearer token-value"
