import json

import pytest

from pr_tracker.scrape import (
    command_source,
    is_mcp_create,
    mcp_pr_urls,
    parse_pr_urls,
    tool_output,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", []),
        ("https://github.com/o/r/pull/12", [("o/r", 12, "https://github.com/o/r/pull/12")]),
        (
            "created\nhttps://github.com/o/r/pull/1\nhttps://github.com/o/r/pull/2",
            [
                ("o/r", 1, "https://github.com/o/r/pull/1"),
                ("o/r", 2, "https://github.com/o/r/pull/2"),
            ],
        ),
        (
            "https://github.com/o/r/pull/3 https://github.com/o/r/pull/3",
            [("o/r", 3, "https://github.com/o/r/pull/3")],
        ),
        ("https://github.com/o/r/issues/4", []),
        ("https://github.com/o/r/pull/5/files", [("o/r", 5, "https://github.com/o/r/pull/5")]),
        ("https://api.github.com/repos/o/r/pulls/6", []),
        (
            "https://github.com/my-org/my.repo_x/pull/7",
            [("my-org/my.repo_x", 7, "https://github.com/my-org/my.repo_x/pull/7")],
        ),
    ],
)
def test_parse_pr_urls(text, expected):
    assert parse_pr_urls(text) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("gh pr create --fill", "gh"),
        ("cd /tmp && gh pr create -t x", "gh"),
        ("gh  pr   create", "gh"),
        ("gh stack submit --auto", "stack"),
        ("gh stack push", "stack"),
        (["bash", "-lc", "gh pr create --fill"], "gh"),
        # a PR URL printed by these must never create a row
        ("gh pr view 12 --json url", None),
        ("gh pr list", None),
        ("gh stack view", None),
        ("grep -r 'github.com/o/r/pull' .", None),
        ("echo ghpr create", None),
        ("GH_REPO=o/r gh pr create --fill | tail -1", "gh"),
        ("rtk gh pr create --fill", "gh"),
        ("git push -u origin x; gh pr create --fill", "gh"),
        ("make check && (gh pr create --fill)", "gh"),
        ('url=$(gh pr create --fill) && echo "$url"', "gh"),
        (
            'gh pr create --title "Fix" --body "$(cat <<\'EOF\'\nSee gh stack push.\nEOF\n)"',
            "gh",
        ),
        # only run as a command counts, never text that mentions one
        ('grep -rn "gh pr create" src', None),
        ("grep -rn gh\\ pr\\ create src", None),
        ('echo \'{"tool_input": {"command": "gh pr create"}}\' | pr-tracker hook', None),
        ("cat > t.py <<'EOF'\npayload('gh pr create --fill')\ngh pr create\nEOF\npytest", None),
        ("cat <<-EOF\n\tgh stack submit\n\tEOF", None),
        ("pytest -k 'gh stack push'", None),
        ("ls  # then gh pr create", None),
        # any wrapper counts, with no list of wrappers to keep up to date
        ("gh app-auth exec -- gh pr create --fill", "gh"),
        ("gh app-auth exec --repo github.com/acme/widget -- gh pr create --fill", "gh"),
        ("gh app-auth exec --app-id 123 --installation-id 456 -- gh pr create", "gh"),
        ("gh app-auth exec -R=github.com/acme/widget -- gh pr create", "gh"),
        ("cd repo && gh app-auth exec -- gh pr create --body-file - <<'EOF'\nBody\nEOF", "gh"),
        ("gh app-auth exec -- gh stack submit", "stack"),
        ("rtk gh app-auth exec -- gh pr create", "gh"),
        ("sudo -u bot timeout 60 gh pr create --fill", "gh"),
        ("op run --env-file=.env -- gh pr create --fill", "gh"),
        ("/opt/homebrew/bin/gh pr create --fill", "gh"),
        ("gh \\\n  pr create --fill", "gh"),
        ('echo "gh app-auth exec -- gh pr create"', None),
        ("grep 'gh app-auth exec -- gh pr create' notes.md", None),
        ("gh app-auth exec -- gh pr view 12", None),
        ("gh app-auth exec -- gh api repos/{owner}/{repo}/pulls", None),
        ("rg gh\\ pr\\ create src", None),
        ("cat <<\\EOF > t.sh\ngh pr create --fill\nEOF", None),
        ("xgh pr create", None),
        # a quoted script that a shell runs counts like the command itself
        ('bash -c "gh pr create --fill"', "gh"),
        ("bash -lc 'cd repo && gh pr create --fill'", "gh"),
        ('/bin/zsh -c "gh stack submit"', "stack"),
        ("sh -c 'gh app-auth exec -- gh pr create'", "gh"),
        ('docker exec box sh -c "gh pr create --title \\"x y\\""', "gh"),
        ('eval "gh pr create --fill"', "gh"),
        ("bash -c \"echo 'gh pr create'\"", None),
        ('bash -c "grep -rn \\"gh pr create\\" src"', None),
        ('grep -c "gh pr create" notes.md', None),
        ("echo \"bash -c 'gh pr create'\"", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_command_source(command, expected):
    assert command_source(command) == expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__github__create_pull_request", True),
        ("mcp__plugin_github_github__create_pull_request", True),
        ("create_pull_request", True),
        ("mcp__github__get_pull_request", False),
        ("mcp__github__create_pull_request_review", False),
        ("Bash", False),
        (None, False),
    ],
)
def test_is_mcp_create(name, expected):
    assert is_mcp_create(name) is expected


