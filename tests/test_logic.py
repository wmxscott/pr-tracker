"""The pure logic: tick gating, check normalisation, event transitions and stack trees."""

import pytest

from pr_tracker.github import normalize_check, rollup_state, row_from_node, stack_entries
from pr_tracker.ledger import build_forest, humanize_age
from pr_tracker.refresh import compute_events, is_tick_due


def no_jitter(a, b):
    return 0


@pytest.mark.parametrize(
    ("last", "now", "rand", "expected"),
    [
        (None, 1000, no_jitter, True),  # never ticked
        (1000, 1100, no_jitter, False),  # too soon
        (1000, 1300, no_jitter, True),  # exactly due
        (1000, 1290, lambda a, b: -30, True),  # jitter pulls it early
        (1000, 1310, lambda a, b: 30, False),  # jitter pushes it late
    ],
)
def test_tick_gating(last, now, rand, expected):
    assert is_tick_due(last, now, 300, 30, rand) is expected


def test_jitter_stays_in_bounds():
    seen = []
    is_tick_due(1000, 1000, 300, 30, lambda a, b: seen.append((a, b)) or 0)
    assert seen == [(-30, 30)]


QA = {"workflowRun": {"workflow": {"name": "QA"}}}


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"__typename": "CheckRun", "name": "lint", "conclusion": "SUCCESS"}, ("lint", "SUCCESS")),
        ({"__typename": "CheckRun", "name": "x", "conclusion": "NEUTRAL"}, ("x", "SUCCESS")),
        ({"__typename": "CheckRun", "name": "e2e", "conclusion": "FAILURE"}, ("e2e", "FAILURE")),
        ({"__typename": "CheckRun", "name": "t", "conclusion": "TIMED_OUT"}, ("t", "FAILURE")),
        ({"__typename": "CheckRun", "name": "x", "conclusion": "SKIPPED"}, ("x", "SKIPPED")),
        ({"__typename": "CheckRun", "name": "c", "conclusion": "CANCELLED"}, ("c", "SKIPPED")),
        ({"__typename": "CheckRun", "name": "y", "status": "IN_PROGRESS"}, ("y", "PENDING")),
        ({"__typename": "StatusContext", "context": "ci", "state": "SUCCESS"}, ("ci", "SUCCESS")),
        ({"__typename": "StatusContext", "context": "ci", "state": "EXPECTED"}, ("ci", "PENDING")),
        ({"__typename": "StatusContext", "context": "ci", "state": "ERROR"}, ("ci", "FAILURE")),
        ({"__typename": "Other"}, None),
        # a bare job name is ambiguous across workflows; qualify it
        (
            {"__typename": "CheckRun", "name": "Deploy", "conclusion": "SUCCESS", "checkSuite": QA},
            ("QA / Deploy", "SUCCESS"),
        ),
        # already qualified, or no workflow at all: leave it alone
        (
            {
                "__typename": "CheckRun",
                "name": "QA / Deploy",
                "conclusion": "SUCCESS",
                "checkSuite": QA,
            },
            ("QA / Deploy", "SUCCESS"),
        ),
        (
            {
                "__typename": "CheckRun",
                "name": "solo",
                "conclusion": "SUCCESS",
                "checkSuite": {"workflowRun": None},
            },
            ("solo", "SUCCESS"),
        ),
    ],
)
def test_normalize_check(node, expected):
    got = normalize_check(node)
    assert (got[:2] if got else None) == expected


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], "NONE"),
        (["SUCCESS", "SUCCESS"], "SUCCESS"),
        (["SUCCESS", "PENDING"], "PENDING"),
        (["SUCCESS", "PENDING", "FAILURE"], "FAILURE"),
        (["SKIPPED"], "SUCCESS"),
    ],
)
def test_rollup(states, expected):
    assert rollup_state([(f"c{i}", s, None) for i, s in enumerate(states)])[0] == expected


def test_row_from_node():
    node = {
        "number": 7,
        "url": "https://github.com/o/r/pull/7",
        "title": "t",
        "body": "x" * 10000,
        "isDraft": True,
        "state": "MERGED",
        "mergeable": "UNKNOWN",
        "baseRefName": "main",
        "headRefName": "feat",
        "reviewDecision": "CHANGES_REQUESTED",
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": "sha",
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "__typename": "CheckRun",
                                        "name": "a",
                                        "conclusion": "SUCCESS",
                                    },
                                    {
                                        "__typename": "CheckRun",
                                        "name": "b",
                                        "conclusion": "FAILURE",
                                    },
                                    {"__typename": "CheckRun", "name": "c"},
                                    {
                                        "__typename": "CheckRun",
                                        "name": "d",
                                        "conclusion": "SKIPPED",
                                    },
                                ]
                            }
                        },
                    }
                }
            ]
        },
    }
    row, checks = row_from_node(node)
    assert len(checks) == 4
    assert len(row["body"]) == 8000
    assert row["state"] == "merged"
    assert row["is_draft"] == 1
    assert row["review_state"] == "changes_requested"
    assert row["head_sha"] == "sha"
    assert (row["checks_rollup"], row["checks_pass"], row["checks_fail"]) == ("FAILURE", 1, 1)
    assert (row["checks_pending"], row["checks_skip"]) == (1, 1)


