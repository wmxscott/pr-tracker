# pr-tracker design

This describes pr-tracker 1.0: why it exists, how the pieces fit, and the
trade-offs behind them. The [README](../README.md) covers installing and using
it; this is the reasoning underneath.

## 1. Why it exists

`gh pr list`, `gh dash` and per-branch PR indicators in a terminal or editor
answer "which PRs exist?". None of them answers the question pr-tracker is
for: **"what did my agents open, and where does each one stand?"**

A single agent session routinely opens several PRs, sometimes across
worktrees, and PRs outlive the session that created them. Meanwhile CI goes
red or a reviewer asks for changes, and the agent that opened the PR is never
told. So pr-tracker keeps two things nothing else keeps:

- the link between an agent session and the PRs it opened, and
- a queue of changes to those PRs, delivered back to the owning session.

The store is a cross-session ledger, and the session is one filter over it:
the default one when the picker is opened from inside a session.

## 2. Decisions

| Question | Decision |
|---|---|
| Store scope | One ledger across sessions; the session is a filter, not the schema's root |
| Agents | Built and tested against Claude Code; the hook reads generic hook JSON, and `record` / `drain` / `adopt` are plain CLI commands other integrations can call |
| Session binding | The agent's session id; a nullable parent column is reserved for forks (§3.2) |
| Capture | `gh pr create`, `gh stack submit` / `push`, GitHub MCP `create_pull_request`, and manual `adopt` |
| Stacks | Inferred from base/head branch chains; `gh stack view --json` ordering wins when present |
| Tracked fields | Title, body, branches, draft, mergeable, review decision, per-check states |
| Cadence | A scheduler ticks every 60 s; the tick refreshes only every 300 s ± 30 s |
| Retention | Merged or closed PRs are dropped 30 days after they close (configurable) |
| Human notification | Pull only, through the picker; `Stop` also shows undelivered events |
| Agent notification | Delivered on the session's next tool call, mid-turn |
| Failure handling | Skip silently, keep the last known state, mark it `stale` in the picker |
| Runtime | Python 3.11+, standard library only; every GitHub call goes through `gh` |
| Repo scope | Any repo an agent opens a PR in; no allowlist |

## 3. Architecture

Three entry points share one SQLite database and nothing else.

```
agent tool call ──► pr-tracker hook ──► record (offline, minimal row)
                                              │
                             ~/.local/state/pr-tracker/prs.db
                                              │
scheduler (60 s) ──► pr-tracker refresh ──────┤ hydrate · diff · queue events
                                              │
          next tool call ──► pr-tracker hook ◄┤ deliver events into the session's context
                                              │
                           prs (fzf picker) ◄─┘ read-only render
```

- **`pr-tracker hook`** runs after agent tool calls. It records PRs the call
  created and delivers queued events.
- **`pr-tracker refresh`** runs from a scheduler: the Homebrew service, a
  launchd agent, or cron. It asks GitHub about open PRs, diffs, and queues
  events.
- **`prs`** (also `pr-tracker pick`) renders the ledger in fzf.

The database uses WAL mode and a 3-second busy timeout, so all three can
overlap.

### 3.1 Why the hook does no network I/O

The hook runs after every shell command in every session. It scrapes the PR
URL out of the tool's output, writes a minimal row flagged `needs_hydrate`,
and returns. Title, checks, review state and branches are filled in by the
next refresh. This keeps the hook fast, safe offline, and independent of
`gh`'s auth state when the PR is created.

The hot path is trimmed further:

- `pr-tracker hook` skips argparse and imports only what it needs.
- With no ledger on disk and no PR being created, which is the common case
  for anyone who has never tracked anything, it stops after one file check.
- Delivery with an empty queue costs one indexed `SELECT` on a read-only
  connection, and marking events delivered skips the schema check that
  other writers run.
- It always exits 0 with nothing on stderr. A bad payload, a locked or
  broken ledger, or a full disk all end in silence. A PR tracker isn't worth
  breaking a tool call over.