def test_tool_output_shapes():
    assert tool_output(None) == ""
    assert tool_output("plain") == "plain"
    shell = {"stdout": "https://github.com/o/r/pull/1\n", "stderr": "warn", "interrupted": False}
    assert "pull/1" in tool_output(shell)
    assert "warn" in tool_output(shell)
    blocks = [{"type": "text", "text": "made https://github.com/o/r/pull/2"}]
    assert "pull/2" in tool_output(blocks)


def test_gh_pr_create_output():
    # What `gh pr create` prints on success, including the push chatter on stderr.
    response = {
        "stdout": "https://github.com/o/r/pull/41\n",
        "stderr": "\nCreating pull request for feat into main in o/r\n\n",
    }
    assert parse_pr_urls(tool_output(response)) == [("o/r", 41, "https://github.com/o/r/pull/41")]


def test_gh_stack_submit_output():
    response = {
        "stdout": "Pushed 3 branches\n"
        "  feat-a https://github.com/o/r/pull/10\n"
        "  feat-b https://github.com/o/r/pull/11\n"
        "  feat-c https://github.com/o/r/pull/12\n",
        "stderr": "",
    }
    assert [n for _, n, _ in parse_pr_urls(tool_output(response))] == [10, 11, 12]


def mcp_response(pr: dict) -> list[dict]:
    return [{"type": "text", "text": json.dumps(pr)}]


def test_mcp_create_uses_the_new_prs_own_url():
    pr = {
        "number": 9,
        "url": "https://api.github.com/repos/o/r/pulls/9",
        "html_url": "https://github.com/o/r/pull/9",
        "body": "Follows https://github.com/o/r/pull/3 and https://github.com/o/r/pull/4",
        "base": {"repo": {"html_url": "https://github.com/o/r"}},
    }
    assert mcp_pr_urls(mcp_response(pr)) == [("o/r", 9, "https://github.com/o/r/pull/9")]


def test_mcp_create_minimal_response():
    # Some servers answer with just the URL.
    assert mcp_pr_urls([{"type": "text", "text": '{"url": "https://github.com/o/r/pull/5"}'}]) == [
        ("o/r", 5, "https://github.com/o/r/pull/5")
    ]


def test_mcp_create_unstructured_falls_back_to_the_text():
    response = [{"type": "text", "text": "Created https://github.com/o/r/pull/6"}]
    assert mcp_pr_urls(response) == [("o/r", 6, "https://github.com/o/r/pull/6")]
    assert mcp_pr_urls({"content": response}) == [("o/r", 6, "https://github.com/o/r/pull/6")]
    assert mcp_pr_urls(None) == []
