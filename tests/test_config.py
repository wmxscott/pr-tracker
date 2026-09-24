from pathlib import Path

import pytest

from pr_tracker import config


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "settings.toml"
    path.write_text(text)
    return path


def test_missing_file_is_all_defaults(tmp_path):
    cfg, problems = config.read(tmp_path / "nope.toml")
    assert cfg == config.DEFAULTS
    assert problems == []


def test_defaults():
    assert config.DEFAULTS == {
        "interval_seconds": 300,
        "jitter_seconds": 30,
        "stale_after_multiple": 2,
        "terminal_ttl_days": 30,
        "post_tool_use": True,
        "stop_surface": True,
        "theme": "auto",
    }


def test_sections_are_read(tmp_path):
    cfg, problems = config.read(
        write(
            tmp_path,
            """
[refresh]
interval_seconds = 600
jitter_seconds = 0
stale_after_multiple = 1.5

[retention]
terminal_ttl_days = 7

[notify]
post_tool_use = false
stop_surface = false

[picker]
theme = "dark"
""",
        )
    )
    assert problems == []
    assert cfg == {
        "interval_seconds": 600,
        "jitter_seconds": 0,
        "stale_after_multiple": 1.5,
        "terminal_ttl_days": 7,
        "post_tool_use": False,
        "stop_surface": False,
        "theme": "dark",
    }


def test_the_sample_in_the_readme_parses():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    block = readme.split("```toml\n# ~/.config/pr-tracker/settings.toml\n", 1)[1]
    cfg, problems = config.parse(__import__("tomllib").loads(block.split("```", 1)[0]))
    assert problems == []
    assert cfg == config.DEFAULTS


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("[refresh]\ninterval_seconds = 0", "refresh.interval_seconds must be at least 1"),
        ("[refresh]\ninterval_seconds = '5m'", "refresh.interval_seconds must be a number"),
        ("[refresh]\ninterval_seconds = 1.5", "must be a whole number"),
        ("[refresh]\njitter_seconds = -1", "must be at least 0"),
        ("[notify]\npost_tool_use = 1", "notify.post_tool_use must be true or false"),
        ("[picker]\ntheme = 'blue'", "picker.theme must be one of auto, light, dark"),
        ("[refresh]\nbogus = 1", "unknown setting refresh.bogus"),
        ("[bogus]\nx = 1", "unknown section [bogus]"),
        ("refresh = 5", "[refresh] must be a table"),
        ("[refresh]\ninterval_seconds = true", "must be a number"),
    ],
)
def test_bad_values_fall_back_to_defaults(tmp_path, text, problem):
    cfg, problems = config.read(write(tmp_path, text))
    assert len(problems) == 1
    assert problem in problems[0]
    assert cfg == config.DEFAULTS


def test_one_bad_value_keeps_the_good_ones(tmp_path):
    cfg, problems = config.read(
        write(tmp_path, "[refresh]\ninterval_seconds = 60\njitter_seconds = 'x'")
    )
    assert cfg["interval_seconds"] == 60
    assert cfg["jitter_seconds"] == 30
    assert len(problems) == 1


def test_broken_toml(tmp_path):
    cfg, problems = config.read(write(tmp_path, "[refresh\n"))
    assert cfg == config.DEFAULTS
    assert "using defaults" in problems[0]
    assert config.load(tmp_path / "settings.toml") == config.DEFAULTS
