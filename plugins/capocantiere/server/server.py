#!/usr/bin/env python3
# gemini-delegate: MCP server that lets Claude Code (the "manager") delegate
# work to Antigravity CLI (`agy`, Gemini) using a Google AI Pro subscription.
#
# Python port of server.mjs, with the same behavior and the same state files,
# so you can switch between the two runtimes without losing jobs.
#
# - Every code-writing task runs in an isolated git worktree, on a dedicated branch.
#   The main repo is never touched until Claude calls gemini_apply.
# - Jobs are asynchronous and can run in parallel (with a queue).
# - Usage/quota is tracked so the subscription's daily limits aren't exceeded.
#
# Zero dependencies: only Python >= 3.8 (standard library) and git are required.

import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------- config

env = os.environ


def num(v, d):
    if v is None or v == "":
        return d
    try:
        f = float(v)
    except ValueError:
        return d
    return int(f) if f.is_integer() else f


def boolean(v, d):
    if v is None or v == "":
        return d
    return re.match(r"^(1|true|yes|on)$", v, re.I) is not None


def _xdg(var, *default):
    return env.get(var) or os.path.join(os.path.expanduser("~"), *default)


CONFIG = {
    "agyBin": env.get("GEMINI_DELEGATE_AGY_BIN") or "agy",
    "model": env.get("GEMINI_DELEGATE_MODEL") or "",  # empty = agy's default
    "analyzeModel": env.get("GEMINI_DELEGATE_ANALYZE_MODEL") or "",
    "effort": env.get("GEMINI_DELEGATE_EFFORT") or "",
    "dailyBudget": num(env.get("GEMINI_DELEGATE_DAILY_BUDGET"), 150),  # agy runs per 24h
    "maxParallel": num(env.get("GEMINI_DELEGATE_MAX_PARALLEL"), 3),
    "taskTimeoutMin": num(env.get("GEMINI_DELEGATE_TASK_TIMEOUT_MIN"), 20),
    "analyzeTimeoutMin": num(env.get("GEMINI_DELEGATE_ANALYZE_TIMEOUT_MIN"), 8),
    "sandbox": boolean(env.get("GEMINI_DELEGATE_SANDBOX"), True),
    # Inside the isolated worktree agy may edit files and run commands without asking:
    # it's the only way to work headless. The main repo stays protected.
    "skipPermissionsInWorktree": boolean(env.get("GEMINI_DELEGATE_SKIP_PERMISSIONS"), True),
    "extraArgs": [x for x in (env.get("GEMINI_DELEGATE_EXTRA_ARGS") or "").split(" ") if x],
    "stateDir": env.get("GEMINI_DELEGATE_STATE_DIR")
    or os.path.join(_xdg("XDG_STATE_HOME", ".local", "state"), "gemini-delegate"),
    "worktreeDir": env.get("GEMINI_DELEGATE_WORKTREE_DIR")
    or os.path.join(_xdg("XDG_CACHE_HOME", ".cache"), "gemini-delegate", "worktrees"),
}

JOBS_DIR = os.path.join(CONFIG["stateDir"], "jobs")
PATCH_DIR = os.path.join(CONFIG["stateDir"], "patches")
USAGE_FILE = os.path.join(CONFIG["stateDir"], "usage.jsonl")
for _d in (JOBS_DIR, PATCH_DIR, CONFIG["worktreeDir"]):
    os.makedirs(_d, exist_ok=True)

VERSION = "1.0.0"
TERMINAL = {"done", "no_changes", "failed", "applied", "discarded", "interrupted"}
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- helpers


def now_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def js(v):
    """Format a value the way the Node version prints it in text output."""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def kill_tree(child, sig):
    """Signal the child and everything it started (its own process group on POSIX)."""
    try:
        if os.name == "posix":
            os.killpg(child.pid, sig)
        else:
            child.send_signal(sig)
    except OSError:
        pass


