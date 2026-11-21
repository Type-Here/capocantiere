# Shared helpers for the capocantiere server: paths, process runner, JSON files, text utilities.
# Standard library only.

import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone

env = os.environ
HERE = os.path.dirname(os.path.abspath(__file__))


def num(v, d):
    if v is None or v == "":
        return d
    try:
        f = float(v)
    except (TypeError, ValueError):
        return d
    return int(f) if f.is_integer() else f


def boolean(v, d):
    if v is None or v == "":
        return d
    if isinstance(v, bool):
        return v
    return re.match(r"^(1|true|yes|on)$", str(v), re.I) is not None


def _xdg(var, *default):
    return env.get(var) or os.path.join(os.path.expanduser("~"), *default)


# ---------------------------------------------------------------- paths
#
# New names (CAPOCANTIERE_*) win; the old GEMINI_DELEGATE_* variables are still honored.

STATE_DIR = (env.get("CAPOCANTIERE_STATE_DIR") or env.get("GEMINI_DELEGATE_STATE_DIR")
             or os.path.join(_xdg("XDG_STATE_HOME", ".local", "state"), "capocantiere"))
WORKTREE_DIR = (env.get("CAPOCANTIERE_WORKTREE_DIR") or env.get("GEMINI_DELEGATE_WORKTREE_DIR")
                or os.path.join(_xdg("XDG_CACHE_HOME", ".cache"), "capocantiere", "worktrees"))
CONFIG_FILE = (env.get("CAPOCANTIERE_CONFIG")
               or os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "capocantiere", "workers.json"))
LEGACY_STATE_DIR = os.path.join(_xdg("XDG_STATE_HOME", ".local", "state"), "gemini-delegate")

JOBS_DIR = os.path.join(STATE_DIR, "jobs")
PATCH_DIR = os.path.join(STATE_DIR, "patches")
USAGE_FILE = os.path.join(STATE_DIR, "usage.jsonl")
SETTINGS_FILE = os.path.join(STATE_DIR, "settings.json")


def migrate_legacy_state():
    """Move the state of the old gemini-delegate server to the new folder, once.

    Old worktrees stay where they are: git keeps their absolute path, and the jobs point to it.
    Patch paths inside the jobs are rewritten to the new folder.
    """
    if os.path.exists(STATE_DIR) or not os.path.isdir(LEGACY_STATE_DIR):
        return
    if env.get("CAPOCANTIERE_STATE_DIR") or env.get("GEMINI_DELEGATE_STATE_DIR"):
        return  # explicit state dir: don't touch anything
    os.makedirs(os.path.dirname(STATE_DIR), exist_ok=True)
    try:
        os.rename(LEGACY_STATE_DIR, STATE_DIR)
    except OSError:
        shutil.copytree(LEGACY_STATE_DIR, STATE_DIR)
    jobs_dir = os.path.join(STATE_DIR, "jobs")
    old_patches = os.path.join(LEGACY_STATE_DIR, "patches")
    for f in os.listdir(jobs_dir) if os.path.isdir(jobs_dir) else []:
        p = os.path.join(jobs_dir, f)
        j = read_json(p, None)
        if not j:
            continue
        if (j.get("patchFile") or "").startswith(old_patches):
            j["patchFile"] = PATCH_DIR + j["patchFile"][len(old_patches):]
        j.setdefault("worker", "agy")  # every job before workers existed ran on agy
        write_json(p, j)


def private_dir(d):
    """A folder only the user can read: jobs hold tasks, responses and patches; worktrees hold the code."""
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        if os.name == "posix" and os.stat(d).st_mode & 0o077:
            os.chmod(d, 0o700)
    except OSError:
        pass


def ensure_dirs():
    migrate_legacy_state()
    private_dir(STATE_DIR)
    if not (env.get("CAPOCANTIERE_WORKTREE_DIR") or env.get("GEMINI_DELEGATE_WORKTREE_DIR")):
        private_dir(os.path.dirname(WORKTREE_DIR))  # ~/.cache/capocantiere, ours (a folder you chose stays as it is)
    for d in (JOBS_DIR, PATCH_DIR, WORKTREE_DIR):
        private_dir(d)


# ---------------------------------------------------------------- time / text


def now_ms():
    return int(time.time() * 1000)


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def js(v):
    """Format a value for text output (JavaScript-style: true/false/null)."""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v)


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
    return datetime.now().strftime("j%m%d-%H%M-") + secrets.token_hex(2)


# ---------------------------------------------------------------- JSON files


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else default
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------- processes

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


TOKEN_VAR_RE = re.compile(r"^CAPOCANTIERE_.*_TOKEN$")


def child_env(extra_env=None, full_env=None):
    """The environment of a child process. Workers' tokens (CAPOCANTIERE_<WORKER>_TOKEN) never pass through:
    the server injects each one only into its own worker, under the CLI's variable."""
    if full_env is not None:
        return dict(full_env)
    e = {k: v for k, v in os.environ.items() if not TOKEN_VAR_RE.match(k)}
    e.update(extra_env or {})
    return e


def run(cmd, args, cwd=None, input=None, timeout_ms=None, on_spawn=None, binary=False, extra_env=None, full_env=None):
    """Run a command; never raises. Returns code/stdout/stderr/error/timed_out.

    extra_env is added to the server's environment; full_env replaces it (isolated runs)."""
    try:
        child = subprocess.Popen(
            [cmd] + list(args),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env(extra_env, full_env),
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
