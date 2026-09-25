"""`prs`: an fzf picker over the tracked pull requests.

Read-only over the ledger, apart from `R`, which refreshes and then marks
every pending event delivered.

Two independent filters, both shown as pills in the header:

  s   this session only  <->  every tracked PR
  a   open -> needs attention -> merged/closed -> all

Both are applied inside fzf via `transform`, which rewrites the list and the
header in place. Relaunching fzf per keystroke would lose the cursor and
flash the screen. The filter state lives in a scratch JSON file for the life
of the process, because a bound command is a fresh process every time.

The list has focus by default: search starts disabled so j/k scroll. `/`
hands the keyboard to the query input, where j and k are just letters again,
and esc hands it back.

The session comes from `--session` or PR_TRACKER_SESSION_ID. A launcher that
opens the picker in a popup should resolve it before the popup takes focus.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from pr_tracker import __version__, config, paths, refresh
from pr_tracker.ledger import build_forest, connect, default_branches, humanize_age
from pr_tracker.ledger import flush as flush_events

# fzf 0.59 added --no-input, show-input/hide-input and FZF_INPUT_STATE.
FZF_MINIMUM = (0, 59)

# Catppuccin Latte and Macchiato, chosen once at launch. The toolbar
# deliberately draws only from the neutral ramp -- base, surface, overlay,
# text -- so every scrap of color left in the view belongs to PR status and
# nothing competes with it.
LATTE = {
    "base": (239, 241, 245),  # base
    "surface": (204, 208, 218),  # surface0
    "surface1": (188, 192, 204),  # surface1
    "surface2": (172, 176, 190),  # surface2
    "overlay0": (156, 160, 176),  # overlay0
    "overlay1": (140, 143, 161),  # overlay1
    "subtext": (108, 111, 133),  # subtext0
    "text": (76, 79, 105),  # text
    "green": (64, 160, 43),
    "red": (210, 15, 57),
    "yellow": (223, 142, 29),
    "peach": (254, 100, 11),
    "mauve": (136, 57, 239),
    "blue": (30, 102, 245),
}
MACCHIATO = {
    "base": (36, 39, 58),
    "surface": (54, 58, 79),
    "surface1": (73, 77, 100),
    "surface2": (91, 96, 120),
    "overlay0": (110, 115, 141),
    "overlay1": (128, 135, 162),
    "subtext": (165, 173, 203),
    "text": (202, 211, 245),
    "green": (166, 218, 149),
    "red": (237, 135, 150),
    "yellow": (238, 212, 159),
    "peach": (245, 169, 127),
    "mauve": (198, 160, 246),
    "blue": (138, 173, 244),
}

# Nerd Font glyphs, written as escapes rather than literal characters: the
# Private Use Area survives neither every editor nor every pipe this file has
# passed through, and a silently emptied string renders as nothing at all.
# Codepoints verified against FiraCode Nerd Font Mono's cmap.
ICO_OPEN = "\uf407"  # nf-oct-git_pull_request
ICO_DRAFT = "\uf4dd"  # nf-oct-git_pull_request_draft
ICO_MERGED = "\uf419"  # nf-oct-git_merge
ICO_CLOSED = "\uf4dc"  # nf-oct-git_pull_request_closed

# The rollup is a verdict on the whole PR, so it gets its own boxed family --
# reusing the per-check tick made a green PR read "check check 10".
ICO_ROLLUP_PASS = "\uf14a"  # nf-fa-square_check
ICO_ROLLUP_FAIL = "\uf2d3"  # nf-fa-window_close (nf-fa-times_rectangle)
ICO_ROLLUP_WAIT = "\uf0c8"  # nf-fa-square (solid, in yellow: no verdict yet)

ICO_PASS = "\uf00c"  # nf-fa-check
ICO_FAIL = "\uf00d"  # nf-fa-xmark
ICO_WAIT = "\uf252"  # nf-fa-hourglass_half
ICO_SKIP = "\uf05e"  # nf-fa-ban
ICO_ORPHAN = "\uf127"  # nf-fa-unlink

ICO_APPROVED = "\uf4a4"  # nf-oct-check_circle_fill
ICO_CHANGES = "\uf06a"  # nf-fa-exclamation_circle
ICO_REVIEW_WANTED = "\uf06e"  # nf-fa-eye
ICO_MERGEABLE = "\uf419"  # nf-oct-git_merge
ICO_CONFLICT = "\uf421"  # nf-oct-alert
ICO_UNKNOWN = "\uf059"  # nf-fa-question_circle

ICO_BELL = "\U000f009a"  # nf-md-bell       (every event delivered)
ICO_BELL_RING = "\U000f009e"  # nf-md-bell_ring  (something still queued)

SPINE = "\u2502"  # box drawings light vertical
SPINE_END = "\u2514"  # box drawings light up and right

ICO_STACK = "\uf51e"  # nf-oct-stack
ICO_OPEN_GROUP = "\uf47c"  # nf-oct-chevron_down
ICO_SHUT_GROUP = "\uf460"  # nf-oct-chevron_right

ICO_SESSION = "\uf415"  # nf-oct-person
ICO_EVERYONE = "\uf484"  # nf-oct-globe
ICO_ALL = "\uf451"  # nf-oct-list_unordered

PILL_L = "\ue0b6"  # nf-ple-left_half_circle_thick
PILL_R = "\ue0b4"  # nf-ple-right_half_circle_thick
PILL_SEP = "\ue0b0"  # nf-pl-left_hard_divider
GROUP_SEP = "\ue621"  # nf-indent-line
EMPTY = "\u00b7"  # a placeholder that holds a column without drawing an icon

RESET = "\x1b[0m"

SCOPES = ("open", "attention", "terminal", "all")
SCOPE_LABEL = {
    "open": "open",
    "attention": "attention",
    "terminal": "merged",
    "all": "all",
}
SCOPE_ICON = {
    "open": ICO_OPEN,
    "attention": ICO_CONFLICT,
    "terminal": ICO_MERGED,
    "all": ICO_ALL,
}

# The preview's share of the window. One constant, used both to place the
# preview window and to budget how wide a row may be.
PREVIEW_RATIO = 0.40

# Columns fzf keeps for itself out of the list side: the pointer, the
# multi-select marker, and the preview's left border. Measured against fzf
# 0.74 at four terminal widths rather than guessed -- over-estimating wastes
# screen, under-estimating feeds the status gutter to the truncator.
LIST_CHROME = 3
# Keys that are only meaningful while the list has focus. They become ordinary
# characters the moment the query input takes over, and come back on esc.
LIST_KEYS = ("j", "k", "s", "a", "y", "q", "R", "/", "space")


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(text):
    return len(ANSI_RE.sub("", text))


def list_width(state):
    """How many columns a row may occupy.

    FZF_COLUMNS is exported to bound commands but is 0 until fzf has laid
    itself out, so the launch-time terminal width is stashed in the state file
    as the fallback. Subtract the preview's share plus the pointer, gutter and
    preview border.
    """
    cols = 0
    try:
        cols = int(os.environ.get("FZF_COLUMNS") or 0)
    except ValueError:
        cols = 0
    cols = cols or state.get("cols") or shutil.get_terminal_size((120, 40)).columns
    # fzf hands the preview int(cols * ratio) and keeps the remainder.
    return max(cols - int(cols * PREVIEW_RATIO) - LIST_CHROME, 40)


def sgr(rgb, bold=False, dim=False):
    attrs = []
    if bold:
        attrs.append("1")
    if dim:
        attrs.append("2")
    attrs.append(f"38;2;{rgb[0]};{rgb[1]};{rgb[2]}")
    return f"\x1b[{';'.join(attrs)}m"


def bg(rgb):
    return f"\x1b[48;2;{rgb[0]};{rgb[1]};{rgb[2]}m"


def _where():
    return paths.resolve(os.environ)


def system_appearance(env=os.environ):
    """'light' or 'dark', from theme-monitor's file if it is running, else macOS itself."""
    try:
        value = paths.theme_file(env).read_text().strip()
        if value in ("light", "dark"):
            return value
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            return "dark" if result.stdout.strip() == "Dark" else "light"
        except (OSError, subprocess.SubprocessError):
            pass
    return "light"


