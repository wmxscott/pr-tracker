"""Finding pull request URLs in what a tool printed, and deciding whether it created them."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/pull/(\d+)")

# Commands that mean "a PR was just created". A PR URL merely printed by
# `gh pr view` or `gh pr list` must never create a row.
CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")
STACK_RE = re.compile(r"\bgh\s+stack\s+(submit|push)\b")

# GitHub MCP servers name the tool `create_pull_request`; agents prefix it
# with the server name, as in `mcp__github__create_pull_request`.
MCP_CREATE_RE = re.compile(r"create_pull_request$")


def parse_pr_urls(text: str) -> list[tuple[str, int, str]]:
    """Every distinct GitHub PR URL in `text`, as (repo, number, url), in first-seen order."""
    seen, out = set(), []
    for match in PR_URL_RE.finditer(text or ""):
        repo, number = match.group(1), int(match.group(2))
        if (repo, number) in seen:
            continue
        seen.add((repo, number))
        out.append((repo, number, f"https://github.com/{repo}/pull/{number}"))
    return out


def command_text(command: Any) -> str:
    """A shell command as one string. Some agents send argv lists rather than strings."""
    if isinstance(command, str):
        return command
    if isinstance(command, list):
        return " ".join(str(part) for part in command)
    return ""


def command_source(command: Any) -> str | None:
    """'gh' | 'stack' | None: what kind of creation this command was, if any."""
    text = command_text(command)
    if STACK_RE.search(text):
        return "stack"
    if CREATE_RE.search(text):
        return "gh"
    return None


def is_mcp_create(tool_name: Any) -> bool:
    return isinstance(tool_name, str) and bool(MCP_CREATE_RE.search(tool_name))


def _strings(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, list | tuple):
        for item in value:
            _strings(item, out)


def tool_output(response: Any) -> str:
    """Every string anywhere in a tool's response, joined. We only ever regex over it.

    Shell tools return `{"stdout": ..., "stderr": ...}`, MCP tools a list of
    content blocks whose text is often JSON again, and some agents a bare
    string. Walking every string covers all of them without knowing which.
    """
    if response is None:
        return ""
    parts: list[str] = []
    _strings(response, parts)
    return "\n".join(parts) if parts else json.dumps(response)


def _url_fields(value: Any, out: list[str]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "{[":
            with contextlib.suppress(ValueError):
                _url_fields(json.loads(text), out)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ("html_url", "url") and isinstance(item, str):
                out.append(item)
            else:
                _url_fields(item, out)
    elif isinstance(value, list | tuple):
        for item in value:
            _url_fields(item, out)


def mcp_pr_urls(response: Any) -> list[tuple[str, int, str]]:
    """The PR an MCP `create_pull_request` call made.

    Its response carries the new PR's body, and a body that mentions another
    PR must not record that one too, so the PR's own `html_url` wins. Anything
    unstructured falls back to every URL in the text.
    """
    fields: list[str] = []
    _url_fields(response, fields)
    found = parse_pr_urls("\n".join(fields))
    return found or parse_pr_urls(tool_output(response))
