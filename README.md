# Capocantiere — Opus as manager, Gemini as worker

A Claude Code plugin. Claude (Opus) plans, specifies, reviews and decides; **Gemini**, through Antigravity CLI (`agy`) and your Google AI Pro subscription (the student plan works too), writes the code for mechanical tasks and analyzes large codebases.

## What's inside

| Component | Purpose |
|---|---|
| **MCP server `gemini-delegate`** (Python or Node, zero dependencies) | The tools `gemini_delegate`, `gemini_analyze`, `gemini_jobs`, `gemini_apply`, `gemini_discard`, `gemini_usage` |
| **Skill `gemini-delegation`** | Teaches Claude *when* to delegate, how to write a task for Gemini and how to review the result. Claude loads it on its own when needed |
| **Agent `gemini-reviewer`** (Sonnet) | First-pass review of large diffs, to save Opus's context |
| **SessionStart hook** | At the start of each session, tells Claude whether `agy` is available, how much budget is left and whether there are open jobs from previous sessions |
| **Commands** | `/capocantiere:manager`, `/capocantiere:delegate`, `/capocantiere:analyze`, `/capocantiere:status`, `/capocantiere:review` |

### How a job works

1. `gemini_delegate` creates an **isolated git worktree** on a `gemini/<id>` branch, starting from the current state of the repo (uncommitted changes included).
2. It runs `agy -p … --output-format json` inside it, in the background. Several jobs can run in parallel (with a queue).
3. When the work is done, it commits to the job's branch and saves the diff.
4. Claude reviews it with `gemini_jobs`, then either applies it to your working tree (`gemini_apply`, no commit) or discards it (`gemini_discard`).

**Gemini never touches your repo directly.** Inside the worktree, `agy` runs with `--dangerously-skip-permissions --sandbox` (the only way to let it work without a human approving each step), while `gemini_analyze` runs in the real repo **without** auto-approval and checks that the repo state doesn't change.

The server also counts runs over the last 24h and blocks delegation beyond a budget (default 150). If Gemini responds with 429/quota errors, the skill tells Claude to stop delegating and do the work itself.

## Requirements

- Claude Code
- git
- **Python ≥ 3.8 or Node.js ≥ 18** (only one of them: see [Python or Node](#python-or-node))
- Antigravity CLI (`agy`), logged in with your Google AI Pro account

## Installation

**1. Install Antigravity CLI and log in**

Follow the official guide: https://antigravity.google/docs/cli. Then run `agy` once interactively to log in with your Google account. Check it works with:

```bash
agy -p "reply only: ok" --output-format json
agy models     # list the available model slugs
```

**2. Install the plugin**

Clone or unzip the repo wherever you like, then:

```bash
claude plugin marketplace add /path/to/capocantiere
claude plugin install capocantiere@capocantiere-local
```

(or, inside Claude Code: `/plugin marketplace add /path/to/capocantiere` followed by `/plugin install capocantiere@capocantiere-local`). Restart Claude Code.

**3. Verify**

In a session inside a git repo: `/capocantiere:status`.

## Usage

- `/capocantiere:manager` → for the rest of the session Claude delegates all mechanical work on its own. The most convenient mode.
- `/capocantiere:delegate add the missing tests for src/billing` → plan, parallel delegation, review, apply, verify.
- `/capocantiere:analyze how does the authentication flow work?` → Gemini reads, Claude only receives the summary.
- `/capocantiere:review` → reviews and applies/discards all completed jobs.

Even without the commands, Claude can use the tools and the skill on its own when it finds them useful.

## Python or Node

The MCP server exists in two equivalent versions in `plugins/capocantiere/server/`: `server.py` (standard library only) and `server.mjs` (no npm packages). They have the same tools and behavior, share `tools.json` for the tool definitions, and use the same state files, so you can switch between them at any time without losing jobs.

`run.sh` starts the right one. By default it uses `python3` if it's on your `PATH`, otherwise `node`. To force a choice, set `CAPOCANTIERE_RUNTIME` to `python` or `node` (see below).

## Configuration (optional)

Environment variables, e.g. in `~/.claude/settings.json` under `"env"`:

| Variable | Default | Meaning |
|---|---|---|
| `CAPOCANTIERE_RUNTIME` | `auto` | `python`, `node`, or `auto` (python3 if available, otherwise node) |
| `GEMINI_DELEGATE_MODEL` | agy's default | model slug for tasks (see `agy models`) |
| `GEMINI_DELEGATE_ANALYZE_MODEL` | = MODEL | model for `gemini_analyze` (e.g. a faster one) |
| `GEMINI_DELEGATE_EFFORT` | agy's default | `low` … `max` |
| `GEMINI_DELEGATE_DAILY_BUDGET` | `150` | max agy runs per 24h |
| `GEMINI_DELEGATE_MAX_PARALLEL` | `3` | concurrent jobs |
| `GEMINI_DELEGATE_TASK_TIMEOUT_MIN` | `20` | timeout for a job |
| `GEMINI_DELEGATE_ANALYZE_TIMEOUT_MIN` | `8` | timeout for an analysis |
| `GEMINI_DELEGATE_SANDBOX` | `1` | pass `--sandbox` to agy |
| `GEMINI_DELEGATE_SKIP_PERMISSIONS` | `1` | auto-approval inside the isolated worktree |
| `GEMINI_DELEGATE_AGY_BIN` | `agy` | path to the binary |
| `GEMINI_DELEGATE_EXTRA_ARGS` | — | extra arguments for agy |

Example:

```json
{
  "env": {
    "GEMINI_DELEGATE_DAILY_BUDGET": "120",
    "GEMINI_DELEGATE_MAX_PARALLEL": "2",
    "MCP_TOOL_TIMEOUT": "600000"
  }
}
```

`MCP_TOOL_TIMEOUT` (in ms) is only needed if long analyses get cut off by Claude Code; write jobs are asynchronous and aren't affected.

## Where files go

- State, jobs, patches and usage log: `~/.local/state/gemini-delegate/`
- Temporary worktrees: `~/.cache/gemini-delegate/worktrees/` (removed after apply/discard)

## Known limitations

- The plugin starts the server through `sh run.sh`, so `sh` must be on the `PATH`. It always is on Linux and macOS; on Windows it has only been designed for, not tested.
- **Untracked** files are not copied into the job's worktree: `git add` or commit them first, or mention them in the task.
- The budget counts `agy` runs, not the model's internal requests (each run makes several). Keep it conservative and raise it if it holds up.
- The `agy` JSON format is the one documented as of November 2026; if Google changes it, the server still shows the raw output.

## Perché "Capocantiere"

Il nome viene da una conversazione con Claude, nel browser:

> **Io:** mi dai un nome per il progetto?
>
> **Claude:** Ti consiglio **Capocantiere**. È esattamente lo schema: Opus dirige il cantiere, assegna i lavori, controlla e firma, mentre Gemini fa la manovalanza. Funziona bene anche da comando: `/capocantiere:manager`, `/capocantiere:delegate`.

*(A* capocantiere *is an Italian site foreman: the one who runs the construction site, hands out the work, checks it and signs off, while the crew does the heavy lifting.)*