### 3.2 Session identity

Claude Code keeps a session's id across `--resume`, so resuming needs no
chain-following. Forks and compacted sessions get a new id, and the hook
payload carries no pointer to the parent. `sessions.parent_session_id` exists
for that case, but 1.0 never fills it: we do not build a lineage tree we
cannot populate.

`session_prs` is many-to-many, so a PR adopted by a second session shows in
both without duplicating the PR row.

## 4. Data model

```sql
PRAGMA journal_mode = WAL;   -- hook, refresh and picker all touch the db
PRAGMA busy_timeout = 3000;
PRAGMA foreign_keys = ON;    -- without this, deleting a PR orphans its rows

CREATE TABLE prs (
  id                INTEGER PRIMARY KEY,
  repo              TEXT    NOT NULL,          -- "owner/name"
  number            INTEGER NOT NULL,
  url               TEXT    NOT NULL,
  title             TEXT,
  head_ref          TEXT,
  base_ref          TEXT,
  head_sha          TEXT,                      -- signature for checks_passed
  body              TEXT,                      -- first 8000 chars, for the preview
  state             TEXT    NOT NULL DEFAULT 'open',   -- open|merged|closed
  is_draft          INTEGER NOT NULL DEFAULT 0,
  mergeable         TEXT,                      -- MERGEABLE|CONFLICTING|UNKNOWN
  review_state      TEXT,                      -- approved|changes_requested|review_required|none
  checks_rollup     TEXT,                      -- SUCCESS|FAILURE|PENDING|NONE
  checks_pass       INTEGER NOT NULL DEFAULT 0,
  checks_fail       INTEGER NOT NULL DEFAULT 0,
  checks_pending    INTEGER NOT NULL DEFAULT 0,
  checks_skip       INTEGER NOT NULL DEFAULT 0,
  stack_id          TEXT,                      -- from `gh stack view --json`, else NULL
  stack_pos         INTEGER,
  local_path        TEXT,                      -- hook's cwd: where `gh stack` can run
  needs_hydrate     INTEGER NOT NULL DEFAULT 1,
  created_at        INTEGER NOT NULL,
  last_refreshed_at INTEGER,
  terminal_at       INTEGER,                   -- set when state leaves 'open'; drives retention
  UNIQUE (repo, number)
);

CREATE TABLE repos (                           -- default branch, for orphan detection
  repo           TEXT PRIMARY KEY,
  default_branch TEXT
);

CREATE TABLE pr_checks (                       -- feeds the preview and the failing-set diff
  pr_id  INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  name   TEXT    NOT NULL,                     -- "Workflow / job" for check runs
  state  TEXT    NOT NULL,                     -- SUCCESS|FAILURE|PENDING|SKIPPED
  url    TEXT,
  PRIMARY KEY (pr_id, name)
);

CREATE TABLE sessions (
  session_id        TEXT PRIMARY KEY,
  parent_session_id TEXT,                      -- reserved; NULL in 1.0
  agent             TEXT NOT NULL DEFAULT 'claude',   -- from `hook --agent`
  cwd               TEXT,
  first_seen_at     INTEGER NOT NULL
);

CREATE TABLE session_prs (
  session_id TEXT    NOT NULL REFERENCES sessions(session_id),
  pr_id      INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  source     TEXT    NOT NULL,                 -- gh|stack|mcp|adopt
  created_at INTEGER NOT NULL,
  PRIMARY KEY (session_id, pr_id)
);

CREATE TABLE events (
  id          INTEGER PRIMARY KEY,
  pr_id       INTEGER NOT NULL REFERENCES prs(id) ON DELETE CASCADE,
  session_id  TEXT    NOT NULL,
  kind        TEXT    NOT NULL,                -- see §7
  signature   TEXT    NOT NULL,                -- dedupe key, see §7
  detail      TEXT,
  created_at  INTEGER NOT NULL,
  consumed_at INTEGER,
  UNIQUE (pr_id, session_id, kind, signature)
);

CREATE TABLE stack_scans (                     -- see §5.1; drained by the refresh
  id           INTEGER PRIMARY KEY,
  session_id   TEXT    NOT NULL,
  local_path   TEXT    NOT NULL,
  requested_at INTEGER NOT NULL,
  done_at      INTEGER,
  found        INTEGER,                        -- PRs the scan recognised; NULL if it could not run
  UNIQUE (session_id, local_path, requested_at)
);

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
-- schema_version, last_tick_at, and failing:<repo> for repos whose refresh is failing

CREATE INDEX events_pending ON events(session_id, consumed_at);
CREATE INDEX session_prs_pr ON session_prs(pr_id);
```