def resolve_theme(cfg, env=os.environ):
    """PR_TRACKER_THEME, then `picker.theme`, then the system appearance."""
    choice = (env.get("PR_TRACKER_THEME") or cfg.get("theme") or "auto").lower()
    if choice in ("light", "dark"):
        return choice
    return system_appearance(env)


def palette(state=None):
    """The palette chosen at launch. Bound commands read it back from the state file."""
    theme = (state or {}).get("theme") or resolve_theme(config.load(_where().config))
    return MACCHIATO if theme == "dark" else LATTE


# ── filter state ────────────────────────────────────────────────────────────


DEFAULT_STATE = {
    "session_only": True,
    "scope": "open",
    "cols": 0,
    "collapsed": [],
    "theme": "",
}


def read_state(path):
    try:
        state = dict(DEFAULT_STATE)
        state.update(json.loads(Path(path).read_text()))
        return state
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)


def write_state(path, state):
    Path(path).write_text(json.dumps(state))


# ── queries ─────────────────────────────────────────────────────────────────

SCOPE_SQL = {
    "open": "p.state = 'open'",
    "terminal": "p.state != 'open'",
    "all": "1",
    # Wants something from you: red checks, a reviewer asking for changes, or
    # an event the owning session has not been told about yet.
    "attention": (
        "p.state = 'open' AND ("
        "  p.checks_rollup = 'FAILURE'"
        "  OR p.review_state = 'changes_requested'"
        "  OR EXISTS (SELECT 1 FROM events e"
        "             WHERE e.pr_id = p.id AND e.consumed_at IS NULL)"
        ")"
    ),
}


