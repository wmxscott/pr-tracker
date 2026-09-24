"""The optional TOML settings file.

Every setting has a working default, so a missing file is not an error. A bad
value is reported and replaced by its default rather than stopping anything:
the hook reads this file on every tool call and must never fail over it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

THEMES = ("auto", "light", "dark")

# section -> key -> (default, kind, minimum)
SCHEMA: dict[str, dict[str, tuple[Any, str, float | None]]] = {
    "refresh": {
        "interval_seconds": (300, "int", 1),
        "jitter_seconds": (30, "int", 0),
        "stale_after_multiple": (2, "number", 1),
    },
    "retention": {
        "terminal_ttl_days": (30, "int", 1),
    },
    "notify": {
        "post_tool_use": (True, "bool", None),
        "stop_surface": (True, "bool", None),
    },
    "picker": {
        "theme": ("auto", "theme", None),
    },
}

DEFAULTS: dict[str, Any] = {
    key: spec[0] for section in SCHEMA.values() for key, spec in section.items()
}


def _check(value: Any, kind: str, minimum: float | None) -> str | None:
    if kind == "bool":
        return None if isinstance(value, bool) else "must be true or false"
    if kind == "theme":
        return None if value in THEMES else f"must be one of {', '.join(THEMES)}"
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "must be a number"
    if kind == "int" and not isinstance(value, int):
        return "must be a whole number"
    if minimum is not None and value < minimum:
        return f"must be at least {minimum:g}"
    return None


def parse(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    cfg = dict(DEFAULTS)
    problems = []
    for section, value in data.items():
        if section not in SCHEMA:
            problems.append(f"unknown section [{section}]")
            continue
        if not isinstance(value, dict):
            problems.append(f"[{section}] must be a table")
            continue
        for key, setting in value.items():
            spec = SCHEMA[section].get(key)
            if spec is None:
                problems.append(f"unknown setting {section}.{key}")
                continue
            error = _check(setting, spec[1], spec[2])
            if error:
                problems.append(f"{section}.{key} {error}; using {spec[0]!r}")
            else:
                cfg[key] = setting
    return cfg, problems


def read(path: Path) -> tuple[dict[str, Any], list[str]]:
    """The settings plus a list of problems found in the file."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        return dict(DEFAULTS), []
    except (OSError, tomllib.TOMLDecodeError) as error:
        return dict(DEFAULTS), [f"{path}: {error}; using defaults"]
    cfg, problems = parse(data)
    return cfg, [f"{path}: {problem}" for problem in problems]


def load(path: Path) -> dict[str, Any]:
    return read(path)[0]
