#!/usr/bin/env python3
"""End-to-end tests of the capocantiere MCP server, against the fake worker CLIs in tests/fakes.

    python3 tests/e2e.py                    # Python server
    python3 tests/e2e.py --runtime node     # Node server
    python3 tests/e2e.py -k fallback        # only tests whose name contains "fallback"

Every test gets its own temporary git repo, state/cache/config folders and server process.
No real worker CLI is ever started: the config points every worker to a fake binary.
"""

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_SH = os.path.join(ROOT, "plugins", "capocantiere", "server", "run.sh")
FAKES = os.path.join(ROOT, "tests", "fakes")
RUNTIME = "python"


class Server:
    """A capocantiere server process spoken to over MCP stdio."""

    def __init__(self, env):
        self.p = subprocess.Popen(["sh", RUN_SH], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env)
        self.next_id = 0
        self.waiting = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "e2e", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for line in self.p.stdout:
            msg = json.loads(line)
            q = self.waiting.get(msg.get("id"))
            if q:
                q.put(msg)

    def _send(self, msg):
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def request(self, method, params, timeout=60):
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            q = self.waiting[mid] = queue.Queue()
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError("no answer to %s within %ss" % (method, timeout))
        if "error" in msg:
            raise AssertionError("JSON-RPC error: %s" % msg["error"])
        return msg["result"]

    def call(self, name, args=None, timeout=60):
        """Returns (text, is_error)."""
        r = self.request("tools/call", {"name": name, "arguments": args or {}}, timeout)
        return r["content"][0]["text"], r.get("isError", False)

    def ok(self, name, args=None, timeout=60):
        text, err = self.call(name, args, timeout)
        if err:
            raise AssertionError("%s returned an error:\n%s" % (name, text))
        return text

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def job_id(text):
    for word in text.replace("\n", " ").split():
        if word.startswith("j") and word.count("-") == 2 and len(word) >= 14:
            return word.rstrip(".,:")
    raise AssertionError("no job id in:\n" + text)


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="capocantiere-e2e-")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        git("init", "-q")
        with open(os.path.join(self.repo, "main.txt"), "w") as f:
            f.write("hello\n")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        self.log = os.path.join(self.tmp, "calls.jsonl")
        copilot_home = os.path.join(self.tmp, "copilot-home")
        os.makedirs(copilot_home)
        with open(os.path.join(copilot_home, "config.json"), "w") as f:
            f.write('// managed\n{\n  "lastLoggedInUser": {"host": "https://github.com", "login": "student"}\n}\n')
        self.vibe_home = os.path.join(self.tmp, "vibe-home")
        os.makedirs(self.vibe_home)
        self.vibe_config('outside_workdir = "deny"\n')
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("CAPOCANTIERE_", "GEMINI_DELEGATE_", "COPILOT_", "GH_TOKEN", "GITHUB_TOKEN", "VIBE_", "MISTRAL_",
                             "GEMINI_API_KEY", "AGY_"))}
        self.env.update({
            "CAPOCANTIERE_RUNTIME": RUNTIME,
            "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
            "XDG_CACHE_HOME": os.path.join(self.tmp, "cache"),
            "XDG_CONFIG_HOME": os.path.join(self.tmp, "config"),
            "CLAUDE_PROJECT_DIR": self.repo,
            "COPILOT_HOME": copilot_home,
            "VIBE_HOME": self.vibe_home,
            "MISTRAL_API_KEY": "fake-key",
            "FAKE_LOG": self.log,
        })
        self.write_config()
        self._server = None

    def tearDown(self):
        if self._server:
            self._server.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def server(self):
        if not self._server:
            self._server = Server(self.env)
        return self._server

    def write_config(self, **workers):
        cfg = {"order": ["copilot", "vibe", "agy"], "fallback": True, "max_parallel": 4, "workers": {
            "copilot": {"bin": os.path.join(FAKES, "copilot")},
            "vibe": {"bin": os.path.join(FAKES, "vibe")},
            "agy": {"enabled": False, "bin": os.path.join(self.tmp, "no-agy")},
        }}
        for wid, s in workers.items():
            cfg["workers"][wid].update(s)
        d = os.path.join(self.tmp, "config", "capocantiere")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "workers.json"), "w") as f:
            json.dump(cfg, f)

    def vibe_config(self, text):
        with open(os.path.join(self.vibe_home, "config.toml"), "w") as f:
            f.write(text)

    def calls(self, worker=None):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            out = [json.loads(l) for l in f if l.strip()]
        return [c for c in out if worker is None or c["bin"] == worker]

    def delegate(self, task, wait=30, **kw):
        text = self.server.ok("delegate", dict(task=task, wait_seconds=wait, **kw), timeout=wait + 30)
        return job_id(text), text

    def branches(self):
        out = subprocess.run(["git", "branch", "--list", "capocantiere/*"], cwd=self.repo, capture_output=True, text=True)
        return out.stdout.strip()

    # ------------------------------------------------------------ tools and workers

    def test_tools_list(self):
        names = [t["name"] for t in self.server.request("tools/list", {})["tools"]]
        self.assertEqual(names, ["delegate", "analyze", "jobs", "apply", "discard", "workers", "mode", "models"])

    def test_workers_report(self):
        text = self.server.ok("workers")
        self.assertIn("copilot — GitHub Copilot CLI [enabled]", text)
        self.assertIn("installed: GitHub Copilot CLI 0.0.0-fake.", text)
        self.assertIn("authenticated: yes (logged in, per", text)
        self.assertIn("vibe — Mistral Vibe CLI [enabled]", text)
        self.assertIn("authenticated: yes (MISTRAL_API_KEY set)", text)
        self.assertIn("agy — Antigravity CLI (Gemini) [DISABLED]", text)
        self.assertIn("can get the account suspended", text)
        self.assertIn("budget 24h: 0/40 used, 40 left", text)
        self.assertIn("strengths: tests, boilerplate, refactor, docs", text)

    def test_missing_binary_is_skipped(self):
        self.write_config(copilot={"bin": os.path.join(self.tmp, "nope")})
        self.assertIn("installed: NO", self.server.ok("workers"))
        jid, text = self.delegate("add a file")
        self.assertIn("worker: vibe", text)
        self.assertIn("copilot skipped: command", text)

    # ------------------------------------------------------------ delegate

    def test_parallel_on_both_workers(self):
        t0 = time.time()
        a = job_id(self.server.ok("delegate", {"task": "FAKE:slow add a file", "worker": "copilot"}))
        b = job_id(self.server.ok("delegate", {"task": "FAKE:slow add a file", "worker": "vibe"}))
        self.server.ok("jobs", {"wait_seconds": 30}, timeout=60)
        elapsed = time.time() - t0
        da = self.server.ok("jobs", {"job_id": a})
        db = self.server.ok("jobs", {"job_id": b})
        self.assertIn("status: done", da)
        self.assertIn("worker: copilot", da)
        self.assertIn("fake-copilot.txt", da)
        self.assertIn("status: done", db)
        self.assertIn("worker: vibe", db)
        self.assertIn("fake-vibe.txt", db)
        self.assertLess(elapsed, 5.5, "the two 3-second jobs did not run in parallel")
        listing = self.server.ok("jobs")
        self.assertIn('"worker":"copilot"', listing)
        self.assertIn('"worker":"vibe"', listing)

    def test_worker_flags(self):
        jc, _ = self.delegate("add a file", worker="copilot")
        jv, _ = self.delegate("add a file", worker="vibe")
        c = self.calls("copilot")[0]
        self.assertTrue(c["cwd"].endswith(jc), c["cwd"])
        for flag in ("-s", "--allow-all-tools", "--no-ask-user", "--disable-builtin-mcps", "--usage-output-file"):
            self.assertIn(flag, c["argv"])
        self.assertIn("shell(git push)", c["argv"])
        self.assertNotIn("--allow-all-paths", c["argv"])
        self.assertNotIn("--allow-all", c["argv"])
        self.assertNotIn("--yolo", c["argv"])
        v = self.calls("vibe")[0]
        self.assertTrue(v["cwd"].endswith(jv), v["cwd"])
        self.assertIn("--auto-approve", v["argv"])
        self.assertEqual(v["argv"][v["argv"].index("--output") + 1], "json")
        self.assertEqual(v["argv"][v["argv"].index("--max-price") + 1], "0.5")
        self.assertIn("--prompt-file", v["argv"])
        self.assertEqual(v["argv"][v["argv"].index("--time-limit") + 1], "1200")  # timeout_min 20
        self.assertIn("--output-dir", v["argv"])
        self.assertIn("--trust", v["argv"])
        self.assertNotIn("--enabled-tools", v["argv"])
        prompt_file = v["argv"][v["argv"].index("--prompt-file") + 1]
        self.assertFalse(os.path.exists(prompt_file), "the prompt file was not deleted")

    def test_vibe_reads_last_assistant_message(self):
        jid, text = self.delegate("add a file", worker="vibe")
        self.assertIn("WORKER'S RESPONSE:\nSUMMARY: wrote fake-vibe.txt", text)
        self.assertNotIn("You are Vibe", text)

    def test_copilot_usage_file(self):
        jid, text = self.delegate("add a file", worker="copilot")
        self.assertIn("cost 2.5", text)  # AI credits from totalNanoAiu
        self.assertIn("cost 2.5", self.server.ok("workers"))

    def test_no_changes(self):
        jid, text = self.delegate("FAKE:noop look around", worker="copilot")
        self.assertIn("status: no_changes", text)
        self.assertEqual(self.branches(), "")
        self.assertFalse(os.path.exists(self.calls("copilot")[0]["cwd"]))

    def test_timeout_killed_by_server(self):
        t0 = time.time()
        jid, text = self.delegate("FAKE:timeout", worker="copilot", timeout_minutes=0.05, fallback=False)
        self.assertIn("status: failed", text)
        self.assertIn("timed out after 0.05 min", text)
        self.assertLess(time.time() - t0, 20)

    def test_timeout_vibe_time_limit(self):
        t0 = time.time()
        jid, text = self.delegate("FAKE:timeout", worker="vibe", timeout_minutes=0.05, fallback=False)
        self.assertIn("status: failed (vibe: TIME_LIMIT)", text)
        self.assertIn("time limit reached", text)
        self.assertIn("outcome: deadline / stop reason: interrupted", text)
        self.assertEqual(self.calls("vibe")[0]["argv"][self.calls("vibe")[0]["argv"].index("--time-limit") + 1], "3")
        self.assertLess(time.time() - t0, 20)

    def test_vibe_export_usage(self):
        jid, text = self.delegate("add a file", worker="vibe")
        self.assertIn("cost 0.0123", text)
        out = self.server.ok("analyze", {"question": "FAKE:noop what?", "worker": "vibe"})
        self.assertIn("tokens 1000]", out)

    def test_vibe_account_login(self):
        del self.env["MISTRAL_API_KEY"]
        self.assertIn("authenticated: NO (not signed in", self.server.ok("workers"))
        with open(os.path.join(self.vibe_home, "whoami_cache.json"), "w") as f:
            json.dump({"format": 3, "payload": {"plan_type": "chat", "plan_name": "FREE"}}, f)
        self.assertIn("authenticated: yes (Mistral account, plan FREE", self.server.ok("workers"))

    def test_vibe_config_warnings(self):
        self.vibe_config("auto_worktree = true\n")
        text = self.server.ok("workers")
        self.assertIn('does not set outside_workdir = "deny"', text)
        self.assertIn("sets auto_worktree = true", text)
        self.vibe_config('outside_workdir = "deny"\n')
        self.assertNotIn("outside_workdir", self.server.ok("workers"))

    def test_generic_error_does_not_fall_back(self):
        jid, text = self.delegate("FAKE:error", worker="copilot")
        self.assertIn("status: failed", text)
        self.assertIn("worker: copilot", text)
        self.assertEqual(self.calls("vibe"), [])

    # ------------------------------------------------------------ fallback

    def test_fallback_copilot_to_vibe_on_quota(self):
        jid, text = self.delegate("FAKE:quota-copilot add a file", worker="copilot")
        self.assertIn("status: done", text)
        self.assertIn("worker: vibe", text)
        self.assertIn("earlier attempt: copilot failed (quota)", text)
        self.assertIn("copilot hit a quota/rate limit: relaunched on vibe", text)
        self.assertIn("fake-vibe.txt", text)
        self.assertNotIn("fake-copilot.txt", text)
        self.assertIn("1 quota errors", self.server.ok("workers"))

    def test_fallback_off(self):
        jid, text = self.delegate("FAKE:quota-copilot add a file", worker="copilot", fallback=False)
        self.assertIn("status: failed", text)
        self.assertIn("QUOTA EXHAUSTED / rate limit on copilot (fallback off)", text)
        self.assertEqual(self.calls("vibe"), [])

    def test_fallback_vibe_to_copilot_on_free_plan_exhausted(self):
        self.write_config()  # default order: copilot first
        jid, text = self.delegate("FAKE:quota-vibe add a file", worker="vibe")
        self.assertIn("status: done", text)
        self.assertIn("worker: copilot", text)
        self.assertIn("earlier attempt: vibe failed (quota)", text)
        self.assertIn("fake-copilot.txt", text)

    def test_vibe_price_limit_does_not_fall_back(self):
        jid, text = self.delegate("FAKE:price add a file", worker="vibe")
        self.assertIn("status: failed (vibe: PRICE_LIMIT)", text)
        self.assertIn("vibe stopped on a limit: price_limit", text)
        self.assertEqual(self.calls("copilot"), [])

    def test_quota_on_every_worker(self):
        jid, text = self.delegate("FAKE:quota")
        self.assertIn("status: failed", text)
        self.assertIn("worker: vibe", text)
        self.assertIn("QUOTA EXHAUSTED / rate limit on vibe", text)
        self.assertIn("earlier attempt: copilot failed (quota)", text)

    def test_analyze_fallback(self):
        text = self.server.ok("analyze", {"question": "FAKE:quota-copilot FAKE:noop where is main?"})
        self.assertIn("copilot hit a quota/rate limit: asked vibe instead", text)
        self.assertIn("[analyze: vibe", text)

    # ------------------------------------------------------------ budget

    def test_budget_per_worker(self):
        self.write_config(copilot={"daily_budget": 1})
        j1, t1 = self.delegate("add a file")
        self.assertIn("worker: copilot", t1)
        j2, t2 = self.delegate("add another file")
        self.assertIn("worker: vibe", t2)
        self.assertIn("copilot skipped: daily budget used up (1/1", t2)
        text, err = self.server.call("delegate", {"task": "x", "worker": "copilot", "fallback": False})
        self.assertTrue(err)
        self.assertIn("No worker available", text)
        j3, t3 = self.delegate("forced", worker="copilot", fallback=False, force=True)
        self.assertIn("worker: copilot", t3)
        report = self.server.ok("workers")
        self.assertIn("budget 24h: 2/1 used, 0 left", report)
        self.assertIn("budget 24h: 1/60 used, 59 left", report)

    # ------------------------------------------------------------ analyze

    def test_analyze_is_read_only(self):
        text = self.server.ok("analyze", {"question": "what is in main.txt?", "worker": "copilot"})
        self.assertIn("[analyze: copilot", text)
        c = self.calls("copilot")[0]
        self.assertEqual(c["cwd"], self.repo)
        self.assertNotIn("--allow-all-tools", c["argv"])
        self.assertIn("write", c["argv"][c["argv"].index("--deny-tool") + 1:][:1])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "fake-copilot.txt")))
        self.server.ok("analyze", {"question": "what is in main.txt?", "worker": "vibe"})
        v = self.calls("vibe")[0]["argv"]
        self.assertNotIn("--auto-approve", v)
        self.assertEqual([v[i + 1] for i, a in enumerate(v) if a == "--enabled-tools"], ["read_file", "grep"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "fake-vibe.txt")))

    # ------------------------------------------------------------ apply / discard

    def test_apply(self):
        jid, text = self.delegate("add a file", worker="copilot")
        self.assertIn("status: done", text)
        applied = self.server.ok("apply", {"job_id": jid})
        self.assertIn("Applied (clean)", applied)
        with open(os.path.join(self.repo, "fake-copilot.txt")) as f:
            self.assertEqual(f.read(), "written by fake copilot\n")
        self.assertEqual(self.branches(), "")
        self.assertIn("No open jobs", self.server.ok("jobs"))

    def test_discard(self):
        jid, text = self.delegate("add a file", worker="vibe")
        worktree = self.calls("vibe")[0]["cwd"]
        self.assertTrue(os.path.exists(worktree))
        self.assertIn("discarded", self.server.ok("discard", {"job_id": jid}))
        self.assertFalse(os.path.exists(worktree))
        self.assertEqual(self.branches(), "")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "fake-vibe.txt")))

    def test_discard_running_job(self):
        jid = job_id(self.server.ok("delegate", {"task": "FAKE:timeout", "worker": "copilot"}))
        time.sleep(1)
        self.assertIn("discarded", self.server.ok("discard", {"job_id": jid}))
        self.assertEqual(self.branches(), "")

    # ------------------------------------------------------------ agy authentication

    def agy_config(self, **s):
        self.write_config(agy=dict({"enabled": True, "bin": os.path.join(FAKES, "agy")}, **s))

    def test_agy_api_key_missing_is_skipped(self):
        self.agy_config()
        text = self.server.ok("workers")
        self.assertIn('authenticated: NO (auth is "api_key" but GEMINI_API_KEY is not set', text)
        self.assertNotIn("can get the account suspended", text)  # enabled with an API key setup: no FAQ warning
        jid, text = self.delegate("add a file", worker="agy")
        self.assertIn("agy skipped: auth is \"api_key\" but GEMINI_API_KEY is not set", text)
        self.assertIn("worker: copilot", text)
        self.assertEqual(self.calls("agy"), [])

    def test_agy_api_key(self):
        self.agy_config(env={"GEMINI_API_KEY": "fake"})
        jid, text = self.delegate("add a file", worker="agy")
        self.assertIn("status: done", text)
        self.assertIn("worker: agy", text)
        self.assertIn("fake-agy.txt", text)
        a = self.calls("agy")[0]["argv"]
        self.assertEqual(json.loads(a[a.index("--settings") + 1]), {"modelProvider": "gemini"})
        self.assertIn("ENV:GEMINI_API_KEY=set", a)
        self.assertIn("--dangerously-skip-permissions", a)
        self.assertIn("authenticated: yes (Gemini API key", self.server.ok("workers"))

    def test_agy_adc(self):
        self.agy_config(auth="adc")
        jid, text = self.delegate("add a file", worker="agy")
        a = self.calls("agy")[0]["argv"]
        self.assertNotIn("--settings", a)
        self.assertIn("ENV:AGY_ADC_AUTH=true", a)

    def test_agy_login_keeps_the_warning(self):
        self.agy_config(auth="login")
        self.assertIn("can get the account suspended", self.server.ok("workers"))
        out = subprocess.run(["sh", RUN_SH, "--session-summary"], env=self.env, capture_output=True, text=True).stdout
        self.assertIn("agy: According to the official Antigravity FAQ", out)

    def test_example_config_loads_cleanly(self):
        d = os.path.join(self.tmp, "config", "capocantiere")
        shutil.copy(os.path.join(ROOT, "plugins", "capocantiere", "workers.example.json"), os.path.join(d, "workers.json"))
        text = self.server.ok("workers")
        self.assertNotIn("CONFIG ERROR", text)
        self.assertIn("agy — Antigravity CLI (Gemini) [DISABLED]", text)

    # ------------------------------------------------------------ session, mode, hook

    def test_session_default(self):
        self.assertIn("Session default worker: vibe", self.server.ok("workers", {"use": "vibe"}))
        jid, text = self.delegate("add a file")
        self.assertIn("worker: vibe", text)
        self.assertIn("cleared", self.server.ok("workers", {"use": "auto"}))
        jid, text = self.delegate("add a file")
        self.assertIn("worker: copilot", text)
        text, err = self.server.call("workers", {"use": "agy"})
        self.assertTrue(err)
        self.assertIn("disabled", text)

    def test_mode_off(self):
        self.server.ok("mode", {"mode": "off"})
        text, err = self.server.call("delegate", {"task": "x"})
        self.assertTrue(err)
        self.assertIn("Delegation is off", text)
        self.assertEqual(self.calls(), [])

    def test_session_summary(self):
        out = subprocess.run(["sh", RUN_SH, "--session-summary"], env=self.env, capture_output=True, text=True).stdout
        self.assertIn("copilot (40/40 runs left", out)
        self.assertIn("vibe (60/60 runs left", out)
        self.assertNotIn("agy (", out)
        self.assertIn("Delegation mode: auto", out)

    def test_legacy_state_migration(self):
        legacy = os.path.join(self.tmp, "state", "gemini-delegate")
        os.makedirs(os.path.join(legacy, "jobs"))
        os.makedirs(os.path.join(legacy, "patches"))
        with open(os.path.join(legacy, "jobs", "g0101-0000-abcd.json"), "w") as f:
            json.dump({"id": "g0101-0000-abcd", "title": "old job", "status": "done", "repo": self.repo,
                       "worktree": "/nonexistent", "branch": "gemini/x", "createdAt": "2026-01-01T00:00:00Z",
                       "filesChanged": 1, "patchFile": os.path.join(legacy, "patches", "g0101-0000-abcd.patch")}, f)
        with open(os.path.join(legacy, "usage.jsonl"), "w") as f:
            f.write(json.dumps({"ts": int(time.time() * 1000), "kind": "implement", "ok": True}) + "\n")
        self.write_config(agy={"enabled": True, "bin": os.path.join(FAKES, "copilot")})  # any binary that answers --version
        listing = self.server.ok("jobs")
        self.assertIn('"worker":"agy"', listing)
        self.assertIn("budget 24h: 1/150 used", self.server.ok("workers"))
        self.assertFalse(os.path.exists(legacy))
        with open(os.path.join(self.tmp, "state", "capocantiere", "jobs", "g0101-0000-abcd.json")) as f:
            self.assertIn(os.path.join("capocantiere", "patches"), json.load(f)["patchFile"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", default="python", choices=["python", "node"])
    ap.add_argument("-k", default=None, help="only run tests whose name contains this")
    args, rest = ap.parse_known_args()
    RUNTIME = args.runtime
    loader = unittest.TestLoader()
    if args.k:
        loader.testNamePatterns = ["*%s*" % args.k]
    suite = loader.loadTestsFromTestCase(E2E)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