def load(state, session_id):
    """Rows for the current filters, plus the event tallies behind the badge."""
    where = _where()
    cfg = config.load(where.config)
    if not where.db.exists():
        return [], cfg
    session_only = state["session_only"] and bool(session_id)
    conn = connect(where.db, readonly=True)
    try:
        where = SCOPE_SQL[state["scope"]]
        if session_only:
            sql = (
                f"SELECT p.* FROM prs p JOIN session_prs sp ON sp.pr_id = p.id"
                f" WHERE sp.session_id = ? AND ({where})"
            )
            rows = conn.execute(sql, (session_id,)).fetchall()
        else:
            rows = conn.execute(f"SELECT p.* FROM prs p WHERE {where}").fetchall()

        # The badge counts the events that were meant for *you* when the list is
        # yours, and everything the PR ever raised when it is not.
        if session_only:
            tally = conn.execute(
                "SELECT pr_id, COUNT(*) total, COUNT(consumed_at) done FROM events"
                " WHERE session_id = ? GROUP BY pr_id",
                (session_id,),
            ).fetchall()
        else:
            tally = conn.execute(
                "SELECT pr_id, COUNT(*) total, COUNT(consumed_at) done FROM events GROUP BY pr_id"
            ).fetchall()
        events = {r["pr_id"]: (r["done"], r["total"]) for r in tally}
        defaults = default_branches(conn)
    finally:
        conn.close()

    prs = [dict(r) for r in rows]
    for pr in prs:
        pr["events_done"], pr["events_total"] = events.get(pr["id"], (0, 0))
    return build_forest(prs, defaults), cfg


# ── row rendering ───────────────────────────────────────────────────────────


def state_icon(pr, pal):
    if pr["state"] == "merged":
        return sgr(pal["mauve"], bold=True) + ICO_MERGED + RESET
    if pr["state"] == "closed":
        return sgr(pal["red"]) + ICO_CLOSED + RESET
    if pr["is_draft"]:
        return sgr(pal["overlay0"]) + ICO_DRAFT + RESET
    return sgr(pal["green"], bold=True) + ICO_OPEN + RESET


def review_glyph(review_state, pal):
    return {
        "approved": (ICO_APPROVED, pal["green"]),
        "changes_requested": (ICO_CHANGES, pal["peach"]),
        "review_required": (ICO_REVIEW_WANTED, pal["overlay0"]),
    }.get(review_state or "", (None, pal["overlay0"]))


def merge_glyph(pr, pal):
    """GitHub reports mergeable as UNKNOWN once a PR is no longer open, so a
    merged PR used to render a permanent question mark. Terminal PRs have no
    merge state worth showing."""
    if pr["state"] != "open":
        return None, pal["overlay0"]
    return {
        "MERGEABLE": (ICO_MERGEABLE, pal["green"]),
        "CONFLICTING": (ICO_CONFLICT, pal["red"]),
    }.get(pr["mergeable"] or "", (ICO_UNKNOWN, pal["overlay0"]))


def stale_span(pr, cfg, pal):
    # Terminal PRs are deliberately not refreshed any more, so "stale" would
    # just be an ever-growing clock on a PR that is never going to change.
    if pr["state"] != "open":
        return ""
    refreshed = pr["last_refreshed_at"]
    if not refreshed:
        return ""
    age = time.time() - refreshed
    if age <= cfg["interval_seconds"] * cfg["stale_after_multiple"]:
        return ""
    return sgr(pal["overlay0"], dim=True) + f"stale {humanize_age(age)}" + RESET


def group_plan(prs):
    """Segment the forest-ordered rows into stacks and standalone PRs.

    build_forest already emits each tree's root followed by its descendants,
    so a run is a root plus everything below it until the next depth-0 row.
    A run of one is not a stack -- it is just a PR.
    """
    plan, run = [], []

    def flush():
        if not run:
            return
        plan.append(("stack", list(run)) if len(run) > 1 else ("pr", run[0]))
        run.clear()

    for pr in prs:
        if pr.get("depth", 0) == 0:
            flush()
        run.append(pr)
    flush()
    return plan


def stack_title(members):
    """What to call a stack.

    The root's own branch: it is the base the rest of the stack is built on,
    it survives PRs being added at the top, and it needs no house convention
    to parse. `gh stack`'s own name wins when the repo has one.
    """
    root = members[0]
    return root.get("stack_id") or root.get("head_ref") or f"#{root['number']}"


def ready_count(members):
    """Members whose CI says they could go: open, not draft, checks green."""
    return sum(
        1
        for pr in members
        if pr["state"] == "open" and not pr["is_draft"] and pr["checks_rollup"] == "SUCCESS"
    )


def group_row(members, collapsed, pal, width):
    """A stack's header line. No status gutter -- a count of how many PRs it
    holds and how many are ready says more here than eight icons would."""
    chevron = ICO_SHUT_GROUP if collapsed else ICO_OPEN_GROUP
    head = f"{sgr(pal['overlay1'])}{chevron}{RESET} {sgr(pal['mauve'])}{ICO_STACK}{RESET} "
    title = stack_title(members)
    ready = ready_count(members)
    tail = (
        f"{sgr(pal['subtext'])}{len(members)} PRs{RESET}"
        f"{sgr(pal['overlay0'], dim=True)} · {RESET}"
        f"{sgr(pal['green'] if ready == len(members) else pal['overlay1'])}"
        f"{ready} ready{RESET}"
    )
    budget = max(width - visible_len(head) - visible_len(tail) - 2, 8)
    if len(title) > budget:
        title = "…" + title[-(budget - 1) :]
    pad = max(width - visible_len(head) - len(title) - visible_len(tail), 1)
    body = f"{sgr(pal['text'], bold=True)}{title}{RESET}"
    return f"{head}{body}{' ' * pad}{tail}\tg:{members[0]['id']}"


