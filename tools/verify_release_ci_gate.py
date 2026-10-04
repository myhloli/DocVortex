"""Release access control verification: release commit itself has a successful CI, or it is only different from the latest green light ancestor by version number assignment.

push with pure version number bump does not trigger CI (ci.yml configures paths-ignore for version.py),
At this time, release is allowed to borrow the conclusion of the latest green light run on the first parent chain, but the borrowing must also meet:
release commit is included in the history of origin/main, and the changes between two commit only involve version files.
And the AST of this file is completely consistent except for the top-level __version__ string literal, to avoid entrainment of unused CI
Check published content.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import urllib.parse
import urllib.request

CI_WORKFLOW_PATH = ".github/workflows/ci.yml"
VERSION_FILE = "src/docvortex/version.py"
RELEASE_SAFE_FILES = frozenset({VERSION_FILE})
MAIN_REF = "origin/main"
ANCESTOR_LIMIT = 16


def successful_push_shas(api_url: str, repository: str, token: str, branch: str = "main", per_page: int = 30) -> set[str]:
    """Query the CI run triggered by the push event on the branch, and return the set of head SHA whose conclusion is successful."""
    query = urllib.parse.urlencode({"branch": branch, "event": "push", "per_page": str(per_page)})
    request = urllib.request.Request(
        f"{api_url}/repos/{repository}/actions/runs?{query}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request) as response:
        payload = json.load(response)
    return {
        run["head_sha"]
        for run in payload.get("workflow_runs", [])
        if run.get("path") == CI_WORKFLOW_PATH and run.get("status") == "completed" and run.get("conclusion") == "success"
    }


def first_parent_chain(repo: str, sha: str, limit: int = ANCESTOR_LIMIT) -> list[str]:
    """Returns a list of commits (including itself) along the first parent chain starting from sha, subject to limit."""
    output = subprocess.run(
        ["git", "-C", repo, "rev-list", "--first-parent", "-n", str(limit), sha],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return output.split()


def changed_files(repo: str, base: str, target: str) -> list[str]:
    """List the changed file paths between two commit."""
    output = subprocess.run(
        ["git", "-C", repo, "diff", "--name-only", base, target],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    return [line for line in output.splitlines() if line]


def reachable_from(repo: str, sha: str, reference: str = MAIN_REF) -> bool:
    """Determine whether commit is included in the history of reference (default origin/main)."""
    return (
        subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", sha, reference],
            capture_output=True,
        ).returncode
        == 0
    )


def _signature_without_version_assignment(source: str) -> str | None:
    """Parse the version module, remove the only top-level __version__ string assignment and return AST signature; structural exception returns None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    remaining = []
    assignments = 0
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__version__"
        ):
            if not (isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
                return None
            assignments += 1
            continue
        remaining.append(node)
    if assignments != 1:
        return None
    return ast.dump(ast.Module(body=remaining, type_ignores=[]))


def version_assignment_only_change(repo: str, base: str, target: str) -> tuple[bool, str]:
    """Verify the version module of commit twice. Only the top-level __version__ string literal is different, and the AST of the other statements are completely consistent."""
    signatures = []
    for revision in (base, target):
        show = subprocess.run(
            ["git", "-C", repo, "show", f"{revision}:{VERSION_FILE}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if show.returncode != 0:
            return False, f"{VERSION_FILE} is missing at {revision[:12]}"
        signature = _signature_without_version_assignment(show.stdout)
        if signature is None:
            return False, f"{VERSION_FILE} at {revision[:12]} lacks exactly one top-level __version__ string assignment"
        signatures.append(signature)
    if signatures[0] != signatures[1]:
        return False, f"{VERSION_FILE} changed beyond the top-level __version__ assignment since {base[:12]}"
    return True, f"{VERSION_FILE} only changes the top-level __version__ string"


def verify(repo: str, release_sha: str, green_shas: set[str], main_ref: str = MAIN_REF) -> tuple[bool, str]:
    """Verify the released access control and return (whether passed, explanation)."""
    chain = first_parent_chain(repo, release_sha)
    if release_sha in green_shas:
        return True, "release commit has a successful CI run on main"
    green_ancestor = next((sha for sha in chain[1:] if sha in green_shas), None)
    if green_ancestor is None:
        return (
            False,
            f"no successful CI run on main for the release commit or its {len(chain) - 1} nearest first-parent ancestors",
        )
    if not reachable_from(repo, release_sha, main_ref):
        return False, f"release commit is not reachable from {main_ref}; push the version bump to main before releasing"
    changed = changed_files(repo, green_ancestor, release_sha)
    unsafe = sorted(set(changed) - RELEASE_SAFE_FILES)
    if unsafe:
        return (
            False,
            f"release commit changes files beyond {sorted(RELEASE_SAFE_FILES)} since green ancestor {green_ancestor[:12]}: {unsafe}",
        )
    assignment_only, reason = version_assignment_only_change(repo, green_ancestor, release_sha)
    if not assignment_only:
        return False, reason
    return True, f"release commit only changes the top-level __version__ string since green ancestor {green_ancestor[:12]}"


def main() -> None:
    passed, message = verify(
        os.getcwd(),
        os.environ["GITHUB_SHA"],
        successful_push_shas(os.environ["GITHUB_API_URL"], os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_TOKEN"]),
    )
    print(message)
    if not passed:
        raise SystemExit(message)


if __name__ == "__main__":
    main()
