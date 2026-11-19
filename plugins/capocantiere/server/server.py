#!/usr/bin/env python3
# capocantiere: MCP server that lets Claude Code (the "manager") delegate work to external
# coding-agent CLIs ("workers": GitHub Copilot CLI, Mistral Vibe, Antigravity CLI).
#
# Python port of server.mjs, with the same behavior and the same state files,
# so you can switch between the two runtimes without losing jobs.
#
# - Every code-writing task runs in an isolated git worktree, on a dedicated branch.
#   The main repo is never touched until Claude calls `apply`.
# - Jobs are asynchronous and can run in parallel (with a queue, and a limit per worker).
# - Usage is tracked per worker so each one's daily budget isn't exceeded. On quota/rate-limit
#   errors the job can move on to the next worker (fallback).
#
# Zero dependencies: only Python >= 3.8 (standard library) and git are required.

import hashlib
import json
import os
import re
import shutil
import signal
import sys
import threading
import time

import common
from common import (JOBS_DIR, PATCH_DIR, SETTINGS_FILE, USAGE_FILE, CONFIG_FILE, HERE, SIGKILL, iso_now, kill_tree,
                    new_id, not_found, now_ms, read_json, run, tail, truncate, write_json)
import workers as W

VERSION = "2.0.0"
TERMINAL = {"done", "no_changes", "failed", "applied", "discarded", "interrupted"}

# The worker chosen with `workers use=...` (/capocantiere:use). One server process = one Claude session.
SESSION = {"default": None}


def git(cwd, *args):
    r = run("git", args, cwd=cwd)
    if r["code"] != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r["stderr"].strip() or r["stdout"].strip()))
    return r["stdout"]


# serialize git operations that touch worktrees/branches
GIT_LOCK = threading.Lock()


def repo_root(d=None):
    start = d or common.env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    r = run("git", ["rev-parse", "--show-toplevel"], cwd=start)
    if r["code"] != 0:
        raise RuntimeError('"%s" is not a git repository. capocantiere requires git.' % start)
    return r["stdout"].strip()


def current_repo():
    try:
        return repo_root()
    except Exception:
        return None


# ---------------------------------------------------------------- usage / budget (per worker)

USAGE_LOCK = threading.Lock()