def count_cells(pr, pal, digits):
    """The four per-state counts, each in a column of its own.

    Every cell keeps its width whether or not it has a number in it, so a
    skipped-check count never shifts the column a failure count lives in.
    """
    cells = []
    for count, icon, color in (
        (pr["checks_pass"], ICO_PASS, pal["green"]),
        (pr["checks_fail"], ICO_FAIL, pal["red"]),
        (pr["checks_pending"], ICO_WAIT, pal["yellow"]),
        (pr["checks_skip"], ICO_SKIP, pal["overlay0"]),
    ):
        if count:
            cells.append(f"{sgr(color)}{icon} {str(count).rjust(digits)}{RESET}")
        else:
            cells.append(" " * (2 + digits))
    return " ".join(cells)


def rollup_cell(pr, pal):
    if pr["needs_hydrate"] or (pr["checks_rollup"] or "NONE") == "NONE":
        return f"{sgr(pal['overlay0'], dim=True)}{EMPTY}{RESET}"
    return {
        "SUCCESS": sgr(pal["green"], bold=True) + ICO_ROLLUP_PASS,
        "FAILURE": sgr(pal["red"], bold=True) + ICO_ROLLUP_FAIL,
        "PENDING": sgr(pal["yellow"], bold=True) + ICO_ROLLUP_WAIT,
    }[pr["checks_rollup"]] + RESET


def review_cell(pr, pal):
    icon, color = review_glyph(pr["review_state"], pal)
    return f"{sgr(color)}{icon}{RESET}" if icon else " "


def notify_cell(pr, pal, span):
    done, total = pr.get("events_done", 0), pr.get("events_total", 0)
    if not total:
        return " " * (2 + span)
    text = f"{done}/{total}".rjust(span)
    if done < total:
        return f"{sgr(pal['yellow'], bold=True)}{ICO_BELL_RING} {text}{RESET}"
    return f"{sgr(pal['overlay0'], dim=True)}{ICO_BELL} {text}{RESET}"


def flag_cells(pr, cfg, pal):
    flags = []
    if pr.get("orphan"):
        flags.append(sgr(pal["peach"], dim=True) + ICO_ORPHAN + RESET)
    stale = stale_span(pr, cfg, pal)
    if stale:
        flags.append(stale)
    return "  ".join(flags)


def status_block(pr, cfg, pal, digits, span):
    """One row's status, as three groups in fixed columns.

    Grouped by what the icons answer: what CI thinks, what people think, and
    what is wrong with the PR itself.
    """
    sep = f" {sgr(pal['surface2'])}{GROUP_SEP}{RESET} "
    checks = f"{rollup_cell(pr, pal)}  {count_cells(pr, pal, digits)}"
    people = f"{review_cell(pr, pal)}  {notify_cell(pr, pal, span)}"
    block = f"{checks}{sep}{people}"
    flags = flag_cells(pr, cfg, pal)
    return f"{block}{sep}{flags}" if flags else block


def render(prs, cfg, pal, width, collapsed=()):
    """One line per PR, plus a header line per stack.

    Stack members render flush left exactly like a standalone PR -- the group
    costs them no columns. The header row above is what holds them together,
    and collapsing it hides the members rather than indenting them away.

    Every status column is sized once for the whole screen, so each kind of
    icon lands in the same column on every row. Titles are cropped to clear
    the gutter: the title is the part a reader can reconstruct, and fzf would
    otherwise truncate from the end and eat the status instead.
    """
    plan = group_plan(prs)
    collapsed = {int(c) for c in collapsed}

    visible = []
    for kind, item in plan:
        if kind == "pr":
            visible.append(item)
        elif item[0]["id"] not in collapsed:
            visible.extend(item)

    counts = [
        c
        for pr in visible
        for c in (pr["checks_pass"], pr["checks_fail"], pr["checks_pending"], pr["checks_skip"])
    ]
    digits = max((len(str(c)) for c in counts if c), default=1)
    span = max(
        (
            len(f"{pr.get('events_done', 0)}/{pr.get('events_total', 0)}")
            for pr in visible
            if pr.get("events_total")
        ),
        default=1,
    )
    blocks = {id(pr): status_block(pr, cfg, pal, digits, span) for pr in visible}
    gutter = max((visible_len(b) for b in blocks.values()), default=0)
    status_col = max(width - gutter, 20)

    def pr_line(pr, lead=""):
        head = f"{lead}{state_icon(pr, pal)} {sgr(pal['subtext'])}#{pr['number']}{RESET} "
        head_len = visible_len(head)
        block = blocks[id(pr)]
        title = pr["title"] or f"{pr['repo']} (not yet refreshed)"
        budget = max(status_col - head_len - 2, 8)
        if len(title) > budget:
            title = title[: budget - 1].rstrip() + "\u2026"
        pad = max(status_col - head_len - len(title), 1)
        body = f"{sgr(pal['text'], bold=True)}{title}{RESET}"
        return f"{head}{body}{' ' * pad}{block}\t{pr['id']}"

    lines = []
    for kind, item in plan:
        if kind == "pr":
            lines.append(pr_line(item))
            continue
        shut = item[0]["id"] in collapsed
        lines.append(group_row(item, shut, pal, width))
        if shut:
            continue
        # One column of spine, and one space: enough to read as belonging to
        # the header above without the staircase this replaced.
        for i, pr in enumerate(item):
            glyph = SPINE_END if i == len(item) - 1 else SPINE
            lines.append(pr_line(pr, sgr(pal["overlay0"], dim=True) + glyph + RESET + " "))
    return lines


