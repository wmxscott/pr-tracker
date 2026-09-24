from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from pr_tracker import ledger, paths

SCRUBBED = (
    "PR_TRACKER_CONFIG",
    "PR_TRACKER_STATE_DIR",
    "PR_TRACKER_SESSION_ID",
    "PR_TRACKER_DISABLE",
    "PR_TRACKER_THEME",
    "CLAUDE_SESSION_ID",
    "FZF_COLUMNS",
    "FZF_PREVIEW_COLUMNS",
    "FZF_INPUT_STATE",
    "FZF_SELECT_COUNT",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)

# A stand-in for `gh`. It logs every call and answers from a JSON spec:
#   {"graphql": {"owner/name": {"code": 0, "stdout": {...}, "stderr": ""}},
#    "pr_view": {"code": 0, "stdout": {...}},
#    "stack_view": {"<cwd>": {"code": 0, "stdout": {...}}}}
# Anything without an entry fails, so no test can reach the real GitHub.
FAKE_GH = """#!{python}
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "cwd": os.getcwd()}}) + "\\n")
try:
    with open(os.environ["FAKE_GH_SPEC"]) as f:
        spec = json.load(f)
except (KeyError, OSError, ValueError):
    spec = {{}}

answer = None
if args[:2] == ["api", "graphql"]:
    fields = dict(a.split("=", 1) for a in args[2:] if "=" in a and not a.startswith("-"))
    answer = spec.get("graphql", {{}}).get(fields.get("owner", "") + "/" + fields.get("name", ""))
elif args[:2] == ["pr", "view"]:
    answer = spec.get("pr_view")
elif args[:2] == ["stack", "view"]:
    answer = spec.get("stack_view", {{}}).get(os.path.realpath(os.getcwd()))

if answer is None:
    sys.stderr.write("fake gh: no answer for " + " ".join(args) + "\\n")
    sys.exit(1)
out = answer.get("stdout", "")
sys.stdout.write(out if isinstance(out, str) else json.dumps(out))
sys.stderr.write(answer.get("stderr", ""))
sys.exit(answer.get("code", 0))
"""


@dataclass
class FakeGh:
    spec_path: Path
    log_path: Path

    def set(self, **spec) -> None:
        self.spec_path.write_text(json.dumps(spec))

    def calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    """Every test gets its own HOME, XDG dirs and a fake `gh` first on PATH."""
    for var in SCRUBBED:
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local/state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local/share"))
    monkeypatch.setenv("PR_TRACKER_THEME", "light")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH.format(python=sys.executable))
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    fake = FakeGh(tmp_path / "gh-spec.json", tmp_path / "gh-log.jsonl")
    monkeypatch.setenv("FAKE_GH_SPEC", str(fake.spec_path))
    monkeypatch.setenv("FAKE_GH_LOG", str(fake.log_path))
    return fake


@pytest.fixture
def fake_gh(isolated: FakeGh) -> FakeGh:
    return isolated


@pytest.fixture
def where() -> paths.Paths:
    resolved = paths.resolve(os.environ)
    assert "/home/" in str(resolved.state), "tests must never touch the real state directory"
    return resolved


def url(number: int, repo: str = "o/r") -> str:
    return f"https://github.com/{repo}/pull/{number}"


def add_pr(
    db: Path,
    number: int,
    session: str = "s",
    repo: str = "o/r",
    **fields,
) -> int:
    """A tracked PR, as if it had been recorded and then refreshed."""
    ledger.record(db, session, None, "gh", [(repo, number, url(number, repo))])
    conn = ledger.connect(db)
    try:
        pr_id = conn.execute(
            "SELECT id FROM prs WHERE repo = ? AND number = ?", (repo, number)
        ).fetchone()["id"]
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            conn.execute(f"UPDATE prs SET {assignments} WHERE id = ?", (*fields.values(), pr_id))
        conn.commit()
        return pr_id
    finally:
        conn.close()


def add_event(db: Path, pr_id: int, session: str = "s", signature: str = "lint") -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO events (pr_id, session_id, kind, signature, detail, created_at)"
        " VALUES (?, ?, 'checks_failed', ?, ?, 1)",
        (pr_id, session, signature, f"checks failing: {signature}"),
    )
    conn.commit()
    conn.close()


def check_run(name: str, conclusion: str | None, status: str = "COMPLETED") -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "conclusion": conclusion,
        "status": status,
        "detailsUrl": f"https://example.com/{name}",
    }


def pr_node(
    number: int,
    repo: str = "o/r",
    state: str = "OPEN",
    checks: list[dict] | None = None,
    review: str | None = None,
    head: str | None = None,
    base: str = "main",
    sha: str = "abc123",
    title: str | None = None,
) -> dict:
    return {
        "number": number,
        "url": url(number, repo),
        "title": title or f"PR {number}",
        "body": f"body of {number}",
        "isDraft": False,
        "state": state,
        "mergeable": "MERGEABLE",
        "baseRefName": base,
        "headRefName": head or f"branch-{number}",
        "reviewDecision": review,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": sha,
                        "statusCheckRollup": {"contexts": {"nodes": checks or []}},
                    }
                }
            ]
        },
    }


def graphql(*nodes: dict, default_branch: str = "main", errors: list | None = None) -> dict:
    repository: dict = {"defaultBranchRef": {"name": default_branch}}
    for node in nodes:
        repository[f"p{node['number']}"] = node
    body: dict = {"data": {"repository": repository}}
    if errors:
        body["errors"] = errors
    return {"code": 1 if errors else 0, "stdout": body}