def run(cmd, args, cwd=None, input=None, timeout_ms=None, on_spawn=None, binary=False):
    """Run a command; never raises. Returns code/stdout/stderr/error/timed_out."""
    try:
        child = subprocess.Popen(
            [cmd] + list(args),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except OSError as e:
        return {"code": -1, "stdout": b"" if binary else "", "stderr": str(e), "error": e, "timed_out": False}
    if on_spawn:
        on_spawn(child)
    data = input.encode("utf-8") if isinstance(input, str) else input
    timed_out = False
    try:
        out, err = child.communicate(data, timeout=timeout_ms / 1000 if timeout_ms else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(child, signal.SIGTERM)
        try:
            out, err = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            kill_tree(child, SIGKILL)
            out, err = child.communicate()
    code = child.returncode if child.returncode is not None and child.returncode >= 0 else -1
    return {
        "code": code,
        "stdout": out if binary else out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
        "error": None,
        "timed_out": timed_out,
    }


def not_found(r):
    return isinstance(r.get("error"), FileNotFoundError)


def git(cwd, *args):
    r = run("git", args, cwd=cwd)
    if r["code"] != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r["stderr"].strip() or r["stdout"].strip()))
    return r["stdout"]


# serialize git operations that touch worktrees/branches
GIT_LOCK = threading.Lock()


def truncate(s, n):
    if not s:
        return ""
    if len(s) <= n:
        return s
    return s[:n] + "\n… [truncated: %d characters omitted]" % (len(s) - n)


def tail(s, n):
    if not s:
        return ""
    return s if len(s) <= n else "…" + s[-n:]


def new_id():
    return datetime.now().strftime("g%m%d-%H%M-") + secrets.token_hex(2)


def repo_root(d=None):
    start = d or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    r = run("git", ["rev-parse", "--show-toplevel"], cwd=start)
    if r["code"] != 0:
        raise RuntimeError('"%s" is not a git repository. gemini-delegate requires git.' % start)
    return r["stdout"].strip()


# ---------------------------------------------------------------- usage / quota

USAGE_LOCK = threading.Lock()