Events are marked consumed, never deleted, so a later delivery cannot hand
the same event over twice.

Schema changes are additive. Tables are created with `IF NOT EXISTS`, and a
column added after a release is listed separately and applied with
`ALTER TABLE` when missing, so any older ledger opens without a migration
step.

## 5. Capture

One command, `pr-tracker hook`, with a mode per hook:

| Hook | Matcher | Mode |
|---|---|---|
| `PostToolUse` | `Bash` | `post-bash`: record if the command created PRs, then deliver events |
| `PostToolUse` | `mcp__.*github.*__create_pull_request` | `post-mcp`: record from the tool response, then deliver events |
| `Stop` | | `stop`: show undelivered events without consuming them |

With no mode, it infers one from the payload's `hook_event_name` and
`tool_name`. Each tool call gets exactly one invocation that both records and
delivers. Two separate hooks, each opening the database on every shell call,
is the cost this avoids.

The [`pr-tracker@ai-toolkit`](https://github.com/wmxscott/ai-toolkit) Claude
Code plugin registers these hooks, adds a `/prs` command, and teaches the
agent when to `adopt`. The README shows the same hooks wired by hand.

**Recording is gated on the command, not on the URL.** A PR URL in the output
of `gh pr view`, `gh pr list` or a `grep` must not create a row. A row is
created only when the command matches `gh pr create`, `gh stack submit` or
`gh stack push`, and then for every distinct
`https://github.com/<owner>/<repo>/pull/<n>` URL in the output. `gh stack`
prints several at once; all are recorded. The command may arrive as a string
or an argv list, and the output is taken from every string anywhere in the
tool response, so the shape of an agent's response doesn't matter.

**MCP responses** carry the new PR's body, and a body that mentions another
PR must not record that one too. So the PR's own `html_url` / `url` field
wins, and only an unstructured response falls back to every URL in the text.

### 5.1 `gh stack` and stack metadata

`gh stack submit` output is not a reliable source of URLs, and
`gh stack view --json` only works inside the checkout. The refresh runs from
a scheduler with no checkout of its own. So the hook, which does have the
right working directory, records `local_path` on every row it creates, and
queues a `stack_scans` row whenever the command was a `gh stack` one, even if
it printed no URLs.

The next refresh drains pending scans: for each `local_path`, it runs
`gh stack view --json` there, records any PRs the hook did not see against
the same session, and fills `stack_id` / `stack_pos`. This is the only place
`gh stack` is consulted.

- A path that no longer exists is marked done and dropped.
- A non-zero exit means the branch isn't part of a stack. That's normal, not
  an error.
- A scan that succeeds but recognises no PRs is logged, because it means
  gh-stack's JSON shape has moved. The parser reads `branches[].pr.url` and
  tolerates a few likely variants.

Repos without `gh stack` simply have no `stack_id`, and the picker infers
stacks from branches alone (§8).

### 5.2 Subagents

Hooks fire inside subagents too, and the payload then carries an `agent_id`.

- **Record** under the payload's `session_id`. For Claude Code that is
  expected to be the parent session's, the one the user can resume, though
  it hasn't been confirmed for every agent.
- **Never deliver** when `agent_id` is present. A subagent draining the
  session's queue would consume events into a context that is discarded when
  it returns, and the main session would never see them. Marking events
  consumed prevents double delivery; this rule prevents misdelivery.

### 5.3 Adopt, and other integrations

`pr-tracker adopt [URL | NUMBER]` attaches an existing PR to a session: a PR
opened in the browser, by an earlier session, or by any path the hooks
missed. With a number or no argument it asks `gh pr view` in the current
checkout, so unlike the hook it does touch the network.

`pr-tracker record`, `drain` and `untrack` expose the rest of the ledger to
integrations the hook doesn't cover. Commands that act on a session take
`--session`, falling back to `$PR_TRACKER_SESSION_ID`, then
`$CLAUDE_SESSION_ID`.

## 6. Refresh

The scheduler is only a ticker; **the tick owns the schedule**. With
Homebrew, `brew services start pr-tracker` runs `pr-tracker refresh` every
60 seconds. From source, a launchd agent (an example ships in `contrib/`) or a
cron entry does the same.

Each invocation:

1. Takes an exclusive, non-blocking `flock` on
   `~/.local/state/pr-tracker/refresh.lock`. If another refresh holds it,
   exits 0.
2. Checks `meta.last_tick_at`. The tick is due when
   `now - last_tick_at >= interval_seconds + uniform(-jitter_seconds, jitter_seconds)`.
   Not due: exits 0. The jitter is re-rolled every tick, so the phase drifts
   and machines never settle into polling in lockstep.
3. Drains pending `stack_scans` (§5.1).
4. Selects PRs that are open or still need their first hydration. PRs that
   are merged or closed and already hydrated are never asked about again.
5. Groups them by repo and asks GitHub with `gh api graphql`, each PR an
   aliased field on one `repository` query. Up to 50 PRs share a query, so a
   typical repo costs one request per tick. **No open PRs means no GraphQL
   query.** Batching is what protects the API; the jitter only spreads the
   phase.
6. Diffs each fresh row against the stored one, writes it, and queues
   events (§7).
7. Deletes PRs whose `terminal_at` is older than `terminal_ttl_days`, with
   their checks, events and session links.
8. Stamps `last_tick_at`.

`pr-tracker refresh --force` and the picker's `R` skip step 2.

**Failures are per repo.** If a repo's query returns nothing usable (auth,
network, rate limit), its rows keep their previous values and their previous
`last_refreshed_at`, so staleness can be seen rather than silently
misleading. Other repos still refresh, and the tick is still stamped, so the
next retry waits for the next due tick instead of hammering a failing API
every minute. The log records when a repo starts failing and when it
recovers, not every tick, and `pr-tracker status` lists repos that are
currently failing.

