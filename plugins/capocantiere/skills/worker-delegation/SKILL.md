---
name: worker-delegation
description: How to delegate work to external coding-agent CLIs ("workers" such as GitHub Copilot CLI and Mistral Vibe) with the delegate, analyze, jobs, apply, discard, blame, workers, mode and models tools, how to choose the worker, handle fallbacks and review the result. Use it when a task contains mechanical, easily specified work (boilerplate, tests, repetitive refactors, migrations, documentation, small features) that is worth handing off, when you need to explore large codebases or files, or when the user (or CLAUDE.md) asks to use a worker.
---

# Delegating to workers

Workers are external coding agents (GitHub Copilot CLI, Mistral Vibe, optionally Antigravity CLI) that run on the user's own subscriptions or API keys. They are **one option**, next to doing the work yourself or handing it to a Claude subagent: pick whichever fits each piece of work best.

**Who decides.** The user's instructions come first, whether they are in the conversation or in CLAUDE.md: if they say to use workers more, less, only for some kinds of work, only a specific worker, or not at all, follow them. Without instructions, use your own judgment:

- delegate to a worker when the task is easy to specify and verify, and handing it off saves your context or Claude quota;
- do it yourself when it's quicker than writing the spec, or it needs the conversation's context;
- use a Claude subagent (e.g. Sonnet) when the task is worth delegating but needs more judgment or reliability than a worker usually gives.

Worker runs are cheap but each worker has a daily budget, and their quality is less reliable. Whoever does the work, you stay responsible for the review and the final result.

## Delegation mode

The user can set how much you delegate, globally or per repo. The current mode is in the session-start message (or call `mode`):

- `off`: delegation is disabled for this repo (the tools refuse). Don't try to use it.
- `manual`: delegate only when the user explicitly asks.
- `auto` (default): use your judgment as described here.
- `prefer`: actively look for work to delegate; keep design, delicate parts, review and verification.

Explicit instructions in the conversation or in CLAUDE.md take precedence over the mode, except `off`, which is a hard block. Change the mode with `mode` only when the user asks.

## Good fits for a worker

| Good fit for a worker | Better done by you (or a Claude subagent) |
|---|---|
| Boilerplate, scaffolding, CRUD, DTOs, types | Architecture and design decisions |
| Writing tests for existing code | Debugging non-trivial or non-reproducible bugs |
| Mechanical, repetitive refactors (rename, extract, migrate an API) | Security code, auth, cryptography, payments |
| Documentation, docstrings, READMEs, comments | Changes that touch many modules in intertwined ways |
| Small features with a clear spec and verifiable criteria | The final review and the decision to apply |
| `analyze`: exploring codebases, huge files, logs, "where is X done?" | Tasks that cost you less than writing the spec |

Rule of thumb: if you can write the acceptance criteria in a few lines and verify them with a command, delegate.

## Choosing the worker

The session-start message lists the workers available, their remaining budget and their strengths; `workers` gives the full picture (installed, authenticated, budget used/left, active jobs, model, warnings).

- **Usually omit `worker`.** The server uses the session default (set by the user with `/capocantiere:use`) or the configured preference order, and skips workers that are disabled, not installed or out of budget.
- **Pass `worker` when it clearly matters:**
  - match the task to the worker's `strengths` (tags such as `tests`, `boilerplate`, `docs`, `refactor`, `analysis`);
  - spread parallel jobs across workers when one of them has little budget left or already has jobs running (each worker has its own `max_parallel`);
  - after a failure on one worker, a retry on another one is often better than a second try on the same.
- **The user's choice wins:** if they named a worker (in chat, CLAUDE.md or with `/capocantiere:use`), use it and don't switch on your own except through the automatic fallback.
- **Model:** omit `model` unless you have a reason. Model names differ between workers, and each worker has its own configured default. For `agy`, `model` takes a capability group (`fast`, `standard`, `strong`, `frontier`; see `models`).
- **Never enable a disabled worker** or change the config yourself; tell the user what to change instead. In particular `agy` is disabled by default because using it with a consumer subscription inside third-party agents can get the account suspended.

## The work loop

