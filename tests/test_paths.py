from pathlib import Path

from pr_tracker import paths


def test_defaults_match_the_original_ledger_location():
    # The ledger has always lived here; existing data must carry over.
    where = paths.resolve({"HOME": "/h"})
    assert where.config == Path("/h/.config/pr-tracker/settings.toml")
    assert where.db == Path("/h/.local/state/pr-tracker/prs.db")
    assert where.lock == Path("/h/.local/state/pr-tracker/refresh.lock")


def test_xdg_dirs():
    where = paths.resolve({"HOME": "/h", "XDG_CONFIG_HOME": "/c", "XDG_STATE_HOME": "/s"})
    assert where.config == Path("/c/pr-tracker/settings.toml")
    assert where.db == Path("/s/pr-tracker/prs.db")


def test_relative_xdg_dirs_are_ignored():
    # The XDG spec says relative paths are invalid and must be ignored.
    where = paths.resolve({"HOME": "/h", "XDG_CONFIG_HOME": "rel", "XDG_STATE_HOME": "rel"})
    assert where.config == Path("/h/.config/pr-tracker/settings.toml")
    assert where.db == Path("/h/.local/state/pr-tracker/prs.db")


def test_explicit_overrides_win():
    where = paths.resolve(
        {
            "HOME": "/h",
            "XDG_STATE_HOME": "/s",
            "PR_TRACKER_CONFIG": "/etc/prt.toml",
            "PR_TRACKER_STATE_DIR": "/var/prt",
        }
    )
    assert where.config == Path("/etc/prt.toml")
    assert where.db == Path("/var/prt/prs.db")


def test_theme_file():
    assert paths.theme_file({"HOME": "/h"}) == Path(
        "/h/.local/share/theme-monitor/theme-change.trigger"
    )
    assert paths.theme_file({"HOME": "/h", "XDG_DATA_HOME": "/d"}) == Path(
        "/d/theme-monitor/theme-change.trigger"
    )