def record_usage(entry):
    line = json.dumps(dict({"ts": now_ms()}, **{k: v for k, v in entry.items() if v is not None or k == "usage"}),
                      ensure_ascii=False)
    with USAGE_LOCK, open(USAGE_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def empty_usage():
    return {"runs": 0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "failures": 0, "quota_errors": 0, "cost": 0.0}


def usage_by_worker(hours=24):
    """{worker id: totals} over the last `hours`. Entries written before workers existed count as agy."""
    since = now_ms() - hours * 3600_000
    out = {}
    if not os.path.exists(USAGE_FILE):
        return out
    with open(USAGE_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or (e.get("ts") or 0) < since:
                continue
            u = e.get("usage") or {}
            o = out.setdefault(e.get("worker") or "agy", empty_usage())
            o["runs"] += 1
            o["turns"] += e.get("turns") or 0
            for k in ("input_tokens", "output_tokens", "total_tokens"):
                o[k] += u.get(k) or 0
            o["cost"] += e.get("cost") or 0
            if not e.get("ok"):
                o["failures"] += 1
            if e.get("quota"):
                o["quota_errors"] += 1
    return out


def active_jobs(worker_id=None, statuses=("running", "queued")):
    return [j for j in list(jobs.values()) if j["status"] in statuses and (worker_id is None or j.get("worker") == worker_id)]


def budget_state(w, usage=None):
    u = (usage if usage is not None else usage_by_worker(24)).get(w.id) or empty_usage()
    committed = u["runs"] + len(active_jobs(w.id))  # includes jobs still running or queued
    return {"usage": u, "committed": committed, "budget": w.s["daily_budget"],
            "left": max(w.s["daily_budget"] - committed, 0)}


# ---------------------------------------------------------------- choosing a worker


def candidate_order(requested, fallback):
    order = W.load_config()["order"]
    first = requested or SESSION["default"]
    if not first:
        return order
    rest = [w for w in order if w != first]
    # an explicit request without fallback means exactly that worker
    return [first] + (rest if fallback else [])


def pick_worker(requested=None, force=False, fallback=None, exclude=()):
    """Choose the worker for a run. Returns (worker, notes). Raises if none is usable.

    Skips workers that are disabled, not installed, or out of budget (unless force); every skipped
    worker that was asked for explicitly, or that sits before the chosen one, is explained in notes.
    """
    cfg = W.load_config()
    if requested and requested not in W.ADAPTERS:
        raise RuntimeError('Unknown worker "%s". Workers: %s.' % (requested, ", ".join(W.ADAPTERS)))
    fallback = cfg["fallback"] if fallback is None else fallback
    usage = usage_by_worker(24)
    notes = []
    for wid in candidate_order(requested, fallback):
        if wid in exclude:
            continue
        w = W.get_worker(wid)
        if not w.enabled:
            if wid == requested or wid == SESSION["default"]:
                notes.append("%s is disabled in %s." % (wid, CONFIG_FILE))
            continue
        installed, _ = w.version()
        if not installed:
            notes.append('%s skipped: command "%s" not found.' % (wid, w.bin))
            continue
        problem = w.blocker()
        if problem:
            notes.append("%s skipped: %s." % (wid, problem))
            continue
        b = budget_state(w, usage)
        if b["left"] <= 0 and not force:
            notes.append("%s skipped: daily budget used up (%d/%s runs in 24h, including active jobs)."
                         % (wid, b["committed"], b["budget"]))
            continue
        return w, notes
    raise RuntimeError("No worker available. %s Do the work yourself, or retry with force=true if only the budget "
                       "is the problem. See the workers tool." % (" ".join(notes) or "All workers are disabled."))


# ---------------------------------------------------------------- running a worker


def run_worker(w, role, prompt, cwd, model, effort, timeout_min, auto_approve, job_id=None, on_spawn=None):
    spec = {"role": role, "prompt": prompt, "cwd": cwd, "model": model, "effort": effort,
            "timeout_min": timeout_min, "auto_approve": auto_approve}
    inv = w.invocation(spec)
    spec.update({k: v for k, v in inv.items() if k not in ("argv", "env", "input", "cleanup")})
    started = time.time()
    try:
        r = run(w.bin, inv["argv"], cwd=cwd, input=inv.get("input"),
                timeout_ms=int((timeout_min + (1 if w.OWN_TIMEOUT else 0)) * 60_000),
                on_spawn=on_spawn, extra_env=inv.get("env"))
        nf = not_found(r)
        p = w.parse(r, spec) if not nf else {"ok": False, "status": "NOT_FOUND", "response": "", "error": ""}
    finally:
        for f in inv.get("cleanup") or []:
            try:
                shutil.rmtree(f) if os.path.isdir(f) else os.remove(f)
            except OSError:
                pass
    ok = bool(p.get("ok")) and not nf and not r["timed_out"]
    err_text = "\n".join(str(x) for x in [
        p.get("error"),
        "timed out after %s min" % timeout_min if r["timed_out"] else "",
        "" if ok else tail(r["stderr"], 1500),
    ] if x)
    quota = not ok and bool(w.QUOTA_RE.search(err_text) or (not p.get("response") and w.QUOTA_RE.search(r["stdout"] or "")))
    seconds = round(time.time() - started)
    record_usage({
        "worker": w.id,
        "kind": role,
        "job": job_id,
        "model": model or None,
        "ok": ok,
        "quota": quota,
        "turns": p.get("turns") or 0,
        "usage": p.get("usage") or None,
        "cost": p.get("cost") or None,
        "seconds": seconds,
    })
    duration = p.get("duration")
    return {
        "ok": ok,
        "notFound": nf,
        "timedOut": r["timed_out"],
        "quota": quota,
        "status": p.get("status") or ("SUCCESS" if ok else "ERROR"),
        "response": p.get("response") or "",
        "error": err_text,
        "conversationId": p.get("conversation_id"),
        "turns": p.get("turns") or 0,
        "usage": p.get("usage"),
        "cost": p.get("cost"),
        "durationSeconds": duration if duration is not None else seconds,
        "softDenied": w.soft_denied(r),
        "stderrTail": tail(r["stderr"], 3000),
    }


def not_found_msg(w):
    return 'Command "%s" not found for worker %s. %s' % (w.bin, w.id, w.INSTALL_HINT)


# ---------------------------------------------------------------- delegation mode
#
# How much Claude should delegate: off | manual | auto | prefer (rules in modes.json).
# A global default plus per-repo overrides, saved in settings.json. Only "off" is enforced here;
# the others are instructions given to Claude at session start.

with open(os.path.join(HERE, "modes.json"), encoding="utf-8") as _f:
    MODE_RULES = json.load(_f)
MODES = list(MODE_RULES["modes"])


def delegation_mode(repo):
    """Effective mode for a repo: returns (mode, where it comes from)."""
    s = read_json(SETTINGS_FILE, {})
    m = (s.get("repoModes") or {}).get(repo) if repo else None
    if m in MODES:
        return m, "set for this repo"
    if s.get("mode") in MODES:
        return s["mode"], "global setting"
    return MODE_RULES["default"], "default"


def ensure_enabled(repo):
    mode, source = delegation_mode(repo)
    if mode == "off":
        raise RuntimeError("Delegation is off (%s). Do the work without workers; "
                           "the user can change this with /capocantiere:mode." % source)


def mode_line(repo):
    mode, source = delegation_mode(repo)
    return "Delegation mode: %s (%s). %s" % (mode, source, MODE_RULES["modes"][mode]["instruction"])


def set_mode(a):
    mode = (a.get("mode") or "").strip()
    scope = a.get("scope") or "repo"
    if mode not in MODES + ["inherit"]:
        raise RuntimeError("Unknown mode \"%s\". Modes: %s, or inherit." % (mode, ", ".join(MODES)))
    s = read_json(SETTINGS_FILE, {})
    if scope == "global":
        if mode == "inherit":
            s.pop("mode", None)
        else:
            s["mode"] = mode
    elif scope == "repo":
        repo = repo_root(a.get("repo_dir"))
        overrides = s.get("repoModes") or {}
        if mode == "inherit":
            overrides.pop(repo, None)
        else:
            overrides[repo] = mode
        s["repoModes"] = overrides
    else:
        raise RuntimeError('scope must be "repo" or "global"')
    write_json(SETTINGS_FILE, s)


def mode_report(repo):
    s = read_json(SETTINGS_FILE, {})
    mode, source = delegation_mode(repo)
    lines = [
        "Delegation mode%s: %s (%s)" % (" for " + repo if repo else "", mode, source),
        "Global default: %s" % (s.get("mode") or MODE_RULES["default"] + " (built-in)"),
    ]
    overrides = s.get("repoModes") or {}
    if overrides:
        lines.append("Per-repo settings: " + "; ".join("%s → %s" % (r, m) for r, m in sorted(overrides.items())))
    lines += ["", "Modes:"] + ["  %-7s %s" % (m, MODE_RULES["modes"][m]["description"]) for m in MODES]
    lines += ["", "Now: " + MODE_RULES["modes"][mode]["instruction"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- prompts


def implement_prompt(job):
    files, context, acceptance, test_command = job["files"], job["context"], job["acceptance"], job["testCommand"]
    lines = [
        "You are an implementation engineer. A tech lead (another AI agent) has assigned you ONE specific task.",
        "You are working in an isolated git worktree: edit the files directly here. Do not commit, do not create branches, do not push.",
        "Rules:",
        "- Do ONLY what the task asks. No unrequested refactors, no new dependencies unless explicitly requested.",
        "- Follow the style and conventions of the existing code.",
        "- Write code comments and documentation in the language already used in the repository.",
        "- Stay inside this folder: do not read or modify files outside it.",
        "- Before finishing, delete any temporary files or build artifacts you created that are not part of the task (e.g. __pycache__, .pytest_cache, coverage reports).",
        "- If something is ambiguous, choose the most conservative interpretation and flag it in NOTES.",
        "- When you are done, run: %s and fix any errors you introduced." % test_command
        if test_command
        else "- If there is a quick, obvious way to verify your work (tests, typecheck, build), use it.",
        "",
        "## TASK",
        job["task"].strip(),
        "\n## RELEVANT FILES\n" + "\n".join("- " + f for f in files) if files else "",
        "\n## CONTEXT\n" + context.strip() if context else "",
        "\n## ACCEPTANCE CRITERIA\n" + acceptance.strip() if acceptance else "",
        "",
        "## FINAL RESPONSE FORMAT (short, max ~25 lines)",
        "SUMMARY: what you did in 1-3 sentences",
        "FILES: list of files touched",
        "VERIFICATION: commands run and their outcome (or 'not verified')",
        "NOTES: doubts, assumptions, unfinished work (or 'none')",
    ]
    return "\n".join(x for x in lines if x != "")


def analyze_prompt(question, paths, max_words):
    lines = [
        "You are a code analyst in READ-ONLY mode. Do NOT modify, create or delete files. Do not run commands that change state.",
        "Your output will be read by another AI agent with little context to spare: be dense and precise, no preamble.",
        "Always cite files and line numbers (path:line) for every important claim.",
        "Maximum length: about %s words." % max_words,
        "",
        "## QUESTION",
        question.strip(),
        "\n## WHERE TO LOOK\n" + "\n".join("- " + p for p in paths) if paths else "",
    ]
    return "\n".join(x for x in lines if x != "")


# ---------------------------------------------------------------- jobs

jobs = {}  # id -> job
children = {}  # id -> Popen
queue = []
STATE_LOCK = threading.RLock()


def job_file(jid):
    return os.path.join(JOBS_DIR, jid + ".json")


def save_job(job):
    job["updatedAt"] = iso_now()
    write_json(job_file(job["id"]), job)


def load_jobs():
    for f in os.listdir(JOBS_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, f), encoding="utf-8") as fh:
                j = json.load(fh)
            j.setdefault("worker", "agy")
            if j.get("status") in ("running", "queued"):
                # the previous server died while the job was active
                j["status"] = "interrupted"
                j["error"] = (j["error"] + "\n" if j.get("error") else "") + "MCP server restarted while the job was running."
                save_job(j)
            jobs[j["id"]] = j
        except Exception:
            pass


def pump():
    """Start queued jobs while there are free slots, globally and for each job's worker."""
    cfg = W.load_config()
    with STATE_LOCK:
        for jid in list(queue):
            if len(active_jobs(statuses=("running",))) >= cfg["max_parallel"]:
                break
            job = jobs.get(jid)
            if not job or job["status"] != "queued":
                queue.remove(jid)
                continue
            limit = cfg["workers"].get(job["worker"], {}).get("max_parallel", 1)
            if len(active_jobs(job["worker"], ("running",))) >= limit:
                continue  # this worker is full; a job for another worker may still start
            queue.remove(jid)
            job["status"] = "running"
            job["startedAt"] = iso_now()
            save_job(job)
            threading.Thread(target=_execute_wrapper, args=(job,), daemon=True).start()


def _execute_wrapper(job):
    try:
        execute_job(job)
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        save_job(job)
    finally:
        pump()


def create_job(args):
    repo = repo_root(args.get("repo_dir"))
    ensure_enabled(repo)
    w, notes = pick_worker(args.get("worker"), args.get("force"), args.get("fallback"))
    model, effort, model_source = w.resolve_model("implement", args.get("model"), args.get("effort"))
    jid = new_id()
    branch = "capocantiere/" + jid
    worktree = os.path.join(common.WORKTREE_DIR, "%s-%s" % (os.path.basename(repo), jid))

    with GIT_LOCK:
        base = git(repo, "rev-parse", "HEAD").strip()
        base_label = "HEAD"
        if args.get("include_uncommitted") is not False:
            # `git stash create` makes a commit of the uncommitted changes WITHOUT touching the working tree
            s = git(repo, "stash", "create", "capocantiere base " + jid).strip()
            if s:
                base, base_label = s, "HEAD + uncommitted changes"
        git(repo, "worktree", "add", "-q", "-b", branch, worktree, base)

    job = {
        "id": jid,
        "title": args.get("title") or args["task"].strip().split("\n")[0][:80],
        "status": "queued",
        "repo": repo,
        "worktree": worktree,
        "branch": branch,
        "base": base,
        "baseLabel": base_label,
        "task": args["task"],
        "files": args.get("files") or [],
        "context": args.get("context") or "",
        "acceptance": args.get("acceptance") or "",
        "testCommand": args.get("test_command") or "",
        "worker": w.id,
        "requestedWorker": args.get("worker") or None,
        "requestedModel": args.get("model") or None,
        "requestedEffort": args.get("effort") or None,
        "fallback": W.load_config()["fallback"] if args.get("fallback") is None else bool(args.get("fallback")),
        "force": bool(args.get("force")),
        "model": model,
        "modelSource": model_source,
        "effort": effort,
        "timeoutMin": args.get("timeout_minutes") or w.timeout_min("implement"),
        "notes": notes,
        "attempts": [],
        "createdAt": iso_now(),
    }
    with STATE_LOCK:
        jobs[jid] = job
        save_job(job)
        queue.append(jid)
    pump()
    return job


def reset_worktree(job):
    """Throw away a failed attempt's edits before the next worker starts."""
    git(job["worktree"], "reset", "-q", "--hard", "HEAD")
    git(job["worktree"], "clean", "-qfd")


def execute_job(job):
    while True:
        w = W.get_worker(job["worker"])
        res = run_worker(
            w, "implement", implement_prompt(job), job["worktree"], job["model"], job["effort"], job["timeoutMin"],
            auto_approve=True, job_id=job["id"], on_spawn=lambda c: children.__setitem__(job["id"], c),
        )
        children.pop(job["id"], None)
        if job["status"] == "discarded":
            return  # discarded while running
        if res["ok"] or not res["quota"] or not job.get("fallback"):
            break
        # quota / rate limit: move on to the next worker, if any is usable
        try:
            nxt, notes = pick_worker(None, job.get("force"), True, exclude=[a["worker"] for a in job["attempts"]] + [w.id])
        except RuntimeError:
            break
        job["attempts"].append({"worker": w.id, "model": job["model"], "status": res["status"],
                                "quota": True, "error": truncate(res["error"], 1000), "at": iso_now()})
        job["notes"] = (job.get("notes") or []) + notes + [
            "%s hit a quota/rate limit: relaunched on %s." % (w.id, nxt.id)]
        reset_worktree(job)
        job["worker"] = nxt.id
        job["model"], job["effort"], job["modelSource"] = nxt.resolve_model(
            "implement", None, job.get("requestedEffort"))  # a model name rarely means the same on another CLI
        job["timeoutMin"] = nxt.timeout_min("implement")
        save_job(job)

    job.update({
        "finishedAt": iso_now(),
        "workerStatus": res["status"],
        "response": res["response"],
        "conversationId": res["conversationId"],
        "turns": res["turns"],
        "usage": res["usage"],
        "cost": res["cost"],
        "durationSeconds": res["durationSeconds"],
        "softDenied": res["softDenied"],
        "stderrTail": res["stderrTail"],
    })

    # collect changes anyway, even if the worker failed midway
    collect_changes(job)

    if res["notFound"]:
        job["status"] = "failed"
        job["error"] = not_found_msg(w)
    elif not res["ok"]:
        job["status"] = "failed"
        job["error"] = res["error"] or "%s exited with status %s" % (w.id, res["status"])
        if res["quota"]:
            job["error"] = "QUOTA EXHAUSTED / rate limit on %s%s.\n%s" % (
                w.id, "" if job.get("fallback") else " (fallback off)", job["error"])
        job["error"] += w.failure_note(job["model"], res["error"])
    else:
        job["status"] = "done" if job.get("filesChanged", 0) > 0 else "no_changes"
    save_job(job)
    # nothing to review: free the worktree and branch right away
    if not job.get("filesChanged"):
        try:
            remove_worktree(job)
        except Exception:
            pass


# Build artifacts that never belong in a job's diff, even if the repo has no .gitignore for them.
JUNK_PATHSPECS = [":(exclude,glob)" + p for p in ['**/__pycache__/**', '**/*.py[co]', '**/.pytest_cache/**', '**/.mypy_cache/**', '**/.ruff_cache/**', '**/node_modules/**', '**/.DS_Store']]


def collect_changes(job):
    if not os.path.exists(job["worktree"]):
        return
    git(job["worktree"], "add", "-A", "--", ".", *JUNK_PATHSPECS)
    staged = run("git", ["diff", "--cached", "--quiet"], cwd=job["worktree"])
    if staged["code"] == 1:
        git(
            job["worktree"],
            "-c", "user.name=%s (capocantiere)" % job.get("worker", "worker"),
            "-c", "user.email=capocantiere@localhost",
            "commit", "-q", "--no-verify", "-m", "%s: %s" % (job.get("worker", "worker"), job["title"]),
            "-m", "job " + job["id"],
        )
    stat = git(job["repo"], "diff", "--stat", job["base"], job["branch"])
    names = git(job["repo"], "diff", "--name-status", job["base"], job["branch"]).strip()
    # keep the patch as raw bytes so non-UTF-8 files survive intact
    p = run("git", ["diff", "--binary", job["base"], job["branch"]], cwd=job["repo"], binary=True)
    if p["code"] != 0:
        raise RuntimeError("git diff --binary failed: " + p["stderr"].strip())
    patch_file = os.path.join(PATCH_DIR, job["id"] + ".patch")
    with open(patch_file, "wb") as f:
        f.write(p["stdout"])
    job["diffStat"] = stat.strip()
    job["changedFiles"] = names.split("\n") if names else []
    job["filesChanged"] = len(job["changedFiles"])
    job["patchFile"] = patch_file
    job["patchBytes"] = len(p["stdout"])


def remove_worktree(job):
    with GIT_LOCK:
        if os.path.exists(job["worktree"]):
            run("git", ["worktree", "remove", "--force", job["worktree"]], cwd=job["repo"])
        run("git", ["worktree", "prune"], cwd=job["repo"])
        run("git", ["branch", "-D", job["branch"]], cwd=job["repo"])


def job_summary(j):
    return {
        "id": j["id"],
        "title": j["title"],
        "worker": j.get("worker"),
        "status": j["status"],
        "files_changed": j.get("filesChanged"),
        "duration_s": j.get("durationSeconds"),
        "created": j["createdAt"],
    }


def job_detail(j, include_diff=True, max_diff_chars=15000):
    ws = j.get("workerStatus") or j.get("agyStatus")
    lines = [
        "JOB %s — %s" % (j["id"], j["title"]),
        "status: %s%s" % (j["status"], " (%s: %s)" % (j.get("worker"), ws) if ws else ""),
        "worker: %s" % j.get("worker"),
        "branch: %s   worktree: %s" % (j["branch"], j["worktree"]),
        "base: %s (%s)" % (j.get("baseLabel"), (j.get("base") or "")[:10]),
        "model: %s%s%s" % (j.get("model") or "CLI default",
                           " (%s)" % j["modelSource"] if j.get("modelSource") not in (None, "", "CLI default") else "",
                           ", effort " + j["effort"] if j.get("effort") else ""),
    ]
    if j.get("durationSeconds") is not None:
        turns = j.get("turns")
        extra = ", cost %s" % j["cost"] if j.get("cost") else ""
        lines.append("duration: %ss, turns: %s%s" % (round(j["durationSeconds"]), "?" if turns is None else turns, extra))
    for a in j.get("attempts") or []:
        lines.append("earlier attempt: %s failed%s — %s" % (a["worker"], " (quota)" if a.get("quota") else "",
                                                           (a.get("error") or "").split("\n")[0][:200]))
    if j.get("notes"):
        lines += ["", "NOTES:"] + ["- " + n for n in j["notes"]]
    if j.get("error"):
        lines += ["", "ERROR:", truncate(j["error"], 2500)]
    if j.get("softDenied"):
        lines += ["", "ACTIONS DENIED TO THE WORKER (soft-deny):"] + j["softDenied"]
    if j.get("response"):
        lines += ["", "WORKER'S RESPONSE:", truncate(j["response"], 4000)]
    if j.get("diffStat"):
        lines += ["", "DIFF STAT:", j["diffStat"]]
    if include_diff and j.get("patchBytes") and j.get("patchFile") and os.path.exists(j["patchFile"]):
        with open(j["patchFile"], encoding="utf-8", errors="replace") as f:
            patch = f.read()
        lines += ["", "DIFF (full patch: %s):" % j["patchFile"], truncate(patch, max_diff_chars)]
    if j["status"] == "done":
        lines += ["", "Next step: review the diff, then apply or discard (or a new delegate with the fixes)."]
    return "\n".join(lines)


def wait_for(ids, seconds):
    deadline = time.time() + seconds

    def all_done():
        return all((jobs.get(i) or {}).get("status") in TERMINAL for i in ids)

    while time.time() < deadline:
        if all_done():
            return True
        time.sleep(1)
    return all_done()


# ---------------------------------------------------------------- analyze


def analyze(a):
    repo = repo_root(a.get("repo_dir"))
    ensure_enabled(repo)
    prompt = analyze_prompt(a.get("question") or "", a.get("paths"), a.get("max_words") or 600)

    def snapshot():
        s = run("git", ["status", "--porcelain=v1", "-uall"], cwd=repo)
        d = run("git", ["diff"], cwd=repo)
        return hashlib.sha1((s["stdout"] + d["stdout"]).encode("utf-8")).hexdigest()

    fallback = W.load_config()["fallback"] if a.get("fallback") is None else bool(a.get("fallback"))
    w, notes = pick_worker(a.get("worker"), a.get("force"), fallback)
    requested_model = a.get("model")
    tried = []
    while True:
        model, effort, model_source = w.resolve_model("analyze", requested_model, a.get("effort"))
        before = snapshot()
        res = run_worker(w, "analyze", prompt, repo, model, effort,
                         a.get("timeout_minutes") or w.timeout_min("analyze"),
                         auto_approve=False)  # in the main repo: no auto-approval
        after = snapshot()
        tried.append(w.id)
        if res["notFound"]:
            raise RuntimeError(not_found_msg(w))
        if res["ok"] or not res["quota"] or not fallback:
            break
        try:
            nxt, more = pick_worker(None, a.get("force"), True, exclude=tried)
        except RuntimeError:
            break
        notes += more + ["%s hit a quota/rate limit: asked %s instead." % (w.id, nxt.id)]
        w, requested_model = nxt, None
    out = ["NOTES: " + " ".join(notes)] if notes else []
    if not res["ok"]:
        out.append("ERROR from %s (%s)%s:\n%s%s" % (w.id, res["status"], " — QUOTA/RATE LIMIT" if res["quota"] else "",
                                                   truncate(res["error"], 2000), w.failure_note(model, res["error"])))
    if before != after:
        out.append("⚠️ WARNING: the repo state changed during the analysis. Check `git status` / `git diff`.")
    out.append(truncate(res["response"] or "(no response)", 16000))
    total = (res["usage"] or {}).get("total_tokens")
    out.append("\n[analyze: %s, %s (%s), %ss, turns %s, tokens %s]" % (
        w.id, model or "CLI default", model_source, round(res["durationSeconds"]), res["turns"],
        "?" if total is None else total))
    return {"text": "\n\n".join(out), "isError": not res["ok"] and not res["response"]}


# ---------------------------------------------------------------- workers report


def workers_report():
    cfg = W.load_config()
    usage = usage_by_worker(24)
    lines = ["Workers, in preference order (config: %s%s):" % (
        CONFIG_FILE, "" if os.path.exists(CONFIG_FILE) else ", not created yet: built-in defaults")]
    for w in W.all_workers():
        installed, version = w.version()
        b = budget_state(w, usage)
        u = b["usage"]
        auth, auth_note = w.auth_status() if installed else (None, "")
        state = "enabled" if w.enabled else "DISABLED"
        if w.id == SESSION["default"]:
            state += ", session default"
        lines += ["", "%s — %s [%s]" % (w.id, w.label, state)]
        lines.append("  installed: %s" % (version if installed else "NO (%s not found). %s" % (w.bin, w.INSTALL_HINT)))
        if installed:
            lines.append("  authenticated: %s" % ({True: "yes", False: "NO", None: "unknown"}[auth]
                                                  + (" (%s)" % auth_note if auth_note else "")))
        lines.append("  budget 24h: %d/%s used, %d left%s%s" % (
            b["committed"], b["budget"], b["left"],
            ", %d failed, %d quota errors" % (u["failures"], u["quota_errors"]) if u["failures"] else "",
            ", cost %.4g" % u["cost"] if u["cost"] else ""))
        lines.append("  jobs: %d running, %d queued (max %s)" % (
            len(active_jobs(w.id, ("running",))), len(active_jobs(w.id, ("queued",))), w.s["max_parallel"]))
        lines.append("  models: " + (w.models_line() if installed and w.enabled else "%s (configured)" % (w.s.get("model") or "CLI default")))
        lines.append("  strengths: %s" % (", ".join(w.s.get("strengths") or []) or "—"))
        for warning in w.warnings():
            lines.append("  ⚠️ " + warning)
    lines += ["", "Fallback on quota/budget: %s. Max parallel jobs overall: %s." % (
        "on" if cfg["fallback"] else "off", cfg["max_parallel"])]
    lines.append("Session default: %s." % (SESSION["default"] or "none (preference order)"))
    lines.append(mode_line(current_repo()))
    for e in cfg["errors"]:
        lines.append("CONFIG ERROR: " + e)
    lines.append("State: " + common.STATE_DIR)
    return "\n".join(lines)


def set_session_default(value):
    v = (value or "").strip()
    if v in ("", "auto", "none"):
        SESSION["default"] = None
        return "Session default cleared: workers are chosen in the configured order."
    w = W.get_worker(v)
    if not w.enabled:
        raise RuntimeError("%s is disabled in %s; enable it there first." % (v, CONFIG_FILE))
    installed, _ = w.version()
    SESSION["default"] = v
    return "Session default worker: %s%s." % (v, "" if installed else " (WARNING: %s not found)" % w.bin)


# ---------------------------------------------------------------- tools

# Tool definitions are shared with server.mjs.
with open(os.path.join(HERE, "tools.json"), encoding="utf-8") as _f:
    TOOLS = json.load(_f)


def _get_job(a):
    j = jobs.get(a.get("job_id"))
    if not j:
        raise RuntimeError("job %s not found" % a.get("job_id"))
    return j


def call_tool(name, a):
    if name == "delegate":
        if not (a.get("task") or "").strip():
            raise RuntimeError("task is required")
        job = create_job(a)
        wait = min(a.get("wait_seconds") or 0, 600)
        if wait > 0:
            wait_for([job["id"]], wait)
            if job["status"] in TERMINAL:
                return job_detail(job)
        w = W.get_worker(job["worker"])
        b = budget_state(w)
        return "\n".join(([("NOTES: " + " ".join(job["notes"]))] if job["notes"] else []) + [
            "Job %s started on %s (%s). Branch %s, base: %s." % (job["id"], job["worker"], job["status"], job["branch"], job["baseLabel"]),
            "Model: %s (%s)." % (job["model"] or "CLI default", job["modelSource"]),
            "Active jobs: %d running, %d queued." % (len(active_jobs(statuses=("running",))), len(queue)),
            "%s 24h usage (including active jobs): %d/%s." % (w.id, b["committed"], b["budget"]),
            'Use jobs with job_id="%s" and wait_seconds to wait for the result.' % job["id"],
        ])

    if name == "analyze":
        return analyze(a)

    if name == "jobs":
        if a.get("job_id"):
            j = _get_job(a)
            if a.get("wait_seconds"):
                wait_for([j["id"]], min(a["wait_seconds"], 600))
            return job_detail(j, include_diff=a.get("include_diff") is not False, max_diff_chars=a.get("max_diff_chars") or 15000)
        repo = None
        if not a.get("all_repos"):
            try:
                repo = repo_root(a.get("repo_dir"))
            except Exception:
                repo = None

        def lst():
            return [j for j in list(jobs.values()) if not repo or j["repo"] == repo]

        if a.get("wait_seconds"):
            active = [j["id"] for j in lst() if j["status"] not in TERMINAL]
            if active:
                wait_for(active, min(a["wait_seconds"], 600))
        visible = sorted((j for j in lst() if j["status"] not in ("applied", "discarded")), key=lambda j: j["createdAt"])
        if not visible:
            return "No open jobs."
        return "\n".join(json.dumps(job_summary(j), ensure_ascii=False, separators=(",", ":")) for j in visible)

    if name == "apply":
        j = _get_job(a)
        if j["status"] in ("running", "queued"):
            raise RuntimeError("The job is still running.")
        if j["status"] == "applied":
            return "Already applied."
        if j["status"] == "interrupted" and os.path.exists(j["worktree"]):
            collect_changes(j)
        if not j.get("patchFile") or not os.path.exists(j["patchFile"]) or not j.get("patchBytes"):
            return "No changes to apply."
        r = run("git", ["apply", "--whitespace=nowarn", j["patchFile"]], cwd=j["repo"])
        how = "clean"
        if r["code"] != 0:
            r = run("git", ["apply", "--3way", "--whitespace=nowarn", j["patchFile"]], cwd=j["repo"])
            how = "3-way merge"
            if r["code"] != 0:
                return {
                    "isError": True,
                    "text": "Apply failed (even with 3-way). The repo has changed too much since the job's base.\n%s\n"
                    "Options: resolve conflicts by hand (if there are markers), check the state with `git diff`, "
                    "or relaunch the task." % truncate(r["stderr"], 3000),
                }
        j["status"] = "applied"
        j["appliedAt"] = iso_now()
        save_job(j)
        if not a.get("keep_worktree"):
            remove_worktree(j)
        warn = r["stderr"].strip()
        return "Applied (%s) to the working tree of %s. Changes are NOT committed.\n%s%s\nNow: run tests/typecheck and polish if needed." % (
            how, j["repo"], warn + "\n" if warn else "", j.get("diffStat") or "")

    if name == "discard":
        j = _get_job(a)
        was_running = j["status"] == "running"
        j["status"] = "discarded"
        save_job(j)
        c = children.get(j["id"])
        if c:
            kill_tree(c, signal.SIGTERM)
            t = threading.Timer(3, lambda: kill_tree(c, SIGKILL))
            t.daemon = True
            t.start()
        with STATE_LOCK:
            if j["id"] in queue:
                queue.remove(j["id"])
        pump()  # a slot may have freed up: start the next queued job
        if was_running:
            time.sleep(1.5)
        remove_worktree(j)
        return "Job %s discarded." % j["id"]

    if name == "workers":
        head = set_session_default(a["use"]) + "\n\n" if "use" in a else ""
        return head + workers_report()

    if name == "mode":
        if a.get("mode"):
            set_mode(a)
        try:
            repo = repo_root(a.get("repo_dir"))
        except Exception:
            repo = None
        return mode_report(repo)

    if name == "models":
        wid = a.get("worker") or "agy"
        w = W.get_worker(wid)
        if not isinstance(w, W.AgyWorker):
            return ("%s does not publish a model list. Model in use: %s. Set \"model\" / \"analyze_model\" for it in %s, "
                    "or pass model to delegate/analyze." % (wid, w.models_line(), CONFIG_FILE))
        if any(k in a for k in ("analyze", "implement", "exclude")):
            w.update_model_settings(a)
        return w.models_report(bool(a.get("refresh")))

    raise RuntimeError("unknown tool: %s" % name)


# ---------------------------------------------------------------- session summary (for the SessionStart hook)


def session_summary():
    repo = current_repo()
    tag = "[capocantiere]"
    if delegation_mode(repo)[0] == "off":
        return "%s %s" % (tag, mode_line(repo))
    usage = usage_by_worker(24)
    ready, other = [], []
    for w in W.all_workers():
        if not w.enabled:
            continue
        installed, _ = w.version()
        if not installed:
            other.append("%s not installed" % w.id)
            continue
        if w.blocker():
            other.append("%s unusable: %s" % (w.id, w.blocker()))
            continue
        b = budget_state(w, usage)
        u = b["usage"]
        ready.append("%s (%d/%s runs left%s; good at %s)" % (
            w.id, b["left"], b["budget"], ", %d recent quota errors" % u["quota_errors"] if u["quota_errors"] else "",
            ", ".join(w.s.get("strengths") or []) or "anything"))
        other += ["%s: %s" % (w.id, x) for x in w.warnings()]
    lines = []
    if ready:
        lines.append("%s Workers available for delegation (MCP tools delegate/analyze/jobs/apply/discard/workers), "
                     "in preference order: %s." % (tag, "; ".join(ready)))
    else:
        lines.append("%s No worker is available in this session (none enabled and installed): work without delegating. "
                     "See the workers tool." % tag)
    lines += ["%s %s" % (tag, o) for o in other]
    lines.append("%s %s" % (tag, mode_line(repo)))
    if repo:
        pending = [
            j for j in jobs.values()
            if j["repo"] == repo and (j["status"] == "interrupted" or (j["status"] in ("done", "failed") and (j.get("filesChanged") or 0) > 0))
        ]
        if pending:
            lines.append(
                "%s Jobs from previous sessions still open in this repo: %s. "
                "Review them with jobs, then apply or discard."
                % (tag, "; ".join("%s (%s on %s: %s)" % (j["id"], j["status"], j.get("worker"), j["title"]) for j in pending))
            )
    return "\n".join(lines)


# ---------------------------------------------------------------- MCP stdio (JSON-RPC 2.0, newline-delimited)

SEND_LOCK = threading.Lock()


def send(msg):
    data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    with SEND_LOCK:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def handle(msg):
    if not isinstance(msg, dict):
        return
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    is_request = mid is not None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "capocantiere", "version": VERSION},
                "instructions": "Tools for delegating work to external coding-agent CLIs (workers: GitHub Copilot CLI, "
                "Mistral Vibe, ...). You remain the manager: you plan, delegate well-specified tasks, review the diffs, "
                "and apply or discard them.",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            out = call_tool(params.get("name"), params.get("arguments") or {})
            o = {"text": out} if isinstance(out, str) else out
            result = {"content": [{"type": "text", "text": o["text"]}], "isError": bool(o.get("isError"))}
        else:
            if not is_request:
                return  # notifications (e.g. notifications/initialized)
            return send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not supported: %s" % method}})
        if is_request:
            send({"jsonrpc": "2.0", "id": mid, "result": result})
    except Exception as e:
        if not is_request:
            return
        if method == "tools/call":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "Error: %s" % e}], "isError": True}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})


def main():
    common.ensure_dirs()
    if "--example-config" in sys.argv:
        sys.stdout.write(json.dumps(W.example_config(), indent=2) + "\n")
        return
    load_jobs()
    if "--session-summary" in sys.argv:
        try:
            s = session_summary()
            if s:
                sys.stdout.write(s + "\n")
        except Exception:
            pass
        return
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        # each message runs on its own thread, so long tool calls (waits, analyses) don't block the others
        threading.Thread(target=handle, args=(msg,), daemon=True).start()
    for c in list(children.values()):
        kill_tree(c, signal.SIGTERM)
    time.sleep(0.2)
    os._exit(0)


if __name__ == "__main__":
    main()
