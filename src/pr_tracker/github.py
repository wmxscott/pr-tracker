"""Everything that talks to GitHub, through the `gh` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

Check = tuple[str, str, str | None]

# One query per this many PRs. GitHub caps a query's node count, and each PR
# asks for up to 100 check contexts.
BATCH = 50


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str


def available() -> bool:
    return shutil.which("gh") is not None


def gh(args: list[str], cwd: str | None = None, timeout: int = 60) -> Result:
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)
    except FileNotFoundError:
        return Result(127, "", "gh not found on PATH")
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(1, "", str(exc))


PR_FRAGMENT = """
fragment F on PullRequest {
  number url title body isDraft state mergeable baseRefName headRefName reviewDecision
  commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) { nodes {
    __typename
    ... on CheckRun { name conclusion status detailsUrl
      checkSuite { workflowRun { workflow { name } } } }
    ... on StatusContext { context state targetUrl }
  } } } } } }
}
"""


def repo_query(numbers: list[int]) -> str:
    aliases = "\n".join(f"  p{n}: pullRequest(number: {n}) {{ ...F }}" for n in numbers)
    return (
        "query($owner: String!, $name: String!) {\n"
        " repository(owner: $owner, name: $name) {\n"
        f"  defaultBranchRef {{ name }}\n{aliases}\n }}\n}}\n{PR_FRAGMENT}"
    )


@dataclass
class RepoData:
    default_branch: str | None
    nodes: dict[int, dict]
    missing: set[int]
    repo_missing: bool = False


def _not_found(errors: Any) -> set[int]:
    """PR numbers GitHub says do not exist, from a GraphQL `errors` list."""
    missing = set()
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict) or error.get("type") != "NOT_FOUND":
            continue
        path = error.get("path") or []
        if len(path) == 2 and path[0] == "repository" and str(path[1]).startswith("p"):
            number = str(path[1])[1:]
            if number.isdigit():
                missing.add(int(number))
    return missing


def _repo_not_found(errors: Any) -> bool:
    return any(
        isinstance(error, dict)
        and error.get("type") == "NOT_FOUND"
        and error.get("path") == ["repository"]
        for error in (errors if isinstance(errors, list) else [])
    )


def fetch_repo(repo: str, numbers: list[int]) -> tuple[RepoData | None, str]:
    """Batched GraphQL queries for the given PRs in one repo.

    Batching is what actually protects the remote; the tick jitter only spreads
    phase. Returns (None, error) when nothing usable came back, and the caller
    keeps the stored values. A PR that no longer exists makes gh exit non-zero
    but still returns the rest, so a partial response is used, not discarded.
    A repo GitHub can't resolve comes back as `repo_missing`, every PR missing.
    """
    owner, name = repo.split("/", 1)
    data = RepoData(None, {}, set())
    for start in range(0, len(numbers), BATCH):
        chunk = numbers[start : start + BATCH]
        result = gh(
            [
                "api",
                "graphql",
                "-f",
                f"query={repo_query(chunk)}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
            ]
        )
        payload = None
        try:
            payload = json.loads(result.out)
            repository = payload["data"]["repository"]
        except (ValueError, KeyError, TypeError):
            repository = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if repository is None and _repo_not_found(errors):
            return RepoData(None, {}, set(numbers), repo_missing=True), ""
        if not isinstance(repository, dict):
            error = (result.err or result.out).strip().splitlines()
            return None, (error[0] if error else f"gh exited {result.code}")
        data.default_branch = (repository.get("defaultBranchRef") or {}).get("name")
        for number in chunk:
            node = repository.get(f"p{number}")
            if isinstance(node, dict):
                data.nodes[number] = node
        data.missing |= _not_found(payload.get("errors")) - set(data.nodes)
    return data, ""


def normalize_check(node: dict) -> Check | None:
    """A GraphQL check or status node as (name, SUCCESS|FAILURE|PENDING|SKIPPED, url)."""
    kind = node.get("__typename")
    if kind == "CheckRun":
        name = node.get("name") or "check"
        # Job names alone collide across workflows: three "Deploy" rows tell
        # you nothing. GitHub's own UI qualifies them the same way.
        workflow = (
            ((node.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}
        ).get("name")
        if workflow and not name.startswith(f"{workflow} /"):
            name = f"{workflow} / {name}"
        conclusion = (node.get("conclusion") or "").upper()
        if not conclusion:
            state = "PENDING"
        elif conclusion in ("SUCCESS", "NEUTRAL"):
            state = "SUCCESS"
        elif conclusion in ("SKIPPED", "CANCELLED"):
            state = "SKIPPED"
        else:
            state = "FAILURE"
        return name, state, node.get("detailsUrl")
    if kind == "StatusContext":
        name = node.get("context") or "status"
        raw = (node.get("state") or "").upper()
        state = {"SUCCESS": "SUCCESS", "PENDING": "PENDING", "EXPECTED": "PENDING"}.get(
            raw, "FAILURE"
        )
        return name, state, node.get("targetUrl")
    return None


def rollup_state(checks: list[Check]) -> tuple[str, dict[str, int]]:
    counts = {"SUCCESS": 0, "FAILURE": 0, "PENDING": 0, "SKIPPED": 0}
    for _, state, _ in checks:
        counts[state] = counts.get(state, 0) + 1
    if not checks:
        return "NONE", counts
    if counts["FAILURE"]:
        return "FAILURE", counts
    if counts["PENDING"]:
        return "PENDING", counts
    return "SUCCESS", counts


def row_from_node(node: dict) -> tuple[dict, list[Check]]:
    commits = (node.get("commits") or {}).get("nodes") or [{}]
    commit = (commits[0] or {}).get("commit") or {}
    contexts = ((commit.get("statusCheckRollup") or {}).get("contexts") or {}).get("nodes") or []
    checks = [c for c in (normalize_check(n) for n in contexts if isinstance(n, dict)) if c]
    rollup, counts = rollup_state(checks)
    row = {
        "number": node["number"],
        "url": node.get("url"),
        "title": node.get("title"),
        # Bodies can be enormous; the preview only ever shows a screenful.
        "body": (node.get("body") or "")[:8000],
        "head_ref": node.get("headRefName"),
        "base_ref": node.get("baseRefName"),
        "head_sha": commit.get("oid"),
        "state": (node.get("state") or "OPEN").lower(),
        "is_draft": 1 if node.get("isDraft") else 0,
        "mergeable": node.get("mergeable"),
        "review_state": (node.get("reviewDecision") or "").lower() or "none",
        "checks_rollup": rollup,
        "checks_pass": counts["SUCCESS"],
        "checks_fail": counts["FAILURE"],
        "checks_pending": counts["PENDING"],
        "checks_skip": counts["SKIPPED"],
    }
    return row, checks


def stack_entries(payload: str) -> tuple[str | None, list[str]] | None:
    """Parse `gh stack view --json` into (stack id, PR URLs in stack order).

    gh-stack emits {"branches": [{"branch": ..., "pr": {"number", "url", ...}}],
    "stack_metadata": {...}}. The fallbacks cover that shape moving. Returns
    None when the payload is not JSON at all.
    """
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("branches") or data.get("entries") or data.get("stack") or []
    else:
        return None
    if not isinstance(entries, list):
        return None
    meta = data.get("stack_metadata") if isinstance(data, dict) else None
    stack_id = None
    if isinstance(meta, dict):
        stack_id = meta.get("id") or meta.get("uuid") or meta.get("name")
    urls = []
    for entry in entries:
        if not isinstance(entry, dict):
            urls.append("")
            continue
        pr = entry.get("pr") if isinstance(entry.get("pr"), dict) else entry
        urls.append(str(pr.get("url") or entry.get("url") or ""))
    return (str(stack_id) if stack_id else None), urls