# ── header ──────────────────────────────────────────────────────────────────


def pill(key, icon, label, active, pal):
    """A rounded, two-tone chip: the key rides its own darker segment, split
    from the icon and label by a powerline divider, so the thing you press is
    read as a key rather than as the first letter of the label.

    Active chips take accent grounds, inactive ones the neutral surface, so
    the toolbar keeps its shape and width either way.
    """
    # Grounds stay in the two quietest steps of the ramp -- barely off the
    # background in either theme -- and the *ink* carries active vs inactive.
    # The toolbar is not where attention should land.
    key_bg = pal["surface2"] if active else pal["surface1"]
    body_bg = pal["surface1"] if active else pal["surface"]
    key_fg = pal["text"] if active else pal["overlay1"]
    body_fg = pal["text"] if active else pal["overlay1"]
    return (
        f"{sgr(key_bg)}{PILL_L}{RESET}"
        f"{bg(key_bg)}{sgr(key_fg, bold=True)} {key} {RESET}"
        f"{bg(body_bg)}{sgr(key_bg)}{PILL_SEP}{RESET}"
        f"{bg(body_bg)}{sgr(body_fg, bold=active)} {icon} {label} {RESET}"
        f"{sgr(body_bg)}{PILL_R}{RESET}"
    )


def header(state, session_id, pal):
    session_on = state["session_only"] and bool(session_id)
    pills = [
        pill(
            "s",
            ICO_SESSION if session_on else ICO_EVERYONE,
            "session" if session_on else "all",
            session_on,
            pal,
        ),
        pill(
            "a",
            SCOPE_ICON[state["scope"]],
            SCOPE_LABEL[state["scope"]],
            state["scope"] != "open",
            pal,
        ),
    ]
    marked = marked_count()
    if input_shown():
        hints = f"{sgr(pal['yellow'])}search{RESET}  esc back to list"
    elif marked:
        hints = (
            f"{sgr(pal['text'], bold=True)}{marked} marked{RESET}"
            f"{sgr(pal['overlay0'], dim=True)} · ↵ open all · y copy all · esc clear{RESET}"
        )
    else:
        hints = (
            f"{sgr(pal['overlay0'], dim=True)}"
            "␣ mark · ↵ open/fold · y copy · R refresh · / search · q quit"
            f"{RESET}"
        )
    # A blank row, the pills, the hints, another blank row: the toolbar sits
    # off both the pane's top edge and the list. Hints get their own line
    # because the widest pill combination plus a full hint line does not fit a
    # narrow pane, and a truncated hint is worse than a taller toolbar.
    return " \n" + "  ".join(pills) + "\n" + hints + "\n "


# ── preview ─────────────────────────────────────────────────────────────────


def preview_width(default=60):
    try:
        return max(int(os.environ.get("FZF_PREVIEW_COLUMNS", default)), 20)
    except ValueError:
        return default