A PR GitHub reports as `NOT_FOUND` (deleted, or access lost) doesn't fail its
repo. gh exits non-zero, but the rest of the response is still used, and that
PR is marked closed so it stops being asked for and retention reaps it.

Check runs are named `Workflow / job`, since a bare job name like `Deploy`
repeats across workflows and says nothing about which one failed. Status
contexts keep their context name. `NEUTRAL` counts as passing, and `SKIPPED`
and `CANCELLED` as skipped.

## 7. Events and agent notification

When a refresh finds a transition, it queues an event for **every session
that owns the PR**:

| Transition | `kind` | `signature` |
|---|---|---|
| rollup becomes `FAILURE`, or the set of failing checks changes while red | `checks_failed` | sorted names of the failing checks |
| rollup becomes `SUCCESS` | `checks_passed` | `head_sha` |
| review decision becomes `approved` or `changes_requested` | `review_changed` | `<decision>:<head_sha>` |
| `open` becomes `merged` or `closed` | `merged` / `closed` | the new state |

Every rule is a **transition**, not a state. A PR that is still green is not
news. The previous failing set is read from `pr_checks` before it is
overwritten, so a PR that stays red while its failing checks change does
produce a second event.

**The first hydration is silent.** A row recorded moments ago has no earlier
observation to differ from, and "all checks passing" for a PR the agent just
opened is noise. The first refresh writes the baseline and queues nothing.

