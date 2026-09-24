"""Where pr-tracker keeps its config and its ledger."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

APP = "pr-tracker"


def _base(env: Mapping[str, str], var: str, fallback: Path) -> Path:
    value = env.get(var, "")
    return Path(value) if value.startswith("/") else fallback


def _home(env: Mapping[str, str]) -> Path:
    value = env.get("HOME", "")
    return Path(value) if value.startswith("/") else Path.home()


def config_file(env: Mapping[str, str] = os.environ) -> Path:
    """`$PR_TRACKER_CONFIG`, else `$XDG_CONFIG_HOME/pr-tracker/settings.toml`."""
    if env.get("PR_TRACKER_CONFIG"):
        return Path(env["PR_TRACKER_CONFIG"]).expanduser()
    return _base(env, "XDG_CONFIG_HOME", _home(env) / ".config") / APP / "settings.toml"


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """`$PR_TRACKER_STATE_DIR`, else `$XDG_STATE_HOME/pr-tracker`.

    `$XDG_STATE_HOME` defaults to `~/.local/state`.
    """
    if env.get("PR_TRACKER_STATE_DIR"):
        return Path(env["PR_TRACKER_STATE_DIR"]).expanduser()
    return _base(env, "XDG_STATE_HOME", _home(env) / ".local/state") / APP


def theme_file(env: Mapping[str, str] = os.environ) -> Path:
    """The file theme-monitor keeps the system appearance in."""
    base = _base(env, "XDG_DATA_HOME", _home(env) / ".local/share")
    return base / "theme-monitor/theme-change.trigger"


@dataclass(frozen=True)
class Paths:
    config: Path
    state: Path

    @property
    def db(self) -> Path:
        return self.state / "prs.db"

    @property
    def lock(self) -> Path:
        return self.state / "refresh.lock"


def resolve(env: Mapping[str, str] = os.environ) -> Paths:
    return Paths(config_file(env), state_dir(env))