def record_usage(entry):
    line = json.dumps(dict({"ts": now_ms()}, **{k: v for k, v in entry.items() if v is not None or k == "usage"}),
                      ensure_ascii=False)
    with USAGE_LOCK, open(USAGE_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def usage_window(hours=24):
    since = now_ms() - hours * 3600_000
    out = {"runs": 0, "turns": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
           "failures": 0, "quota_errors": 0}
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
            out["runs"] += 1
            out["turns"] += e.get("turns") or 0
            out["input_tokens"] += u.get("input_tokens") or 0
            out["output_tokens"] += u.get("output_tokens") or 0
            out["total_tokens"] += u.get("total_tokens") or 0
            if not e.get("ok"):
                out["failures"] += 1
            if e.get("quota"):
                out["quota_errors"] += 1
    return out


def active_job_count():
    return sum(1 for j in list(jobs.values()) if j["status"] in ("running", "queued"))


def budget_check(force):
    u = usage_window(24)
    u["committed"] = u["runs"] + active_job_count()  # includes jobs still running
    if not force and u["committed"] >= CONFIG["dailyBudget"]:
        raise RuntimeError(
            "Daily budget reached (%d/%s agy runs in the last 24h, including running jobs). "
            "Do the work directly in Claude, or retry with force=true." % (u["committed"], CONFIG["dailyBudget"])
        )
    return u


QUOTA_RE = re.compile(r"(429|RESOURCE_EXHAUSTED|rate ?limit|quota)", re.I)

# ---------------------------------------------------------------- agy


def agy_args(prompt, model, effort, timeout_min, skip_permissions):
    a = ["-p", prompt, "--output-format", "json"]
    if model:
        a += ["--model", model]
    if effort:
        a += ["--effort", effort]
    if timeout_min:
        a += ["--print-timeout", "%sm" % timeout_min]
    if skip_permissions:
        a.append("--dangerously-skip-permissions")
    if CONFIG["sandbox"]:
        a.append("--sandbox")
    return a + CONFIG["extraArgs"]


def parse_agy_output(stdout):
    for l in reversed(stdout.strip().split("\n")):
        t = l.strip()
        if not t.startswith("{"):
            continue
        try:
            v = json.loads(t)
            if isinstance(v, dict):
                return v
        except ValueError:
            pass
    try:
        v = json.loads(stdout)
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


def run_agy(cwd, prompt, model, effort, timeout_min, skip_permissions, kind, job_id=None, on_spawn=None):
    args = agy_args(prompt, model, effort, timeout_min, skip_permissions)
    started = time.time()
    r = run(CONFIG["agyBin"], args, cwd=cwd, timeout_ms=(timeout_min + 1) * 60_000, on_spawn=on_spawn)
    envl = parse_agy_output(r["stdout"]) or {}
    has_envl = bool(envl)
    nf = not_found(r)
    status = envl.get("status") or ("SUCCESS" if r["code"] == 0 else "ERROR")
    ok = not nf and r["code"] == 0 and status == "SUCCESS"
    err = envl.get("error")
    err_text = "\n".join(
        str(x)
        for x in [err, "timed out after %s min" % timeout_min if r["timed_out"] else "", "" if ok else tail(r["stderr"], 1500)]
        if x
    )
    quota = bool(QUOTA_RE.search(err_text) or QUOTA_RE.search(r["stderr"] or ""))
    seconds = round(time.time() - started)
    record_usage({
        "kind": kind,
        "job": job_id,
        "ok": ok,
        "quota": quota,
        "turns": envl.get("num_turns") or 0,
        "usage": envl.get("usage") or None,
        "seconds": seconds,
    })
    # "soft-deny" notices that agy writes to stderr even when it exits with 0
    soft_denied = [
        l for l in (r["stderr"] or "").split("\n")
        if re.search(r"denied|not approved|requires approval|soft-den", l, re.I)
    ][:10]
    response = envl.get("response")
    if response is None:
        response = "" if has_envl else r["stdout"].strip()
    duration = envl.get("duration_seconds")
    return {
        "ok": ok,
        "notFound": nf,
        "quota": quota,
        "status": status,
        "response": response,
        "error": err_text,
        "conversationId": envl.get("conversation_id") or None,
        "turns": envl.get("num_turns") or 0,
        "usage": envl.get("usage") or None,
        "durationSeconds": duration if duration is not None else seconds,
        "softDenied": soft_denied,
        "stderrTail": tail(r["stderr"], 3000),
    }


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
    tmp = job_file(job["id"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, ensure_ascii=False)
    os.replace(tmp, job_file(job["id"]))


def load_jobs():
    for f in os.listdir(JOBS_DIR):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(JOBS_DIR, f), encoding="utf-8") as fh:
                j = json.load(fh)
            if j.get("status") in ("running", "queued"):
                # the previous server died while the job was active
                j["status"] = "interrupted"
                j["error"] = (j["error"] + "\n" if j.get("error") else "") + "MCP server restarted while the job was running."
                save_job(j)
            jobs[j["id"]] = j
        except Exception:
            pass


def running_count():
    return sum(1 for j in list(jobs.values()) if j["status"] == "running")


def pump():
    with STATE_LOCK:
        while queue and running_count() < CONFIG["maxParallel"]:
            job = jobs.get(queue.pop(0))
            if not job or job["status"] != "queued":
                continue
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
    usage = budget_check(args.get("force"))
    jid = new_id()
    branch = "gemini/" + jid
    worktree = os.path.join(CONFIG["worktreeDir"], "%s-%s" % (os.path.basename(repo), jid))

    with GIT_LOCK:
        base = git(repo, "rev-parse", "HEAD").strip()
        base_label = "HEAD"
        if args.get("include_uncommitted") is not False:
            # `git stash create` makes a commit of the uncommitted changes WITHOUT touching the working tree
            s = git(repo, "stash", "create", "gemini-delegate base " + jid).strip()
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
        "model": args.get("model") or CONFIG["model"],
        "effort": args.get("effort") or CONFIG["effort"],
        "timeoutMin": args.get("timeout_minutes") or CONFIG["taskTimeoutMin"],
        "createdAt": iso_now(),
    }
    with STATE_LOCK:
        jobs[jid] = job
        save_job(job)
        queue.append(jid)
    pump()
    return job, usage


def execute_job(job):
    res = run_agy(
        cwd=job["worktree"],
        prompt=implement_prompt(job),
        model=job["model"],
        effort=job["effort"],
        timeout_min=job["timeoutMin"],
        skip_permissions=CONFIG["skipPermissionsInWorktree"],
        kind="implement",
        job_id=job["id"],
        on_spawn=lambda c: children.__setitem__(job["id"], c),
    )
    children.pop(job["id"], None)
    if job["status"] == "discarded":
        return  # discarded while running

    job.update({
        "finishedAt": iso_now(),
        "agyStatus": res["status"],
        "response": res["response"],
        "conversationId": res["conversationId"],
        "turns": res["turns"],
        "usage": res["usage"],
        "durationSeconds": res["durationSeconds"],
        "softDenied": res["softDenied"],
        "stderrTail": res["stderrTail"],
    })

    # collect changes anyway, even if agy failed midway
    collect_changes(job)

    if res["notFound"]:
        job["status"] = "failed"
        job["error"] = 'Command "%s" not found. Install Antigravity CLI and run `agy` once to log in.' % CONFIG["agyBin"]
    elif not res["ok"]:
        job["status"] = "failed"
        job["error"] = res["error"] or "agy exited with status %s" % res["status"]
        if res["quota"]:
            job["error"] = "QUOTA EXHAUSTED / Gemini rate limit.\n" + job["error"]
    else:
        job["status"] = "done" if job.get("filesChanged", 0) > 0 else "no_changes"
    save_job(job)
    # nothing to review: free the worktree and branch right away
    if not job.get("filesChanged"):
        try:
            remove_worktree(job)
        except Exception:
            pass