The signature is the backstop under all of that:
`UNIQUE(pr_id, session_id, kind, signature)` drops a re-observation at the
database level rather than in the diff logic. A PR that stays red across
twenty ticks produces one event.

**Delivery.** After each tool call, the hook drains the session's unconsumed
events into `hookSpecificOutput.additionalContext`, headed
`Tracked pull request updates:`. It is automatic and mid-turn, and the user
types nothing. Nothing is registered on `UserPromptSubmit`, and the user's
prompt is never modified.

**`Stop` is passive.** It shows unconsumed events to the user as a
`systemMessage` and never returns `decision: "block"`. A blocking Stop would
fight the user's turn boundary on unrelated stops, and these events are
informational often enough that forcing a continuation is the wrong default.

Stop also *peeks* rather than consuming. Marking events read there would show
them to the user while the agent that owns the PR never sees them. Leaving
them queued means the next tool call still delivers them into the agent's
context.

`notify.post_tool_use` and `notify.stop_surface` turn either path off, and
`PR_TRACKER_DISABLE=1` turns the whole hook into a no-op.

## 8. The picker

`prs` is read-only over the ledger, apart from `R`. It renders into fzf 0.59
or later; without a usable fzf it prints the list instead.

### Rows

One line per PR: a state glyph (open, draft, merged, closed), the number, the
title, then a status gutter in three groups:

- **what CI thinks:** a rollup verdict, then pass, fail, pending and skipped
  counts, each coloured by its own state rather than by the rollup;
- **what people think:** the review decision, and a delivery badge, `done/total`
  events for this PR, ringing while any are still queued and dim once they
  have all landed;
- **what's wrong with the PR itself:** an orphan marker (below), and
  `stale <age>` once the last good refresh is older than
  `interval_seconds × stale_after_multiple`.

A few rules keep the row honest:

- **The gutter is budgeted, the title is not.** fzf truncates from the end,
  so an unbudgeted row loses its status, the one part a reader can't
  reconstruct. Each row is sized to the list pane, from `FZF_COLUMNS` (or the
  launch-time terminal width, since fzf exports 0 until it has laid itself
  out) minus the preview's share, and the title absorbs the shortfall.
- **Every status cell holds its column** whether or not it has a value, each
  sized once for the whole screen, so nothing shifts from row to row.
- **Unhydrated rows are never blank.** A PR recorded seconds ago shows its
  number and `<repo> (not yet refreshed)` until the next refresh.
- **Terminal PRs never show `stale`.** They are deliberately no longer
  refreshed, so the marker would be an ever-growing clock on something that
  won't change.

The preview shows the branches, review and merge state, every check with
failures first, then the description below a divider. It starts one line
down and wraps the title, because fzf paints its scroll position over the
preview's first line. Merge state appears only for open PRs: GitHub reports
`mergeable` as `UNKNOWN` once a PR is merged or closed, which would otherwise
be a permanent question mark on every PR that landed.

### Stacks

Stacks are built in the picker, not in the refresh, from the `base_ref` /
`head_ref` pairs of PRs in the same repo: if B's base is A's head, B sits
under A. This works the same for `gh stack`, Graphite, or a hand-rolled
stack. When `stack_pos` is known from `gh stack view --json`, it orders the
members, since gh-stack knows the intended order.

A stack renders as **one header row over a single column of spine**, not as
an indented tree. Escalating indentation only earns its columns when a stack
forks. Real stacks are overwhelmingly linear, where depth just restates
position and costs the deepest PR the most title. So every member gets the
same one column, `│` and `└` on the last, whatever its depth.

The header carries no status gutter, just the member count and how many are
**ready** (open, not draft, checks green): the question a stack raises that
its rows don't answer one by one. It is titled with the `stack_id` a
`gh stack` scan recorded (gh-stack's own id, or the checkout path when it
gives none), else the root PR's branch.