def preview(pr_id, state=None, out=None):
    out = out or sys.stdout
    pal = palette(state)
    width = preview_width()
    db = _where().db
    if not db.exists():
        return 0
    conn = connect(db, readonly=True)
    try:
        pr = conn.execute("SELECT * FROM prs WHERE id = ?", (pr_id,)).fetchone()
        if not pr:
            return 0
        checks = conn.execute(
            "SELECT name, state FROM pr_checks WHERE pr_id = ? ORDER BY"
            " CASE state WHEN 'FAILURE' THEN 0 WHEN 'PENDING' THEN 1"
            " WHEN 'SUCCESS' THEN 2 ELSE 3 END, name",
            (pr_id,),
        ).fetchall()
    finally:
        conn.close()

    def emit(line=""):
        print(line, file=out)

    # fzf paints its scroll position over the preview's first line, so start
    # one line down and wrap rather than letting the title run under it.
    emit()
    title = pr["title"] or "(not yet refreshed)"
    for line in textwrap.wrap(title, width=width) or [title]:
        emit(f"{sgr(pal['text'], bold=True)}{line}{RESET}")
    emit(f"{sgr(pal['subtext'])}{pr['repo']}#{pr['number']}{RESET}")
    emit(f"{sgr(pal['overlay0'], dim=True)}{pr['url']}{RESET}")
    emit()
    if pr["base_ref"]:
        arrow = f"{sgr(pal['overlay0'], dim=True)}→{RESET}"
        emit(
            f"{sgr(pal['blue'])}{pr['head_ref']}{RESET} {arrow} "
            f"{sgr(pal['blue'])}{pr['base_ref']}{RESET}"
        )

    icon, color = review_glyph(pr["review_state"], pal)
    if icon:
        label = (pr["review_state"] or "").replace("_", " ")
        emit(f"{sgr(color)}{icon}{RESET} {label}")
    icon, color = merge_glyph(pr, pal)
    if icon:
        label = (pr["mergeable"] or "unknown").lower()
        emit(f"{sgr(color)}{icon}{RESET} {label}")

    emit()
    if not checks:
        emit(f"{sgr(pal['overlay0'], dim=True)}no checks reported{RESET}")
    for check in checks:
        color = {
            "FAILURE": pal["red"],
            "PENDING": pal["yellow"],
            "SUCCESS": pal["green"],
        }.get(check["state"], pal["overlay0"])
        icon = {
            "FAILURE": ICO_FAIL,
            "PENDING": ICO_WAIT,
            "SUCCESS": ICO_PASS,
        }.get(check["state"], ICO_SKIP)
        emit(f"{sgr(color)}{icon}{RESET} {check['name']}")

    body = (pr["body"] or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    emit()
    emit(f"{sgr(pal['overlay0'], dim=True)}{'─' * width}{RESET}")
    emit()
    if not body:
        emit(f"{sgr(pal['overlay0'], dim=True)}no description{RESET}")
        return 0
    for para in body.split("\n"):
        if not para.strip():
            emit()
            continue
        for line in textwrap.wrap(para, width=width) or [""]:
            emit(f"{sgr(pal['subtext'])}{line}{RESET}")
    return 0


# ── actions bound inside fzf ────────────────────────────────────────────────


def self_cmd(*args):
    """A shell command that runs this module again, with the same interpreter.

    `-P` keeps the working directory off sys.path, so a directory that happens
    to contain a `pr_tracker` folder cannot shadow the installed package.
    """
    parts = [sys.executable, "-P", "-m", "pr_tracker.picker", *args]
    return " ".join(shlex.quote(str(p)) for p in parts)


def input_shown():
    """Whether fzf's query field is currently accepting input.

    fzf reports this itself, so there is no local flag to drift out of sync
    with what the user is actually looking at.
    """
    return (os.environ.get("FZF_INPUT_STATE") or "hidden") != "hidden"


def marked_count():
    try:
        return int(os.environ.get("FZF_SELECT_COUNT") or 0)
    except ValueError:
        return 0


def open_urls(urls):
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    for url in urls:
        with contextlib.suppress(OSError):
            subprocess.run(
                [opener, url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )


def copy_text(text):
    """Put `text` on the clipboard with whichever tool this system has."""
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    else:
        candidates = [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]]
    for command in candidates:
        if shutil.which(command[0]):
            with contextlib.suppress(OSError):
                subprocess.run(command, input=text, text=True, check=False)
                return True
    return False


def reload_actions(state_path):
    return (
        f"reload({self_cmd('--rows', state_path)})"
        f"+transform-header({self_cmd('--header', state_path)})"
    )


def act_enter(state_path, session_id, current, marked):
    """enter is contextual: a header folds, a PR opens."""
    if str(current).startswith("g:"):
        state = read_state(state_path)
        root = int(str(current)[2:])
        shut = {int(c) for c in state.get("collapsed", [])}
        shut.symmetric_difference_update({root})
        state["collapsed"] = sorted(shut)
        write_state(state_path, state)
        print(reload_actions(state_path))
        return 0
    open_urls(pr_urls(resolve_refs(marked, state_path, session_id)))
    print("abort")
    return 0


def act_space(state_path, session_id, current, marked):
    """space on a header marks the whole stack; on a PR it marks the one row.

    fzf has no select-by-predicate, but a stack's members are always the rows
    directly beneath its header -- so walking down and selecting each one is
    exactly equivalent, and the cursor is put back where it started.
    """
    if not str(current).startswith("g:"):
        print(f"toggle+down+transform-header({self_cmd('--header', state_path)})")
        return 0

    state = read_state(state_path)
    root = int(str(current)[2:])
    if root in {int(c) for c in state.get("collapsed", [])}:
        # Nothing to walk over while it is folded; open it and let the next
        # press do the marking.
        state["collapsed"] = [c for c in state["collapsed"] if int(c) != root]
        write_state(state_path, state)
        print(reload_actions(state_path))
        return 0

    members = members_of(state_path, session_id).get(root, [])
    if not members:
        print("ignore")
        return 0
    # With nothing marked fzf passes the CURRENT row as {+2} -- here the
    # header itself, which expands to the whole stack and would read as
    # "already selected". Its own count is the honest signal.
    already = set(resolve_refs(marked, state_path, session_id)) if marked_count() else set()
    verb = "deselect" if already.issuperset(members) else "select"
    steps = [f"down+{verb}"] * len(members) + ["up"] * len(members)
    steps.append(f"transform-header({self_cmd('--header', state_path)})")
    print("+".join(steps))
    return 0


def act(key, state_path, session_id):
    """Mutate the filter state, then print the fzf actions that follow from it.

    Bound through `transform`, so the list and the header are both rebuilt
    from state that has already changed -- no ordering race between a reload
    and a header refresh.
    """
    state = read_state(state_path)

    if key == "s":
        state["session_only"] = not state["session_only"]
    elif key == "a":
        state["scope"] = SCOPES[(SCOPES.index(state["scope"]) + 1) % len(SCOPES)]
    elif key == "search":
        keys = ",".join(LIST_KEYS)
        print(f"show-input+unbind({keys})+transform-header({self_cmd('--header', state_path)})")
        return 0
    elif key == "escape":
        if not input_shown():
            # Unwind one layer at a time: marks first, the picker last. fzf
            # exports the live count, so this needs no bookkeeping of its own.
            if marked_count():
                print(f"deselect-all+transform-header({self_cmd('--header', state_path)})")
            else:
                print("abort")
            return 0
        print(
            f"hide-input+clear-query+rebind({','.join(LIST_KEYS)})"
            f"+transform-header({self_cmd('--header', state_path)})"
        )
        return 0
    else:
        print("ignore")
        return 0

    write_state(state_path, state)
    print(reload_actions(state_path))
    return 0


def list_binds(state_path):
    return [
        "j:down",
        "k:up",
        f"space:transform({self_cmd('--act-space', state_path)} {{2}} -- {{+2}})",
        # reload doubles as the progress indicator: fzf spins while it runs
        f"R:reload({self_cmd('--refresh-rows', state_path)})"
        f"+transform-header({self_cmd('--header', state_path)})",
        f"s:transform({self_cmd('--act', 's', state_path)})",
        f"a:transform({self_cmd('--act', 'a', state_path)})",
        f"/:transform({self_cmd('--act', 'search', state_path)})",
        f"esc:transform({self_cmd('--act', 'escape', state_path)})",
        "q:abort",
        f"y:execute-silent({self_cmd('--copy', '--state', state_path)} {{+2}})+abort",
        f"enter:transform({self_cmd('--act-enter', state_path)} {{2}} -- {{+2}})",
    ]


def members_of(state_path, session_id):
    """root pr id -> the stack's member ids, in stack order."""
    state = read_state(state_path)
    prs, _ = load(state, session_id)
    return {
        item[0]["id"]: [pr["id"] for pr in item]
        for kind, item in group_plan(prs)
        if kind == "stack"
    }


def resolve_refs(refs, state_path=None, session_id=""):
    """Expand fzf's field-2 values into PR ids.

    A group reference stands for every PR in its stack, so copying or opening
    with the cursor on a header does the obvious thing.
    """
    groups = None
    out = []
    for ref in refs:
        ref = str(ref)
        if ref.startswith("g:"):
            if groups is None:
                groups = members_of(state_path, session_id) if state_path else {}
            root = int(ref[2:]) if ref[2:].isdigit() else None
            out.extend(groups.get(root, []))
        elif ref.isdigit():
            out.append(int(ref))
    seen, uniq = set(), []
    for i in out:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    return uniq


def pr_urls(pr_ids):
    """URLs for the marked rows, in the order fzf handed them over."""
    ids = [int(i) for i in pr_ids if str(i).isdigit()]
    db = _where().db
    if not ids or not db.exists():
        return []
    conn = connect(db, readonly=True)
    try:
        placeholders = ",".join("?" * len(ids))
        found = {
            r["id"]: r["url"]
            for r in conn.execute(f"SELECT id, url FROM prs WHERE id IN ({placeholders})", ids)
        }
    finally:
        conn.close()
    return [found[i] for i in ids if i in found]


def refresh_now():
    """`R`: poll GitHub now, then mark every pending event delivered.

    Runs in process with its log swallowed: anything written to the terminal
    here would land on top of fzf's screen.
    """
    where = _where()
    with contextlib.redirect_stderr(io.StringIO()), contextlib.suppress(Exception):
        refresh.tick(where, config.load(where.config), force=True)
    flush_events(where.db)


# ── entrypoint ──────────────────────────────────────────────────────────────


def rows_for(state_path, session_id):
    state = read_state(state_path)
    prs, cfg = load(state, session_id)
    lines = render(prs, cfg, palette(state), list_width(state), state.get("collapsed", []))
    return lines, state


def fzf_version(text):
    match = re.match(r"(\d+)\.(\d+)", text.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def check_fzf():
    """None when a usable fzf is on PATH, else why not."""
    if not shutil.which("fzf"):
        return "fzf not found on PATH"
    try:
        result = subprocess.run(["fzf", "--version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"could not run fzf: {exc}"
    version = fzf_version(result.stdout)
    if version and version < FZF_MINIMUM:
        need = ".".join(map(str, FZF_MINIMUM))
        return f"fzf {result.stdout.split()[0]} is too old; the picker needs {need} or later"
    return None


INTERNAL = {
    "--preview",
    "--rows",
    "--refresh-rows",
    "--header",
    "--act",
    "--act-enter",
    "--act-space",
    "--copy",
}


def internal(argv, session_id):
    """The commands fzf's bindings call back into."""
    command = argv[0]
    if command == "--preview":
        # With an empty list fzf still runs this, with an empty {2}.
        state = read_state(argv[1]) if len(argv) > 2 else None
        arg = argv[-1] if len(argv) > 1 else ""
        return preview(int(arg), state) if arg.isdigit() else 0
    if command == "--refresh-rows":
        refresh_now()
        lines, _ = rows_for(argv[1], session_id)
        print("\n".join(lines))
        return 0
    if command == "--rows":
        lines, _ = rows_for(argv[1], session_id)
        print("\n".join(lines))
        return 0
    if command == "--header":
        state = read_state(argv[1])
        print(header(state, session_id, palette(state)))
        return 0
    if command == "--act":
        return act(argv[1], argv[2], session_id)
    if command in ("--act-enter", "--act-space"):
        state_path, rest = argv[1], argv[2:]
        current = rest[0] if rest else ""
        marked = rest[rest.index("--") + 1 :] if "--" in rest else []
        fn = act_enter if command == "--act-enter" else act_space
        return fn(state_path, session_id, current, marked)
    if command == "--copy":
        state_path = argv[2] if argv[1:2] == ["--state"] else None
        refs = argv[3:] if state_path else argv[1:]
        urls = pr_urls(resolve_refs(refs, state_path, session_id))
        if urls:
            copy_text("\n".join(urls))
        return 0
    return 2


def build_parser(prog="prs"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Browse tracked pull requests in fzf.",
        epilog="Keys: j/k move, space mark, enter open or fold, y copy, s session/all, "
        "a cycle open/attention/merged/all, R refresh, / search, esc back, q quit.",
    )
    parser.add_argument(
        "--session",
        help="start scoped to this agent session (default: $PR_TRACKER_SESSION_ID)",
    )
    parser.add_argument(
        "--print",
        dest="print_rows",
        action="store_true",
        help="print the list once instead of opening fzf",
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] and argv[0] in INTERNAL:
        return internal(argv, os.environ.get("PR_TRACKER_SESSION_ID") or "")
    args = build_parser().parse_args(argv)
    return run(args.session, args.print_rows)


def run(session=None, print_rows=False):
    session_id = session or os.environ.get("PR_TRACKER_SESSION_ID") or ""
    problem = None if print_rows else check_fzf()
    if problem:
        print(f"prs: {problem}; printing the list instead", file=sys.stderr)
        print_rows = True

    scratch = tempfile.mkdtemp(prefix="pr-tracker-")
    state_path = os.path.join(scratch, "state.json")
    state = dict(DEFAULT_STATE)
    state["session_only"] = bool(session_id)
    state["cols"] = shutil.get_terminal_size((120, 40)).columns
    state["theme"] = resolve_theme(config.load(_where().config))
    write_state(state_path, state)

    try:
        lines, state = rows_for(state_path, session_id)
        if not lines:
            # Widen rather than presenting an empty list, and if there is
            # genuinely nothing tracked anywhere, say so instead of handing
            # fzf a blank line to sit on.
            if not widen(state_path, session_id, state):
                print("no tracked pull requests", file=sys.stderr)
                return 0
            lines, state = rows_for(state_path, session_id)

        if print_rows:
            if not sys.stdout.isatty():
                lines = [ANSI_RE.sub("", line) for line in lines]
            for line in lines:
                print(line.split("\t", 1)[0])
            return 0 if not problem else 1

        binds = list_binds(state_path)
        result = subprocess.run(
            [
                "fzf",
                "--ansi",
                "--delimiter",
                "\t",
                "--with-nth",
                "1",
                "--layout",
                "reverse",
                "--border=none",
                "--height",
                "100%",
                "--no-info",
                "--multi",  # space marks rows; enter acts on all of them
                # The list owns the keyboard: with no input field at all, a
                # stray letter cannot land in a query that is not filtering.
                # `/` summons the field, esc dismisses it.
                "--no-input",
                "--prompt",
                "pr> ",
                "--header",
                header(state, session_id, palette(state)),
                "--preview",
                f"{self_cmd('--preview', state_path)} {{2}}",
                "--preview-window",
                f"right,{int(PREVIEW_RATIO * 100)}%,border-left",
                *[arg for bind in binds for arg in ("--bind", bind)],
            ],
            # fzf runs --rows itself rather than reading a pipe: one code path
            # feeds the first paint and every later reload, nothing can break a
            # pipe by quitting before it is drained, and fzf never falls back
            # to walking the current directory for want of an input source.
            env={
                **os.environ,
                "FZF_DEFAULT_COMMAND": self_cmd("--rows", state_path),
                "PR_TRACKER_SESSION_ID": session_id,
            },
        )
        return 0 if result.returncode in (0, 1, 130) else result.returncode
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def widen(state_path, session_id, state):
    """Fall back through the filters until something is in view.

    Order matters. A session whose PRs have all merged should keep its own
    list and widen the state filter -- opening on an empty list is jarring,
    but silently swapping in another session's work is worse. So the scope
    goes first and the session filter is the last thing surrendered.

    The pills say which filters ended up selected, so there is nothing to
    annotate: returns True when something was found, False when nothing is
    tracked at all.
    """
    candidates = []
    if state["scope"] != "all":
        candidates.append({**state, "scope": "all"})
    if state["session_only"]:
        candidates.append({**state, "session_only": False})
        if state["scope"] != "all":
            candidates.append({**state, "session_only": False, "scope": "all"})
    for candidate in candidates:
        prs, _ = load(candidate, session_id)
        if prs:
            write_state(state_path, candidate)
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
