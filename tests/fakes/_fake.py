"""Shared logic of the fake worker CLIs used by the end-to-end tests.

The scenario is chosen by a marker in the prompt, so one server can run several scenarios at once:

  (no marker)          edit a file in the working directory and answer
  FAKE:noop            answer without touching any file
  FAKE:quota           fail with a quota / rate-limit error
  FAKE:quota-<worker>  fail with a quota error only on that worker (e.g. FAKE:quota-copilot)
  FAKE:error           fail with a generic error
  FAKE:timeout         hang for a long time (the server must kill it; vibe stops itself at --time-limit)
  FAKE:price           vibe only: stop on --max-price (exit 3, outcome price_limit)
  FAKE:slow            sleep 3 seconds, then edit (to check that jobs run in parallel)
  FAKE:probe           edit, and report which paths given as PROBE:<path> in the prompt are readable
  FAKE:write           write the blocks "FILE:<path>" + "<<<" ... ">>>" of the prompt (whole-file contents)

Every call is appended to $FAKE_LOG as one JSON line: {"bin", "argv", "cwd", "scenario", "env"}, where env holds
the variables that matter for credentials and isolation (see WATCHED_ENV).
"""

import json
import os
import re
import sys
import time


def scenario(prompt, worker):
    m = re.findall(r"FAKE:([a-z-]+)", prompt or "")
    for s in m:
        if s == "quota-" + worker:
            return "quota"
        if not s.startswith("quota-"):
            return s
    return "edit"


WATCHED_ENV = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "MISTRAL_API_KEY", "GEMINI_API_KEY", "DBUS_SESSION_BUS_ADDRESS",
               "TMPDIR", "XDG_CURRENT_DESKTOP")


def log(worker, argv, cwd, sc, extra=None):
    path = os.environ.get("FAKE_LOG")
    if path:
        e = {k: v for k, v in os.environ.items() if k in WATCHED_ENV or k.startswith("CAPOCANTIERE_")}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict({"bin": worker, "argv": argv, "cwd": cwd, "scenario": sc, "env": e}, **(extra or {}))) + "\n")


def probe(prompt):
    """FAKE:probe: which PROBE:<path> paths can be read from here."""
    out = {}
    for p in re.findall(r"PROBE:(\S+)", prompt or ""):
        try:
            with open(p, "rb") as f:
                f.read(1)
            out[p] = True
        except OSError:
            out[p] = False
    return out


def write_blocks(prompt, cwd):
    """FAKE:write: every FILE:<path> followed by a <<< ... >>> block replaces that file's contents."""
    written = []
    for path, body in re.findall(r"FILE:(\S+)\s*\n<<<\n(.*?)>>>", prompt or "", re.S):
        target = os.path.join(cwd, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(body)
        written.append(path)
    return written


def act(worker, sc, cwd, may_write, prompt=None):
    """Common behavior. Returns (exit code, response text, stderr text)."""
    if sc == "timeout":
        time.sleep(600)
    if sc == "quota":
        return 1, "", QUOTA_MESSAGES[worker]
    if sc == "error":
        return 2, "", "Error: something unexpected happened in the fake %s\n" % worker
    if sc == "slow":
        time.sleep(3)
    if sc == "write" and may_write:
        files = write_blocks(prompt, cwd)
        return 0, "SUMMARY: wrote %s\nFILES: %s\nVERIFICATION: not verified\nNOTES: none" % (
            ", ".join(files), ", ".join(files)), ""
    if sc in ("edit", "slow", "probe", "write"):
        if not may_write:
            return 0, "I could not edit files: permission denied for write.", "Tool 'write' denied: requires approval\n"
        target = os.path.join(cwd, "fake-%s.txt" % worker)
        with open(target, "a", encoding="utf-8") as f:
            f.write("written by fake %s\n" % worker)
        return 0, "SUMMARY: wrote fake-%s.txt\nFILES: fake-%s.txt\nVERIFICATION: not verified\nNOTES: none" % (worker, worker), ""
    return 0, "SUMMARY: nothing to change\nFILES: none\nVERIFICATION: not verified\nNOTES: none", ""


QUOTA_MESSAGES = {
    "copilot": "Error: You have used all premium requests in your plan (Plan: 100% used, resets 2026-12-01). "
               "Upgrade at https://github.com/settings/copilot\n",
    "vibe": "Error: API error 429 Too Many Requests: rate limit exceeded for the Experiment plan\n",
}


def finish(code, out, err):
    if out:
        sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    sys.exit(code)