def test_row_from_a_node_with_no_commits_or_checks():
    row, checks = row_from_node({"number": 1, "commits": None, "reviewDecision": None})
    assert checks == []
    assert row["checks_rollup"] == "NONE"
    assert row["review_state"] == "none"
    assert row["state"] == "open"


def test_stack_entries():
    payload = (
        '{"branches": [{"branch": "a", "pr": {"number": 1, "url": "https://github.com/o/r/pull/1"}},'
        ' {"branch": "b"}, {"branch": "c", "pr": {"url": "https://github.com/o/r/pull/3"}}],'
        ' "stack_metadata": {"name": "my-stack"}}'
    )
    assert stack_entries(payload) == (
        "my-stack",
        ["https://github.com/o/r/pull/1", "", "https://github.com/o/r/pull/3"],
    )
    assert stack_entries('[{"url": "https://github.com/o/r/pull/9"}]') == (
        None,
        ["https://github.com/o/r/pull/9"],
    )
    assert stack_entries("not json") is None
    assert stack_entries('"a string"') is None


def pr(**kw):
    base = {
        "number": 1,
        "state": "open",
        "checks_rollup": "SUCCESS",
        "review_state": "none",
        "head_sha": "abc",
    }
    base.update(kw)
    return base


def kinds(events):
    return [e["kind"] for e in events]


def test_merged_only_on_transition():
    assert "merged" in kinds(compute_events(pr(state="open"), pr(state="merged"), []))
    assert "merged" not in kinds(compute_events(pr(state="merged"), pr(state="merged"), []))
    assert "closed" in kinds(compute_events(pr(state="open"), pr(state="closed"), []))


def test_failing_set_is_the_signature():
    pending, red = pr(checks_rollup="PENDING"), pr(checks_rollup="FAILURE")

    def sig(events):
        return [e["signature"] for e in events if e["kind"] == "checks_failed"]

    assert sig(compute_events(pending, red, ["lint"], [])) == ["lint"]
    # still red, same checks: silent
    assert sig(compute_events(red, red, ["lint"], ["lint"])) == []
    # still red, different checks: report the new set
    assert sig(compute_events(red, red, ["lint", "e2e"], ["lint"])) == ["e2e,lint"]


def test_passed_only_on_transition():
    went_green = compute_events(
        pr(checks_rollup="PENDING"), pr(checks_rollup="SUCCESS", head_sha="deadbeef"), []
    )
    assert [e["signature"] for e in went_green if e["kind"] == "checks_passed"] == ["deadbeef"]
    assert "checks_passed" not in kinds(
        compute_events(pr(checks_rollup="SUCCESS"), pr(checks_rollup="SUCCESS"), [])
    )


def test_review():
    assert "review_changed" in kinds(compute_events(pr(), pr(review_state="changes_requested"), []))
    assert "review_changed" in kinds(compute_events(pr(), pr(review_state="approved"), []))
    # review_required is the resting state, not an event
    assert "review_changed" not in kinds(
        compute_events(pr(), pr(review_state="review_required"), [])
    )
    assert "review_changed" not in kinds(
        compute_events(pr(review_state="approved"), pr(review_state="approved"), [])
    )


def row(id, number, head, base, **kw):
    out = {"id": id, "repo": "o/r", "number": number, "head_ref": head, "base_ref": base}
    out["stack_pos"] = None
    out.update(kw)
    return out


def test_linear_stack():
    rows = [row(1, 10, "feat-a", "main"), row(2, 11, "feat-b", "feat-a"), row(3, 12, "c", "feat-b")]
    got = build_forest(rows, {"o/r": "main"})
    assert [p["number"] for p in got] == [10, 11, 12]
    assert [p["depth"] for p in got] == [0, 1, 2]


def test_orphan_marked():
    assert build_forest([row(1, 10, "feat-b", "gone")], {"o/r": "main"})[0]["orphan"]


def test_default_based_pr_is_not_orphan():
    assert not build_forest([row(1, 10, "feat", "main")], {"o/r": "main"})[0]["orphan"]


def test_stack_pos_overrides_number_order():
    rows = [row(1, 30, "a", "main", stack_pos=1), row(2, 20, "b", "main", stack_pos=0)]
    assert [p["number"] for p in build_forest(rows, {"o/r": "main"})] == [20, 30]


def test_siblings_get_last_flag():
    rows = [row(1, 10, "base", "main"), row(2, 11, "x", "base"), row(3, 12, "y", "base")]
    kids = [p for p in build_forest(rows, {"o/r": "main"}) if p["depth"] == 1]
    assert [p["is_last"] for p in kids] == [False, True]


def test_children_carry_the_orphan_key():
    rows = [row(1, 10, "a", "main"), row(2, 11, "b", "a")]
    assert all("orphan" in p for p in build_forest(rows, {"o/r": "main"}))


def test_repos_do_not_cross():
    rows = [row(1, 10, "feat", "main"), {**row(2, 11, "other", "feat"), "repo": "o/other"}]
    got = build_forest(rows, {"o/r": "main", "o/other": "main"})
    assert [p["depth"] for p in got] == [0, 0]


def test_a_cycle_is_shown_not_dropped():
    rows = [row(1, 10, "a", "b"), row(2, 11, "b", "a")]
    assert sorted(p["number"] for p in build_forest(rows, {"o/r": "main"})) == [10, 11]


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(-5, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3600, "1h"), (86400 * 3, "3d")],
)
def test_humanize_age(seconds, text):
    assert humanize_age(seconds) == text
