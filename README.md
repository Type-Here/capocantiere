# Capocantiere — Claude as manager, coding agents as workers

A Claude Code plugin. Claude plans, specifies, reviews and decides; **workers** — external coding-agent CLIs running on your own subscriptions or API keys — write the code for mechanical tasks and analyze large codebases.

Supported workers:

| Worker | CLI | Authentication | Default |
|---|---|---|---|
| `copilot` | [GitHub Copilot CLI](https://docs.github.com/copilot/how-tos/copilot-cli) | `copilot login` (e.g. a Student account) | enabled, first choice |
| `vibe` | [Mistral Vibe](https://github.com/mistralai/mistral-vibe) | your Mistral account (the Free plan works) or `MISTRAL_API_KEY` | enabled, second choice |
| `codex` | [OpenAI Codex CLI](https://developers.openai.com/codex/cli) | `codex login` (a ChatGPT account, any plan, Free included) or an OpenAI API key | **disabled** until you enable it — see [codex](#codex-openai-codex-cli) |
| `agy` | [Antigravity CLI](https://antigravity.google/docs/cli) | Gemini API key, or Google Cloud ADC | **disabled** — see [the warning](#agy-antigravity-cli) |

## What's inside

| Component | Purpose |
|---|---|
| **MCP server `capocantiere`** (Python or Node, zero dependencies) | The tools `delegate`, `analyze`, `jobs`, `apply`, `discard`, `rate`, `blame`, `workers`, `mode`, `models` |
| **Skill `worker-delegation`** | Teaches Claude *when* to delegate, which worker to pick, how to write a task, how to handle fallbacks and how to review the result. Claude loads it on its own when needed |
| **Agent `worker-reviewer`** (Sonnet) | First-pass review of large diffs, to save the manager's context |
| **SessionStart hook** | At the start of each session, tells Claude which workers are available, how much budget each has left and whether there are open jobs from previous sessions |
| **Commands** | `/capocantiere:manager`, `/capocantiere:delegate`, `/capocantiere:analyze`, `/capocantiere:status`, `/capocantiere:review`, `/capocantiere:use`, `/capocantiere:models`, `/capocantiere:mode`, `/capocantiere:doctor`, `/capocantiere:blame`, `/capocantiere:scores` |

### How a job works

1. `delegate` picks a worker: the one requested, else the session default (`/capocantiere:use`), else the first in the configured order that is enabled, installed, authenticated and within its daily budget.
2. It creates an **isolated git worktree** on a `capocantiere/<id>` branch, starting from the current state of the repo (uncommitted changes and untracked files included, except those `.gitignore` excludes and those over 5 MB), and runs the worker's CLI inside it, in the background. Several jobs can run in parallel, on the same or different workers (with a queue and per-worker limits).
3. If the worker hits a **quota / rate limit**, the job's edits are thrown away and it is relaunched on the next available worker (fallback). The job records every attempt.
4. When the work is done, the server commits to the job's branch and saves the diff.
5. Claude reviews it with `jobs`, then either applies it to your working tree (`apply`, no commit) or discards it (`discard`).

**Large diffs.** `jobs` shows a job's diff within a budget (`max_diff_chars`, 15000 by default) without cutting it blindly: every file is listed with its `+/-` lines and flags (new, deleted, binary, symlink, mode change). **Sensitive files** — the ones where a few lines change what gets run: CI (`.github/`, `.gitlab-ci.yml`, ...), git and agent config (`.gitattributes`, `.gitmodules`, `.mcp.json`, `.claude/`, `AGENTS.md`, ...), build and package manifests (`Makefile`, `package.json`, `pyproject.toml`, `Dockerfile`, ...), scripts, symlinks and executable bits — are always shown whole. The other files are shown whole while they fit, and the rest is listed as not shown (`jobs` with `files=[...]` shows just those). The server remembers what Claude was shown: **`apply` refuses a job with a sensitive file whose diff Claude never saw** (unless `unreviewed_ok: true`), and names any ordinary file it applied unseen. A worker can't slip a change to a build script past the review by placing it after 15000 characters of tests.

**Workers never touch your repo directly.** Inside the worktree they auto-approve their own tool calls (the only way to work headless), with safety limits per worker (see below). They still run with your user's permissions unless you turn on [isolation](#isolation-and-credentials). `analyze` runs in the real repo **without** auto-approval, with edits disabled, and checks that the repo state doesn't change.

Each worker has a daily budget (runs per 24h, counted per worker). A worker over budget is skipped; if every worker is out of quota or budget, Claude does the work itself.

## Requirements

- Claude Code
- git
- **Python ≥ 3.8 or Node.js ≥ 18** (only one of them: see [Python or Node](#python-or-node))
- At least one worker CLI, installed and authenticated

## Installation

**1. Install the plugin**

```bash
claude plugin marketplace add /path/to/capocantiere
claude plugin install capocantiere@capocantiere-local
```

(or, inside Claude Code: `/plugin marketplace add /path/to/capocantiere` followed by `/plugin install capocantiere@capocantiere-local`). Restart Claude Code.

**2. Set up the workers you want** (sections below), then check them with `/capocantiere:status` in a session inside a git repo. Nothing needs configuring if the defaults suit you.

### copilot (GitHub Copilot CLI)

1. Install it with the official installer (see the Copilot CLI docs) and run `copilot login` once.
2. Check: `copilot --version`, then `/capocantiere:status` should show `copilot … authenticated: yes`.

How it is run: `copilot -p <prompt> -s --no-ask-user --usage-output-file <tmp>`.

- In the worktree: `--allow-all-tools` — but **not** `--allow-all-paths`, so file access stays limited to the worktree — plus `--deny-tool` for every rule in `deny_tools` (by default `git push`, `git remote`, `git config`, `git worktree`, `git branch/checkout/switch/reset/rebase`, `gh`, `sudo`; deny rules win over allow).
- `--disable-builtin-mcps`, so the built-in GitHub MCP server can't act on your GitHub account; `--secret-env-vars` hides the other workers' API keys from Copilot's shell commands.
- For `analyze`: nothing is auto-approved (Copilot still runs read-only commands) and `--deny-tool write`.
- No folder trust is needed: in `-p` mode an untrusted folder only skips that folder's MCP servers and hooks, which is what we want.
- Usage (AI credits, premium requests, tokens) is read from the usage file and shown in `jobs` and `workers`. The budget counts runs; `max_ai_credits` can cap each run.
- Optional: `"sandbox": true` runs shell commands in Copilot's OS sandbox (on Linux it needs `bwrap`, `slirp4netns` and more; see `copilot help sandbox`).

### vibe (Mistral Vibe)

1. Install it (`uv tool install mistral-vibe`, or the official installer).
2. Sign in: run `vibe` once and log in with your Mistral account in the browser. The Free plan includes limited Vibe use, with no API key and no payment. Alternatively use an API key: `vibe --setup`, export `MISTRAL_API_KEY`, or put it in the worker's `env` in the config file.
3. **Add `outside_workdir = "deny"` to `~/.vibe/config.toml`.** Vibe's default is `ask`, which auto-approve would approve, so a job could touch files outside its worktree. `workers` warns until this is set.
4. Check: `/capocantiere:status` should show `vibe … authenticated: yes (Mistral account, plan FREE …)` (or `MISTRAL_API_KEY set`) and no warning. The account check reads the plan vibe caches in `~/.vibe/whoami_cache.json`; an expired login only shows up on the first run.

How it is run: `vibe --prompt-file <tmp> --output json --output-dir <tmp> --workdir <worktree> --max-turns 40 --max-price 0.5 --time-limit <s>`.

- In the worktree: `--auto-approve`. **Vibe then runs every command, git included:** its `[tools.bash] denylist` is ignored when auto-approving (checked with vibe 3.1.3), so unlike Copilot it cannot be told to keep away from `git branch`, `git push` and the like. Only [isolation](#isolation-and-credentials) keeps a job off the main repo's branches and your credentials; `workers` warns while vibe runs without it.
- For `analyze`: only `--enabled-tools read_file --enabled-tools bash`, without auto-approve (Vibe's default agent would still edit files). Vibe 3.1 has no `grep` tool and searches through bash; without auto-approve bash runs only its read-only allowlist (`rg`, `grep`, `ls`, `git diff/log/status`, ...) and denies everything else, since a headless run cannot ask. Analyses are also capped at `analyze_max_tokens` (200000 by default).
- `--max-price` (USD) and `--max-tokens` cap each run; reaching them fails the job but does **not** trigger the fallback. The cost vibe reports is computed from the prices in its config, so `--max-price` is only an extra guard: the real budget is `daily_budget` (runs per 24h).
- `--time-limit` is passed too (the job's timeout), so vibe stops itself cleanly and still writes its report.
- When the plan's credit is used up (HTTP 402, `usage_limit`) or vibe is rate-limited (HTTP 429), the job counts as a quota error and moves to the next worker. The reason is read from `export.json`, where vibe writes it even when stderr is empty.
- The answer is the last assistant message of the JSON output; outcome, usage and cost come from `export.json`.
- `--trust` is **not** passed by default: with it vibe would read the repo's `AGENTS.md` and project config, which come from the repo (or from an earlier worker in the same worktree) rather than from you. Set `"trust": true` for repos you trust, so that vibe follows their `AGENTS.md`.

### codex (OpenAI Codex CLI)

1. Install it (`npm i -g @openai/codex`, your distribution's package, …) and log in as usual: `codex login` (a ChatGPT account, any plan, Free included, or `--with-api-key`). The worker uses that same login (`"credentials": "host"`).
2. **Keep the login in a file:** put `cli_auth_credentials_store = "file"` in `~/.codex/config.toml` (before logging in, or log in again after adding it). The login is then `~/.codex/auth.json`, and an isolated run needs no D-Bus: the sandbox mounts `~/.codex` read-write (Codex refreshes its token there) and nothing of the keyring. With the login in the keyring it still works, through the keyring proxy like the other workers, and `workers` warns.
3. Enable it: `{ "workers": { "codex": { "enabled": true } } }`. `/capocantiere:status` should show `codex … authenticated: yes (logged in, per ~/.codex/auth.json)`.

How it is run: `codex exec --json -o <tmp> --sandbox <mode> --cd <dir> --ephemeral -` with the prompt on stdin. The answer is the last message (`-o`), token usage comes from the `turn.completed` events. `--ephemeral` keeps these runs out of your session history.

- **Codex's own sandbox stays on, inside capocantiere's isolation** (checked with bwrap on Linux). The two cover different things: Codex's limits what the commands it runs may **write** (the worktree and `/tmp`; not `~/.codex`, not `.git`) and turns their **network** off, but lets them read the whole disk; capocantiere's hides your home folder, `/run` and the main repo's objects from reads. Jobs run `workspace-write` (`"network_access": true` gives the commands network), `analyze` runs `read-only`. `exec` never asks for approval: what its sandbox blocks simply fails.
- Where the kernel forbids nested user namespaces, Codex's sandbox cannot start inside ours: `"sandbox": false` then runs it with `danger-full-access`, which the server allows **only** with `isolation` on.
- `max_parallel` is 1 by default, as a precaution: runs share your login's token refreshes. Raise it if you like.
- Usage limits of the ChatGPT plan ("You've hit your usage limit", HTTP 429) count as quota errors and move the job to the next worker. On the Free plan they come quickly: keep `daily_budget` low.
- `effort` becomes `model_reasoning_effort` (`max` → `xhigh`). Codex does not report which model it used: the arm is the `model` you set (in the worker config or in Codex's `config.toml`), otherwise `codex:default`.
- A project's own `.codex/config.toml` is only read for projects you trusted in Codex, and capocantiere never passes `--trust`. Codex does read the repo's `AGENTS.md`.
- Other credentials: `"token"` gives it `CODEX_API_KEY` (an OpenAI API key, from `~/.local/state/capocantiere/credentials/codex`); `"private-home"` gives it a `CODEX_HOME` of its own, `~/.local/state/capocantiere/homes/codex`, with a separate login (`CODEX_HOME=<that folder> codex -c 'cli_auth_credentials_store="file"' login`), so your `~/.codex` is never mounted.

### agy (Antigravity CLI)

> ⚠️ **Disabled by default.** According to the official Antigravity FAQ, running `agy -p` inside third-party agents is **not supported on consumer plans and can get the account suspended**. Use this worker only with a Gemini API key or Google Cloud credentials, never with the login of a consumer subscription (Google AI Pro, student plan, …).

To enable it with a [Gemini API key from AI Studio](https://aistudio.google.com/apikey):

```json
{ "workers": { "agy": { "enabled": true, "auth": "api_key" } } }
```

and export `GEMINI_API_KEY` (or put it under `"env"` for the worker). With `"auth": "api_key"` every run gets `--settings '{"modelProvider":"gemini"}'`, which applies to that run only, so your interactive `agy` setup is untouched. If the key is missing, the server skips `agy` instead of running it.

Other `auth` values: `"adc"` (Google Cloud Application Default Credentials, sets `AGY_ADC_AUTH=true`) and `"login"` (the signed-in account: it works, but the warning above applies and stays visible in `workers` and at session start).

`agy` keeps its model groups (see [Models](#models)).

## Usage

- `/capocantiere:manager` → for the rest of the session Claude delegates all mechanical work on its own. The most convenient mode.
- `/capocantiere:delegate add the missing tests for src/billing` → plan, parallel delegation, review, apply, verify.
- `/capocantiere:analyze how does the authentication flow work?` → a worker reads, Claude only receives the summary.
- `/capocantiere:review` → reviews and applies/discards all completed jobs.
- `/capocantiere:status` → workers (installed, authenticated, budget, jobs, warnings) and open jobs.
- `/capocantiere:use vibe` → use that worker by default for this session (`/capocantiere:use auto` goes back to the configured order).
- `/capocantiere:mode` → shows or changes how much Claude delegates (see [Delegation mode](#delegation-mode)).
- `/capocantiere:models` → the models of a worker (model groups for `agy`).
- `/capocantiere:scores` → the trust scores of each worker and model (see [Trust scores](#trust-scores-and-tiers)).
- `/capocantiere:blame src/app.js:10-40` → which worker and model wrote those lines (see [Attribution](#attribution-blame)).

Even without the commands, Claude can use the tools and the skill on its own when it finds them useful.

### Who is the manager, and how much it delegates

The manager is whatever model your Claude Code session runs (Opus, Sonnet, Fable…): nothing in the plugin depends on a specific one. Only the `worker-reviewer` agent is pinned to Sonnet, to keep reviews cheap.

The plugin doesn't change Claude's normal behavior much. Workers are one more option, next to doing the work itself or using a Claude subagent, and Claude picks per task. Your instructions win: write in `CLAUDE.md` how much you want workers used (e.g. "delegate tests and docs, preferably to vibe", or "never delegate in this repo") and Claude follows it. Claude also follows one rule of its own: a task that has failed twice is done by Claude, not sent to a worker again.

### Delegation mode

For a lasting setting, use `/capocantiere:mode`. There is a global default and you can override it per repo:

| Mode | Behavior | Enforced by |
|---|---|---|
| `off` | delegation disabled, e.g. for repos whose code must not leave your machine | the server: the delegation tools refuse to run |
| `manual` | delegate only when you ask (in chat, with a command, or in `CLAUDE.md`) | instruction to Claude |
| `auto` *(default)* | Claude decides case by case | instruction to Claude |
| `prefer` | delegate everything that makes sense, like a permanent `/capocantiere:manager` | instruction to Claude |

Examples: `/capocantiere:mode off` (this repo), `/capocantiere:mode prefer global`, `/capocantiere:mode inherit` (this repo goes back to the global default).

The mode is given to Claude at the start of every session. Explicit instructions in chat or in `CLAUDE.md` take precedence over it, except `off`, which is a hard block.

## Configuration

Everything is optional. The config file is `~/.config/capocantiere/workers.json` (`$XDG_CONFIG_HOME` is honored; `CAPOCANTIERE_CONFIG` points elsewhere). It's re-read when it changes, so edits apply without restarting Claude Code. Write only what you change: missing keys use the defaults.

[`plugins/capocantiere/workers.example.json`](plugins/capocantiere/workers.example.json) lists every setting with its default. Keys starting with `_` are comments. A typical file:

```json
{
  "order": ["vibe", "copilot"],
  "workers": {
    "copilot": { "daily_budget": 30, "model": "gpt-5.6-sol" },
    "vibe": { "max_price": 0.25, "strengths": ["analysis", "docs", "tests"] }
  }
}
```

Global settings:

| Setting | Default | Meaning |
|---|---|---|
| `order` | `["copilot", "vibe", "codex", "agy"]` | preference order when no worker is requested |
| `selection` | `"scores"` | `scores`: choose by trust score (the order only breaks ties while there is no history); `order`: always the preference order. See [Trust scores](#trust-scores-and-tiers) |
| `half_life_days` | `30` | trust score events lose half their weight every N days |
| `fallback` | `true` | on quota/rate-limit errors or exhausted budget, move on to the next worker |
| `max_parallel` | `4` | jobs running at once, across all workers |
| `isolation` | `"none"` | `none`, `auto`, `bwrap` or `seatbelt`: see [Isolation and credentials](#isolation-and-credentials) |
| `keyring_dbus_names` | Secret Service, KWallet 5/6 | Linux: the session D-Bus services an isolated worker with `"host"` credentials can reach |

Settings of every worker:

| Setting | Meaning |
|---|---|
| `enabled` | use this worker at all |
| `bin` | path or name of the CLI |
| `model`, `analyze_model` | model for jobs / for analyses (empty = the CLI's default; for `agy` also a group such as `strong`) |
| `effort` | reasoning effort (`low` … `max`), passed with the CLI's own flag |
| `daily_budget` | max runs per 24h for this worker (running jobs included) |
| `max_parallel` | jobs of this worker running at once |
| `timeout_min`, `analyze_timeout_min` | time limits for a job / an analysis |
| `extra_args` | extra command-line arguments |
| `env` | extra environment variables for the CLI (e.g. an API key) |
| `strengths` | tags Claude uses to choose a worker: `tests`, `boilerplate`, `docs`, `refactor`, `analysis`, … |
| `credentials` | `host` (default), `token`, or `private-home` (codex only): see [Isolation and credentials](#isolation-and-credentials) |
| `isolation` | overrides the global setting for this worker (empty = global) |
| `tiers` | models per capability tier, e.g. `{"fast": "<model>", "strong": ["<model>", "<model>"]}` (empty = every tier uses `model`) |
| `isolation_rw`, `isolation_ro`, `isolation_env` | extra paths an isolated worker may write / read, extra environment variables it keeps |

Worker-specific settings: `copilot` — `deny_tools`, `disable_builtin_mcps`, `sandbox`, `max_ai_credits`, `secret_env_vars`; `vibe` — `max_turns`, `max_price`, `max_tokens`, `analyze_max_tokens`, `trust`, `analyze_tools`, `disabled_tools`; `codex` — `sandbox`, `network_access`; `agy` — `auth`, `sandbox`, `skip_permissions`.

**Environment variables** override the file, e.g. in `~/.claude/settings.json` under `"env"`: `CAPOCANTIERE_<WORKER>_<SETTING>` (`CAPOCANTIERE_VIBE_MAX_PRICE=1`, `CAPOCANTIERE_COPILOT_ENABLED=0`; lists are comma-separated), `CAPOCANTIERE_ORDER`, `CAPOCANTIERE_FALLBACK`, `CAPOCANTIERE_MAX_PARALLEL`, `CAPOCANTIERE_SELECTION`, `CAPOCANTIERE_HALF_LIFE_DAYS`, plus `CAPOCANTIERE_RUNTIME` (`python`, `node` or `auto`). The old `GEMINI_DELEGATE_*` variables still apply to `agy`.

`MCP_TOOL_TIMEOUT` (in ms) is only needed if long analyses get cut off by Claude Code; jobs are asynchronous and aren't affected.

## Isolation and credentials

By default (`"isolation": "none"`) a worker runs with all your permissions: its own safety rules (Copilot's deny rules, Vibe's `outside_workdir`) are all that keep it inside the job's worktree. With isolation on, the server starts every run of that worker inside a sandbox. **Isolation, `token` / `private-home` credentials and the `codex` worker are implemented in the Python server only**; the Node server refuses those settings and skips the workers that use them.

```json
{ "isolation": "auto" }
```

`auto` picks the backend of the system you're on; you can also name it (`bwrap`, `seatbelt`), globally or per worker. **If the backend can't work** (a missing tool, another OS), **the worker is skipped with a note**: a run never starts without isolation when you asked for it. `/capocantiere:doctor` checks what is missing and shows the install command for your distribution (pacman, apt, dnf, zypper, apk). It never installs anything or runs sudo.

What an isolated run sees:

- the system, read-only; your home folder, other users' homes, `/mnt`, `/media`, `/run` and the rest of `/tmp` are hidden. Hiding `/run` keeps the worker away from the sockets of local services (Docker, the system D-Bus, systemd); only the folder of `/etc/resolv.conf` stays, when systemd-resolved keeps it there;
- the job's worktree, read-write (for an analysis, the repo read-only), except its `.git` file. Of the main repo's `.git`, only what git needs: everything read-only, except this worktree's own admin folder. Branches, refs and the repo's objects can't be changed: the worker's git writes new objects to a throwaway store of its own, deleted after the run;
- the worker CLI's own folders (`~/.copilot` and `~/.cache/copilot`, `~/.vibe`, `~/.codex`, `~/.gemini`; with `private-home`, only that private folder) read-write, its install folder read-only, plus `isolation_rw` / `isolation_ro`;
- the network, which the model APIs need;
- an environment rebuilt from scratch: locale, proxy and certificate variables, the CLI's own variables, `isolation_env`. Other workers' keys, your tokens and everything else are left out.

| System | Backend | Status |
|---|---|---|
| Linux | `bwrap` ([bubblewrap](https://github.com/containers/bubblewrap)), plus `xdg-dbus-proxy` for `host` credentials | tested |
| macOS | `seatbelt`: `sandbox-exec` with a profile generated for each run | **experimental, untested** |
| Windows | none yet | run the workers inside **WSL2** to use the Linux backend |

### Credentials

Each worker has `"credentials"`:

- **`host`** (default): the login you already made in the CLI (`copilot login`, your Mistral account in Vibe). Both CLIs keep it in the system keyring. Nothing to configure.
- **`token`**: a minimal credential the server injects into **that worker only**: Copilot gets `COPILOT_GITHUB_TOKEN` (a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new) with only the **Copilot Requests** permission), Vibe gets `MISTRAL_API_KEY` and agy gets `GEMINI_API_KEY`. The server reads it from `~/.local/state/capocantiere/credentials/<worker>` (must be `chmod 600`; otherwise the worker is skipped) or from `CAPOCANTIERE_<WORKER>_TOKEN`, never from the config file. Your `host` login is not touched; the two coexist. `CAPOCANTIERE_*_TOKEN` variables are never passed to any child process as they are.
- **`private-home`** (codex only): a config/auth folder of the CLI's own, `~/.local/state/capocantiere/homes/<worker>`, with a login you make there once. Only that folder is mounted in the sandbox, and the login is a file, so there is no D-Bus either. See [codex](#codex-openai-codex-cli).

With `host` credentials on Linux the CLI must reach the keyring through the session D-Bus. The sandbox never gets the full session bus, because that would allow escaping the sandbox (for example through `systemd --user`). `xdg-dbus-proxy` lets through only the keyring services in `keyring_dbus_names`: `org.freedesktop.secrets` (GNOME Keyring, KeePassXC, KDE Wallet 6) and `org.kde.kwalletd6` / `kwalletd5`. Copilot and Vibe both use `org.freedesktop.secrets`. With `token` or `private-home` credentials, or codex with its login in a file, the sandbox has no D-Bus at all.

On macOS, the Keychain checks access item by item, so a CLI reads its own login. Items created by the same program may also be readable: for Vibe, that means other tools run by the same Python.

### Limits (also shown by `workers`)

- **Linux with `host` credentials: the worker can read every secret in your keyring** (the Secret Service has no per-application control). Use `token` credentials to avoid this.
- **The network is open:** a worker can send data out. Sharing the network also shares the loopback, so **services listening on `127.0.0.1`** (databases, dev servers) and abstract Unix sockets (X11, a session D-Bus at an abstract address) are reachable from the sandbox.
- **A worker always sees its own credential.** That is why the `token` credentials are as narrow as possible.
- **macOS is experimental.** `sandbox-exec` is deprecated by Apple, but it is the same mechanism other agent sandboxes use. If Apple removes it, the backend stops working (and the worker is skipped). Windows has no isolation.

### Checking it on your machine

`python3 scripts/sandbox-probe.py` builds the sandboxes with the server's own code and checks what leaks:
- a decoy file and a decoy variable;
- `~/.ssh` and `~/.claude`;
- the main repo;
- on Linux, other D-Bus services and `systemd-run --user`; on macOS, launchd and LaunchServices;
- whether DNS, TLS and git work.

With `--prompt` it also sends one tiny prompt per worker (after asking), and lists which D-Bus services each CLI reached and which were blocked. On a Mac, this is how the `seatbelt` backend gets tested.

## Models

Each worker uses its CLI's default model unless `model` / `analyze_model` is set, or Claude passes `model` for one run (model names differ between CLIs, so Claude normally leaves it out).

For `agy` the plugin reads `agy models` (only what your account can use, refreshed every 6 hours) and sorts the models into capability groups by name rules in [`server/model-groups.json`](plugins/capocantiere/server/model-groups.json):

| Group | What goes in it | Used for |
|---|---|---|
| `fast` | Gemini Flash (low/medium), Flash-Lite | analyses (default) and simple mechanical tasks |
| `standard` | Gemini Flash (high) | implementation jobs (default) |
| `strong` | Gemini Pro, Claude Sonnet | harder tasks, retries after a failure |
| `frontier` | Gemini Ultra / Deep Think, Claude Opus | only the hardest delegated tasks |
| `other` | anything not recognized | never picked automatically |

`/capocantiere:models` shows the list and changes the `agy` defaults: a group follows new models automatically, an exact slug pins a model, `exclude` keeps models from ever being picked automatically, `auto` goes back to the default choice.

## Trust scores and tiers

Capocantiere learns which worker and model does which kind of task well, and uses it to choose the worker when none is requested. It costs Claude no extra calls: the signals come from calls it makes anyway.

- **Arm:** the unit that is scored is `worker:model`, with the model the CLI reports it used (Copilot's usage file, Vibe's `export.json`; for `agy` the model the server resolved). A worker running on its CLI's default model is scored under the model it last reported for that default, so when the CLI changes its default, the new model starts fresh. If the CLI never says, the arm is `worker:default`.
- **Kinds:** `implement` (default), `tests`, `refactor`, `docs` (the `kind` of `delegate`) and `analysis` (every `analyze`).
- **Votes (0..1):**

  | Event | Vote |
  |---|---|
  | `apply` (`quality`: `good`, the default / `minor_fixes` / `major_fixes`) | 1.0 / 0.6 / 0.3 |
  | `discard` of a finished job (`reason`: `bad`, the default) | 0.0 |
  | `discard` with `reason: "obsolete"` (the plan changed) | no vote |
  | job failed (not on a quota), timed out, or ended with no changes | 0.0 |
  | quota / rate limit, isolation or install problems | no vote |
  | `analyze` | only with an explicit `rate` (its id is in the footer) |

  Only the latest event of each job counts: applying a failed job's partial work replaces its 0, and `rate` (`good`, `minor_fixes`, `major_fixes`, `bad`, or `obsolete` to withdraw) corrects any vote.
- **Score:** a Beta distribution per arm and kind. Each vote weighs `0.5 ** (age_days / half_life_days)`. The prior of a kind is the arm's overall score (`alpha0 = 1 + 4p`, `beta0 = 1 + 4(1 - p)`), so a kind with few votes stays close to how the model does in general. An arm never seen starts slightly optimistic (mean 0.6, a little higher for workers earlier in `order`), so new arms get tried.
- **Choice:** only when neither the user nor Claude asks for a worker (an explicit `worker`, the session default from `/capocantiere:use` and the delegation mode always win). Each candidate arm draws `theta ~ Beta(alpha, beta)` for the task's kind and the arms are tried in decreasing `theta` (Thompson sampling): strong arms come first almost always, uncertain ones sometimes, weak ones almost never. The fallback on quota walks down the same list. `jobs` shows the draws in one line.
- **Where:** raw events are appended to `scores.jsonl` in the state folder and the scores are computed when read, so the formula can change without losing the history.
- **Seeing them:** `workers` shows one line per arm (mean and weighted number of votes for its most used kind); `/capocantiere:scores` shows the detail. The session-start message leaves them out.

The trust score doesn't replace the review: a worker with a high score is reviewed like the others.

**Tiers.** `delegate` and `analyze` take an optional `tier` (`fast`, `standard`, `strong`, `frontier`; default `standard` for jobs and `fast` for analyses). A worker with `"tiers"` in its config serves the tiers it lists, with those models (a list means one arm per model) and is skipped for a requested tier it doesn't list; a worker without `"tiers"` serves every tier with its `model` setting, so nothing changes until you configure them. For `agy` a tier is the model group of the same name. An explicit `model` wins over the tier. `effort` is a separate setting.

## Attribution (blame)

Every `apply` records which worker and model produced the code, so you can later ask who wrote a line. The record is kept **per repo, outside the git history**: in `.git/capocantiere/attribution.jsonl` (the common git dir, shared by worktrees). It never enters a commit or a push, and it goes away with the repo.

- **What is recorded:** one line per applied job, in the [Agent Trace](https://agent-trace.dev) record format (v0.1.0): the files and line ranges the job added, as they sit in the working tree right after the apply, with `model_id` = `<worker>/<model>`. Fields outside that schema (job id, task title, worker, model, base commit, patch hash, and one content fingerprint per added line) go under `metadata["dev.capocantiere"]`.
- **Which model:** the one the CLI reports having used, when it reports it (Copilot: the usage file's `modelMetrics`; Vibe: the resolved config in `export.json`). Otherwise the model the server asked for, or `default`. `jobs` shows it as "ran as ..." when it differs from the requested one.
- **How `blame` answers:** each line of the file is matched **by content** (the line with leading and trailing whitespace removed) against the fingerprints of the jobs applied to that file, newest first. So it keeps working after commits, rebases and moved code, and a line edited after the apply is no longer attributed to the worker. Trivial lines (blank, brackets, fewer than 3 letters or digits) are never recorded; in the output they join the lines around them. Consecutive lines with the same author are grouped into one range.
- **Limits:** a file renamed after the apply loses its attribution (records are per path), and two jobs adding an identical line both "own" it: the newest wins.

`blame` is a tool Claude can call on its own (for example when a bug lands in code a worker wrote) and the `/capocantiere:blame <file>[:start-end]` command.

## Python or Node

The MCP server exists in two versions in `plugins/capocantiere/server/`: `server.py` (with `common.py` and `workers.py`, standard library only) and `server.mjs` (no npm packages). They have the same tools and behavior, share `tools.json`, `modes.json` and `model-groups.json`, and use the same state files, so you can switch between them at any time without losing jobs.

`run.sh` starts the right one: `python3` if it's on your `PATH`, otherwise `node`. To force a choice, set `CAPOCANTIERE_RUNTIME` to `python` or `node`.

The Python version is the reference: it is the one covered by the test suite. The Node version is a port, not tested directly, and it doesn't support isolation or `token` credentials (it refuses those settings rather than ignoring them).

**The Node server is frozen:** it keeps working for those who use it, but new features are implemented in the Python server only. Changes to the core behavior (how jobs are created, run and applied) still go into both, so a job behaves the same on either. Attribution (`blame`, and the records written at `apply`) is the first of them: the Node server does not list the tool and records nothing. Trust scores and tiers are the second: the Node server always follows `order`, does not list `rate` nor the `kind`, `tier`, `quality`, `reason` and `scores` parameters, and reports `selection`, `half_life_days` and `tiers` in its config as unsupported (in `workers`).

## Tests

```bash
python3 tests/e2e.py              # Python server
python3 tests/e2e.py -k fallback  # only some tests
python3 tests/e2e.py --runtime node
```

End-to-end tests over MCP stdio, against fake `copilot`, `vibe`, `codex` and `agy` binaries in [`tests/fakes`](tests/fakes) (a marker in the task picks the scenario: success with edits, no changes, quota error, generic error, timeout). Every test uses its own temporary repo and state folders; no real worker CLI is ever started.

Isolation is tested in two ways. Fake `bwrap`, `xdg-dbus-proxy` and `sandbox-exec` (in `tests/fakes/sandbox`) check the command lines, the profile and where tokens go. The real `bwrap`, where it works, runs a fake worker and checks that it can't read files outside its sandbox.

## Where files go

- State, jobs, patches, usage log (per worker), `agy` model list, mode settings: `~/.local/state/capocantiere/`
- Temporary worktrees: `~/.cache/capocantiere/worktrees/` (removed after apply/discard)
- Both folders are made private (`700`): jobs hold your tasks, the workers' answers and the patches, and worktrees hold your code.
- The server's own git commands on a job's worktree (collecting the changes, resetting it for a fallback) don't trust anything the worker could have written there: the worktree's `.git` file, its HEAD, index and worktree config are put back to the server's, and hooks and `core.fsmonitor` are off. A worker can't make the server run a command or commit to another branch.
- Config: `~/.config/capocantiere/workers.json`
- Trust score events: `scores.jsonl` in the state folder
- Attribution records: `.git/capocantiere/attribution.jsonl` inside each repo (see [Attribution](#attribution-blame))

Upgrading from the Gemini-only version (`gemini-delegate`): the old state in `~/.local/state/gemini-delegate/` is moved on first start; old jobs and old usage count as `agy`. Worktrees of old jobs stay where they were until those jobs are applied or discarded.

## Known limitations

- The plugin starts the server through `sh run.sh`, so `sh` must be on the `PATH`. It always is on Linux and macOS; on Windows it has only been designed for, not tested.
- Build artifacts (`__pycache__`, `*.pyc`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `node_modules`, `.DS_Store`) are never included in a job's diff. Workers are also asked to delete the temporary files they create.
- A job's base is a snapshot of your working tree, built in a temporary copy of the git index: untracked files are included unless `.gitignore` excludes them or they are over 5 MB (the job's notes list those). An untracked file that is not ignored (say, a stray `.env`) is therefore visible to the worker.
- Budgets count runs, not the model's internal requests or tokens (each run makes several). Keep them conservative; the per-run caps (`max_price`, `max_ai_credits`) limit the cost of a single run.
- Authentication checks don't call any model: Copilot is "authenticated" when its config records a logged-in user (or a token variable is set), Vibe when its cache records a signed-in account or `MISTRAL_API_KEY` is set, Codex when its `auth.json` exists. An expired login only shows up on the first run.
- Without isolation, Copilot's shell commands are not limited to the worktree by path checks (only file tools are). The deny rules and, optionally, `sandbox` cover the dangerous ones. Turn on [isolation](#isolation-and-credentials) to contain them.
- When Vibe runs on its default model, it does not report which model that is (`active_model` is empty in its `export.json`), so all its votes go to the arm `vibe:default` and a change of Vibe's default model is not noticed. Setting `model` for the vibe worker gives each model its own score. Copilot reports the model it used, so it does not have this problem.
- After the server is updated and reconnected with `/mcp`, Claude Code may keep the old definitions of the tools it already knew (new tools do appear). New parameters still reach the server, but Claude may not know about them until a new session.
- After a fallback, the next worker uses its own default model (a model name rarely means the same on another CLI).
- The CLI flags were checked against GitHub Copilot CLI 1.0.114, Mistral Vibe 3.1.3 (help, config schema and source), OpenAI Codex CLI 0.171.0 and Antigravity CLI 1.5.10. If a CLI changes its output, the server still shows the raw output.

## Why "Capocantiere"

Il nome viene da una conversazione con Claude, nel browser:

> **Io:** mi dai un nome per il progetto?
>
> **Claude:** Ti consiglio **Capocantiere**. È esattamente lo schema: Opus dirige il cantiere, assegna i lavori, controlla e firma, mentre Gemini fa la manovalanza. Funziona bene anche da comando: `/capocantiere:manager`, `/capocantiere:delegate`.

*(A* capocantiere *is an Italian site foreman: the one who runs the construction site, hands out the work, checks it and signs off, while the crew does the heavy lifting. Gemini was the first crew; now the crew is configurable.)*