def collect_changes(job):
    if not os.path.exists(job["worktree"]):
        return
    git(job["worktree"], "add", "-A")
    staged = run("git", ["diff", "--cached", "--quiet"], cwd=job["worktree"])
    if staged["code"] == 1:
        git(
            job["worktree"],
            "-c", "user.name=Gemini (agy)",
            "-c", "user.email=gemini-delegate@localhost",
            "commit", "-q", "--no-verify", "-m", "gemini: " + job["title"], "-m", "job " + job["id"],
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
        "status": j["status"],
        "files_changed": j.get("filesChanged"),
        "duration_s": j.get("durationSeconds"),
        "created": j["createdAt"],
    }


def job_detail(j, include_diff=True, max_diff_chars=15000):
    lines = [
        "JOB %s — %s" % (j["id"], j["title"]),
        "status: %s%s" % (j["status"], " (agy: %s)" % j["agyStatus"] if j.get("agyStatus") else ""),
        "branch: %s   worktree: %s" % (j["branch"], j["worktree"]),
        "base: %s (%s)" % (j.get("baseLabel"), (j.get("base") or "")[:10]),
    ]
    if j.get("durationSeconds") is not None:
        turns = j.get("turns")
        lines.append("duration: %ss, turns: %s" % (j["durationSeconds"], "?" if turns is None else turns))
    if j.get("error"):
        lines += ["", "ERROR:", truncate(j["error"], 2500)]
    if j.get("softDenied"):
        lines += ["", "ACTIONS DENIED TO agy (soft-deny):"] + j["softDenied"]
    if j.get("response"):
        lines += ["", "GEMINI'S RESPONSE:", truncate(j["response"], 4000)]
    if j.get("diffStat"):
        lines += ["", "DIFF STAT:", j["diffStat"]]
    if include_diff and j.get("patchBytes") and j.get("patchFile") and os.path.exists(j["patchFile"]):
        with open(j["patchFile"], encoding="utf-8", errors="replace") as f:
            patch = f.read()
        lines += ["", "DIFF (full patch: %s):" % j["patchFile"], truncate(patch, max_diff_chars)]
    if j["status"] == "done":
        lines += ["", "Next step: review the diff, then gemini_apply or gemini_discard (or a new gemini_delegate with the fixes)."]
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
    if name == "gemini_delegate":
        if not (a.get("task") or "").strip():
            raise RuntimeError("task is required")
        job, usage = create_job(a)
        wait = min(a.get("wait_seconds") or 0, 600)
        if wait > 0:
            wait_for([job["id"]], wait)
            if job["status"] in TERMINAL:
                return job_detail(job)
        return "\n".join([
            "Job %s started (%s). Branch %s, base: %s." % (job["id"], job["status"], job["branch"], job["baseLabel"]),
            "Active jobs: %d/%s running, %d queued." % (running_count(), CONFIG["maxParallel"], len(queue)),
            "24h usage (including running jobs): %d/%s." % (usage["committed"] + 1, CONFIG["dailyBudget"]),
            'Use gemini_jobs with job_id="%s" and wait_seconds to wait for the result.' % job["id"],
        ])

    if name == "gemini_analyze":
        repo = repo_root(a.get("repo_dir"))
        budget_check(a.get("force"))

        def snapshot():
            s = run("git", ["status", "--porcelain=v1", "-uall"], cwd=repo)
            d = run("git", ["diff"], cwd=repo)
            return hashlib.sha1((s["stdout"] + d["stdout"]).encode("utf-8")).hexdigest()

        before = snapshot()
        res = run_agy(
            cwd=repo,
            prompt=analyze_prompt(a.get("question") or "", a.get("paths"), a.get("max_words") or 600),
            model=a.get("model") or CONFIG["analyzeModel"] or CONFIG["model"],
            effort=a.get("effort") or CONFIG["effort"],
            timeout_min=a.get("timeout_minutes") or CONFIG["analyzeTimeoutMin"],
            skip_permissions=False,  # in the main repo: no auto-approval
            kind="analyze",
        )
        after = snapshot()
        out = []
        if res["notFound"]:
            raise RuntimeError('Command "%s" not found. Install Antigravity CLI and run `agy` once to log in.' % CONFIG["agyBin"])
        if not res["ok"]:
            out.append("ERROR (%s)%s:\n%s" % (res["status"], " — QUOTA/RATE LIMIT" if res["quota"] else "", truncate(res["error"], 2000)))
        if before != after:
            out.append("⚠️ WARNING: the repo state changed during the analysis. Check `git status` / `git diff`.")
        out.append(truncate(res["response"] or "(no response)", 16000))
        total = (res["usage"] or {}).get("total_tokens")
        out.append("\n[gemini_analyze: %ss, turns %s, tokens %s]" % (res["durationSeconds"], res["turns"], "?" if total is None else total))
        return {"text": "\n\n".join(out), "isError": not res["ok"] and not res["response"]}

    if name == "gemini_jobs":
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

    if name == "gemini_apply":
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

    if name == "gemini_discard":
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

    if name == "gemini_usage":
        u = usage_window(24)
        v = run(CONFIG["agyBin"], ["--version"])
        if not_found(v):
            agy = "NOT FOUND"
        else:
            agy = ((v["stdout"] or v["stderr"]).strip().split("\n")[0]) or "ok"
        active = [j for j in list(jobs.values()) if j["status"] not in TERMINAL]
        review = [j for j in list(jobs.values()) if j["status"] == "done"]
        return "\n".join([
            "agy: %s (%s)" % (agy, CONFIG["agyBin"]),
            "Last 24h: %d/%s runs, %d turns, %d tokens, %d failed, %d quota errors"
            % (u["runs"], CONFIG["dailyBudget"], u["turns"], u["total_tokens"], u["failures"], u["quota_errors"]),
            "Active jobs: %d (max parallel %s); to review: %d" % (len(active), CONFIG["maxParallel"], len(review)),
            "Config: model=%s analyze_model=%s effort=%s sandbox=%s skip_permissions_in_worktree=%s timeout_task=%sm" % (
                CONFIG["model"] or "(default agy)",
                CONFIG["analyzeModel"] or "(=model)",
                CONFIG["effort"] or "(default)",
                js(CONFIG["sandbox"]),
                js(CONFIG["skipPermissionsInWorktree"]),
                CONFIG["taskTimeoutMin"],
            ),
            "State: " + CONFIG["stateDir"],
        ])

    raise RuntimeError("unknown tool: %s" % name)


