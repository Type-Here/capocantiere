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

Every call is appended to $FAKE_LOG as one JSON line: {"bin", "argv", "cwd", "scenario"}.
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


def log(worker, argv, cwd, sc):
    path = os.environ.get("FAKE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"bin": worker, "argv": argv, "cwd": cwd, "scenario": sc}) + "\n")


def act(worker, sc, cwd, may_write):
    """Common behavior. Returns (exit code, response text, stderr text)."""
    if sc == "timeout":
        time.sleep(600)
    if sc == "quota":
        return 1, "", QUOTA_MESSAGES[worker]
    if sc == "error":
        return 2, "", "Error: something unexpected happened in the fake %s\n" % worker
    if sc == "slow":
        time.sleep(3)
    if sc in ("edit", "slow"):
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