1. **Understand and plan.** If you need to know a large part of the code, use `analyze` first instead of reading everything yourself: ask for a summary with `path:line` references, then read only the parts that matter.
2. **Break the work down** into independent tasks that touch different files. Tasks that touch the same files go in sequence, not in parallel.
3. **Delegate** with `delegate`, one job per task. Several independent jobs can be launched right away in parallel, also on different workers (the server queues them beyond the limits).
4. **While the workers run**, do your own part (the hard part) instead of waiting.
5. **Collect** with `jobs` using `wait_seconds` (e.g. 300). For each `done` job, look at the diff.
6. **Actually review.** Check: does it do what the task asked? Did it touch files it wasn't asked to? Does it follow the style? Are there notes or denied actions (soft-deny) in the response? For large diffs you can have the `worker-reviewer` subagent do a first pass.
7. **Decide:**
   - good → `apply` (the changes land in the working tree, uncommitted), then run tests/typecheck yourself;
   - almost good → apply and fix the details yourself (often cheaper than a second round);
   - wrong → `discard` and relaunch with a better spec (possibly on another worker), or do it yourself.
8. **Final verification** is always yours: tests, build, lint on the main repo.

## Writing a good task

The worker cannot see your conversation. The `task` field must stand on its own.

- What: a concrete goal in 1-3 sentences.
- Where: `files` with the exact paths; point to an existing file to imitate in `context`.
- Constraints: what NOT to touch, libraries to use/avoid, conventions.
- `acceptance`: verifiable conditions ("`npm test -- user` passes", "function X returns Y for input Z").
- `test_command`: the most targeted command possible, so the worker can correct itself.

Example:

```
task: "Add input validation to the POST /api/users endpoint in src/routes/users.ts using zod, as already done in src/routes/products.ts. Valid email, password >= 12 characters, non-empty name. On error respond 400 with { errors } in the same format as products."
files: ["src/routes/users.ts", "src/routes/products.ts", "src/routes/users.test.ts"]
acceptance: "New tests in users.test.ts for the 3 invalid cases and one valid case; all tests pass."
test_command: "npm test -- users"
```

## Quota, budget, fallback and failures

- Before a big piece of work (many jobs), check `workers` for the budget left on each worker.
- **Fallback.** If a worker hits a quota/rate limit, or its daily budget is used up, the server moves the job to the next available worker (when the fallback setting is on, the default) and says so in the result (`NOTES`, `earlier attempt: ...`). Read those notes and mention the switch to the user in one line. Pass `fallback: false` only when the task must run on that specific worker.
- If every worker is out of quota or budget, the tool fails with "No worker available": do the work yourself and tell the user. Use `force: true` (ignore the budget) only if the user asks.
- Failures for other reasons (timeout, wrong `test_command`, ambiguous spec, per-run cost cap) don't trigger the fallback: read the error, fix the spec, and retry once, preferably on another worker.
- **The same task failed twice → do it yourself.** Don't start a third attempt on any worker, whatever the reason for the failures.
- If a worker isn't installed or authenticated, the `workers` tool tells you what's missing; tell the user once and use the others (or work normally).
- A worker skipped for **isolation or credentials** (e.g. `bwrap needs xdg-dbus-proxy`, `no token`) is skipped on purpose: it must never run without the isolation the user asked for. Don't try to get around it (no `force`, no other settings). Tell the user once and point to `/capocantiere:doctor`. Never install system packages yourself.

## Technical notes

- Every job starts from the current state of the repo, including uncommitted changes (but not untracked files: if they're needed, mention them in the task or commit first).
- The main repo is never touched until you call `apply`. If the apply fails due to conflicts, the repo has changed too much: relaunch the task on the new state.
- Inside the worktree, workers auto-approve their tool calls, with dangerous commands (e.g. `git push`) denied. `analyze` runs in the main repo without auto-approval and with edits denied; if it reports that the repo state changed, check `git status`.
- **`blame`** tells which applied job (worker, model, date, task title) wrote the lines of a file, matching them by content, so it still works after commits and moved code; lines edited after the apply count as not from a worker. Use it when a bug lands in code a worker may have written: reread that job's task (`jobs` with its id, if still listed) to see whether the spec or the worker was at fault. Don't call it routinely.
- At session start you may receive the list of open jobs from previous sessions: handle them (apply or discard) before letting them pile up.
