---
name: gemini-delegation
description: How to act as a manager and delegate work to Gemini (Antigravity CLI) with the gemini_delegate, gemini_analyze, gemini_jobs, gemini_apply, gemini_discard and gemini_usage tools. Use it whenever a task contains mechanical or easily specified work (boilerplate, tests, repetitive refactors, migrations, documentation, small features), when you need to explore large codebases or files, or when the user asks to use Gemini.
---

# Delegating to Gemini: you are the manager

You have access to Gemini through the user's Google subscription. Your context and the Claude quota are the precious resource. Gemini runs are cheap but have a daily budget and less reliable quality. So you think, specify, verify and decide; Gemini executes.

## What to delegate and what to keep

| Delegate to Gemini | Keep for yourself |
|---|---|
| Boilerplate, scaffolding, CRUD, DTOs, types | Architecture and design decisions |
| Writing tests for existing code | Debugging non-trivial or non-reproducible bugs |
| Mechanical, repetitive refactors (rename, extract, migrate an API) | Security code, auth, cryptography, payments |
| Documentation, docstrings, READMEs, comments | Changes that touch many modules in intertwined ways |
| Small features with a clear spec and verifiable criteria | The final review and the decision to apply |
| `gemini_analyze`: exploring codebases, huge files, logs, "where is X done?" | Tasks that cost you less than writing the spec |

Rule of thumb: if you can write the acceptance criteria in a few lines and verify them with a command, delegate.

## The work loop

1. **Understand and plan.** If you need to know a large part of the code, use `gemini_analyze` first instead of reading everything yourself: ask for a summary with `path:line` references, then read only the parts that matter.
2. **Break the work down** into independent tasks that touch different files. Tasks that touch the same files go in sequence, not in parallel.
3. **Delegate** with `gemini_delegate`, one job per task. Several independent jobs can be launched right away in parallel (the server queues them beyond the limit).
4. **While Gemini works**, do your own part (the hard part) instead of waiting.
5. **Collect** with `gemini_jobs` using `wait_seconds` (e.g. 300). For each `done` job, look at the diff.
6. **Actually review.** Check: does it do what the task asked? Did it touch files it wasn't asked to? Does it follow the style? Are there notes or denied actions (soft-deny) in the response? For large diffs you can have the `gemini-reviewer` subagent do a first pass.
7. **Decide:**
   - good → `gemini_apply` (the changes land in the working tree, uncommitted), then run tests/typecheck yourself;
   - almost good → apply and fix the details yourself (often cheaper than a second round);
   - wrong → `gemini_discard` and relaunch with a better spec, or do it yourself.
8. **Final verification** is always yours: tests, build, lint on the main repo.

## Writing a good task

Gemini cannot see your conversation. The `task` field must stand on its own.

- What: a concrete goal in 1-3 sentences.
- Where: `files` with the exact paths; point to an existing file to imitate in `context`.
- Constraints: what NOT to touch, libraries to use/avoid, conventions.
- `acceptance`: verifiable conditions ("`npm test -- user` passes", "function X returns Y for input Z").
- `test_command`: the most targeted command possible, so Gemini can correct itself.

Example:

```
task: "Add input validation to the POST /api/users endpoint in src/routes/users.ts using zod, as already done in src/routes/products.ts. Valid email, password >= 12 characters, non-empty name. On error respond 400 with { errors } in the same format as products."
files: ["src/routes/users.ts", "src/routes/products.ts", "src/routes/users.test.ts"]
acceptance: "New tests in users.test.ts for the 3 invalid cases and one valid case; all tests pass."
test_command: "npm test -- users"
```

## Quota and failures

- Before a big piece of work (many jobs), check `gemini_usage`.
- If a job fails because of **quota/rate limit**, don't insist: do the work yourself and tell the user.
- If it fails for other reasons, read the error: it's often a wrong `test_command` or an ambiguous spec. One retry, then do it yourself.
- If `agy` isn't installed, tell the user once and work normally.
- The `model`/`effort` fields are optional: use a higher effort only for more complex tasks.

## Technical notes

- Every job starts from the current state of the repo, including uncommitted changes (but not untracked files: if they're needed, mention them in the task or commit first).
- The main repo is never touched until you call `gemini_apply`. If the apply fails due to conflicts, the repo has changed too much: relaunch the task on the new state.
- `gemini_analyze` runs in the main repo in read-only mode; if it reports that the state changed, check `git status`.
- At session start you may receive the list of open jobs from previous sessions: handle them (apply or discard) before letting them pile up.
