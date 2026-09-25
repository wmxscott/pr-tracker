"""Finding pull request URLs in what a tool printed, and deciding whether it created them."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/pull/(\d+)")

# Commands that mean "a PR was just created", only where a command starts:
# after a separator, env assignments or a wrapper. A PR URL printed by
# `gh pr view`, or by any command that merely mentions `gh pr create`, such as
# a grep or a test fixture, must never create a row.
_START = (
    r"(?:^|[;&|(\n`])\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:(?:env|command|exec|time|nohup|rtk)\s+)*"
)
CREATE_RE = re.compile(_START + r"gh\s+pr\s+create\b")
STACK_RE = re.compile(_START + r"gh\s+stack\s+(submit|push)\b")
HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")

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


def _code(text: str, i: int = 0, nested: bool = False) -> tuple[str, int]:
    """`text` from `i` with quoted strings, heredoc bodies and comments blanked out.

    What `$(...)` runs inside double quotes is kept, as `url="$(gh pr create)"`
    runs gh. With `nested`, stops after the `)` that closes the substitution.
    """
    out: list[str] = []
    heredocs: list[tuple[bool, str]] = []
    depth, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            out.append(" ")
            i += 2
        elif c == "'":
            end = text.find("'", i + 1)
            out.append(" ")
            i = n if end < 0 else end + 1
        elif c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 2
                elif text.startswith("$(", i):
                    inner, i = _code(text, i + 2, nested=True)
                    out.append(f" $({inner}) ")
                else:
                    i += 1
            out.append(" ")
            i += 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif match := HEREDOC_RE.match(text, i):
            heredocs.append((match[1] == "-", match[3]))
            out.append(" ")
            i = match.end()
        elif c == "\n" and heredocs:
            out.append("\n")
            i += 1
            for strip, word in heredocs:
                while i < n:
                    end = text.find("\n", i)
                    line = text[i : n if end < 0 else end]
                    i = n if end < 0 else end + 1
                    if (line.lstrip("\t") if strip else line) == word:
                        break
            heredocs = []
        elif c == ")" and nested and depth == 0:
            return "".join(out), i + 1
        else:
            depth += (c == "(") - (c == ")")
            out.append(c)
            i += 1
    return "".join(out), i


def command_source(command: Any) -> str | None:
    """'gh' | 'stack' | None: what kind of creation this command was, if any."""
    # An argv list may carry the script as one argument, as in `bash -lc "..."`.
    texts = [command_text(command)]
    if isinstance(command, list):
        texts += [str(part) for part in command]
    code = "\n".join(_code(text)[0] for text in texts)
    if STACK_RE.search(code):
        return "stack"
    if CREATE_RE.search(code):
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