A PR whose base is neither the repo's default branch nor another tracked PR's
head renders as a root **with an orphan marker**. Its base was changed, or
its parent merged and the branch was deleted, and it stays distinct from a
PR that was never stacked. Two PRs based on each other's heads have no root;
they are shown rather than dropped.

### Filters

Two independent filters, drawn as pills in the header:

- `s`: this session's PRs, or every tracked PR. On by default when a session
  is known.
- `a`: cycles open → needs attention → merged/closed → all. *Needs
  attention* is an open PR with failing checks, changes requested, or an
  undelivered event.

If the opening view is empty, the picker widens until something is in view,
and the pills show where it landed. **Scope is surrendered before session.**
A session whose PRs have all merged keeps its own list and opens on `all`.
Opening on an empty list is jarring, but quietly substituting another
session's work is worse. Only a session with nothing tracked at all falls
through to every session's PRs. If nothing is tracked anywhere, it says so
and exits.

### Keys and fzf mechanics

The list has focus by default. fzf starts with `--no-input`, so there is no
query field for a stray keystroke to land in, and every letter is free to be
a binding. (`--disabled` isn't enough: it leaves a field accepting text that
filters nothing, which reads as a broken search.) `/` shows the input and
unbinds the list keys so `j`, `k`, `s`, `a` and `y` type as letters; `esc`
hides it, clears the query and rebinds them. Whether the input is showing is
read back from `FZF_INPUT_STATE` rather than tracked locally, so it can't
drift.

`esc` unwinds one layer per press: leave search, else clear the marks, else
quit. The mark count comes from `FZF_SELECT_COUNT`, which fzf exports to
bound commands. While anything is marked, the hint line becomes
`N marked · ↵ open all · y copy all · esc clear`, so what `esc` will do next
is never a guess. Hints get a line of their own because the widest pill
combination plus a full hint line doesn't fit a narrow pane, and a truncated
hint is worse than a taller toolbar.

`space` marks rows. `enter` and `y` act on every marked row through fzf's
`{+2}`, which falls back to the highlighted row when nothing is marked, so
one binding serves both cases. On a stack header they are contextual:
`enter` folds the stack, `space` marks every member, and `y` copies every
URL. A header's id is a group reference, `g:<root id>`, that expands to its
members wherever a PR id is accepted. fzf has no select-by-predicate, but a
stack's members are always the rows directly beneath its header, so marking
one emits `down+select` per member and walks the cursor back. Whether to
select or deselect checks `FZF_SELECT_COUNT` first: with nothing marked,
`{+2}` is the header itself, which expands to the whole stack and would
otherwise look already selected.

Filters apply **in place**. Each key runs a `transform` that updates a
scratch state file and then emits `reload` plus `transform-header`, so the
list and the pills are both rebuilt from state that has already changed.
Relaunching fzf per keystroke would lose the cursor and flash the screen, and
refreshing the header from a separate binding would race the reload.

fzf gets its rows by running the picker's own `--rows` command through
`FZF_DEFAULT_COMMAND` rather than reading a pipe. One code path feeds the
first paint and every reload, quitting early can't break a pipe mid-write,
and fzf never falls back to walking the current directory for want of input.
Bound commands re-run the same Python interpreter with `-P`, so a directory
that happens to contain a `pr_tracker` folder can't shadow the installed
package.

`R` forces a refresh, then marks every pending event delivered, for every
session. That's an explicit "I've dealt with this", trading the agents'
notifications for a quiet badge.

### Colour and glyphs