# ---------------------------------------------------------------- session summary (for the SessionStart hook)


def session_summary():
    v = run(CONFIG["agyBin"], ["--version"])
    if not_found(v):
        return ("[gemini-delegate] agy not found: delegation to Gemini is unavailable in this session "
                "(install Antigravity CLI and run `agy` to log in).")
    lines = []
    u = usage_window(24)
    left = max(CONFIG["dailyBudget"] - u["runs"], 0)
    lines.append(
        "[gemini-delegate] Delegation to Gemini available (MCP tools gemini_*). 24h budget: %s/%s runs left" % (left, CONFIG["dailyBudget"])
        + (", WARNING: %d recent quota errors" % u["quota_errors"] if u["quota_errors"] else "") + "."
    )
    try:
        repo = repo_root()
    except Exception:
        repo = None
    if repo:
        pending = [
            j for j in jobs.values()
            if j["repo"] == repo and (j["status"] == "interrupted" or (j["status"] in ("done", "failed") and (j.get("filesChanged") or 0) > 0))
        ]
        if pending:
            lines.append(
                "[gemini-delegate] Jobs from previous sessions still open in this repo: %s. "
                "Review them with gemini_jobs, then gemini_apply or gemini_discard."
                % "; ".join("%s (%s: %s)" % (j["id"], j["status"], j["title"]) for j in pending)
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
                "serverInfo": {"name": "gemini-delegate", "version": VERSION},
                "instructions": "Tools for delegating work to Gemini (Antigravity CLI) using the user's Google subscription. "
                "You remain the manager: you plan, delegate well-specified tasks, review the diffs, and apply or discard them.",
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