Colours are Catppuccin Latte or Macchiato, chosen **once at launch** from
`PR_TRACKER_THEME`, then `picker.theme`, then the system appearance, read
from [theme-monitor](https://github.com/wmxscott/theme-monitor)'s state file
if it exists, else from macOS, else light. Nothing polls, and nothing needs
keeping in sync between launches.

The toolbar draws only from the neutral surface steps, with the ink rather
than the ground carrying active versus inactive. Hue is reserved for PR
status, so nothing in the chrome competes with a red check.

Nerd Font glyphs are written as `\uXXXX` escapes, and a test asserts each is
a single non-empty codepoint. Literal Private Use Area characters don't
survive every editor and pipe, and a silently emptied string renders as
nothing at all.

## 9. herdr integration (optional)

pr-tracker doesn't need [herdr](https://herdr.dev), but the two fit: one key
opens `prs` in a herdr popup, scoped to the agent session in the pane you were
looking at.

The one design constraint is **when** the session is resolved. By the time
the picker runs, the popup itself may hold focus, so reading "the focused
pane" from inside the picker would be wrong. The launcher must resolve the
session at action-invoke time, from herdr's plugin context (the focused pane
and its agent), and thread it forward as `PR_TRACKER_SESSION_ID` in the
popup's environment. The picker then reads that variable: set means
session-scoped, unset means every open PR.

[herdr-launchpad](https://github.com/wmxscott/herdr-launchpad) does this
with its `session_env` setting, and `requires_session` offers `prs` only in
panes running an agent. The README also shows a standalone two-file herdr
plugin that does the same.

## 10. Configuration

`~/.config/pr-tracker/settings.toml` (following `$XDG_CONFIG_HOME`, or
`$PR_TRACKER_CONFIG`) is optional; every setting has a working default. The
README lists them.

Two rules shape it:

- **The file is read on every run**, so changes apply straight away with no
  restart. The scheduler's 60-second tick is how often pr-tracker *checks*
  whether a refresh is due; `interval_seconds` is how often one happens.
- **A bad value never stops anything.** It is replaced by its default and
  reported by `pr-tracker status` and `refresh --force`. The hook reads this
  file on every tool call and must not fail over it.

## 11. Failure modes

| Condition | Behaviour |
|---|---|
| `gh` missing, logged out, or offline | That repo is skipped; rows keep their last known state and show `stale` once old enough; `status` lists the failure |
| GitHub rate limit | The same; the next due tick retries |
| Two ticks overlap | `flock`; the second exits 0 at once |
| Database busy | WAL plus a 3-second busy timeout |
| Hook payload has no session id | Nothing is recorded or delivered |
| Hook fails for any reason | Exit 0, no output |
| PR deleted or access lost | Marked closed on `NOT_FOUND`; retention reaps it |
| `gh stack view` changes shape | The scan is logged as finding nothing |
| No session in the environment | The picker opens on every open PR |
| fzf missing or older than 0.59 | `prs` prints the list instead |

## 12. Testing

The logic worth testing is pure, and factored to stay that way:

- **Transitions:** stored row × fresh row → events, including signatures:
  "stays red emits one event", "a changed failing set emits a second",
  "already green emits nothing".
- **Stack building:** `(head_ref, base_ref)` pairs → ordered forest,
  including orphans, cycles, separate repos, and `stack_pos` ordering.
- **Tick gating:** `last_tick_at` and jitter bounds → due or not.
- **Command gating:** command and output → recorded or ignored, including
  `gh pr view` output creating nothing.
- **GraphQL parsing:** response nodes → rows and normalised checks.

Everything else runs end to end against a temporary `HOME`, with a fake `gh`
first on `PATH` that logs every call and answers from a spec. So the tests
never read a real ledger or reach GitHub, and they can assert on network
behaviour directly: one query per repo, no call when nothing is open,
failures that keep stored values and log once, merged PRs no longer polled.
The hook is tested through its real entry point: offline recording, the JSON
contract, subagents recording without draining, Stop peeking, exit 0 on bad
input, a closed stdout, and a broken ledger, plus a timing check on the
common case.

## 13. Out of scope

- Adapters for agents other than Claude Code beyond the generic hook and
  CLI.
- Forges other than GitHub.com.
- Human-facing notifications on PR changes (pull only, by decision).
- Changing PRs from the picker: merging, closing, re-running checks.
- Session lineage across forks (§3.2).
