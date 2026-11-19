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
            "GIT_CONFIG_GLOBAL": os.devnull,  # the user's git config (e.g. commit signing) must not affect the tests
            "GIT_CONFIG_NOSYSTEM": "1",
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

    def write_config(self, glob=None, **workers):
        # "order": the tests that predate trust scores rely on the preference order; the trust score tests
        # pass glob={"selection": "scores"}
        cfg = {"order": ["copilot", "vibe", "agy"], "fallback": True, "max_parallel": 4, "selection": "order", "workers": {
            "copilot": {"bin": os.path.join(FAKES, "copilot")},
            "vibe": {"bin": os.path.join(FAKES, "vibe")},
            "agy": {"enabled": False, "bin": os.path.join(self.tmp, "no-agy")},
        }}
        cfg.update(glob or {})
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
        expected = ["delegate", "analyze", "jobs", "apply", "discard", "rate", "blame", "workers", "mode", "models"]
        if RUNTIME == "node":
            expected.remove("rate")
            expected.remove("blame")  # the Node server is frozen: Python-only tools are not listed
        self.assertEqual(names, expected)

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
        self.assertIn("tokens 1000, id a", out)

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

    # ------------------------------------------------------------ attribution (blame)

    APP = "function add(a, b) {\n  return a + b;\n}\n\nfunction mul(a, b) {\n  return a * b;\n}\n"

    @staticmethod
    def write_task(files):
        """A task for the FAKE:write scenario: the worker writes these whole files."""
        return "FAKE:write\n" + "".join("FILE:%s\n<<<\n%s>>>\n" % (p, c) for p, c in files.items())

    def write_file(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def records(self):
        path = os.path.join(self.repo, ".git", "capocantiere", "attribution.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def apply_job(self, files, worker="copilot", title="write code"):
        jid, text = self.delegate(self.write_task(files), worker=worker, fallback=False, title=title)
        self.assertIn("status: done", text)
        self.server.ok("apply", {"job_id": jid})
        return jid

    def blame(self, path, **kw):
        if RUNTIME == "node":
            self.skipTest("blame is Python-only")
        return self.server.ok("blame", dict(path=path, **kw))

    def test_effective_model(self):
        jid, text = self.delegate("add a file", worker="copilot", fallback=False)
        self.assertIn("ran as fake-model", text)  # Copilot's usage file: modelMetrics
        jid, text = self.delegate("add a file", worker="vibe", fallback=False)
        self.assertIn("ran as devstral-fake-2512", text)  # vibe's export.json: config.active_model -> models

    def test_apply_records_an_agent_trace(self):
        if RUNTIME == "node":
            self.skipTest("attribution is Python-only")
        jid = self.apply_job({"src/app.js": self.APP})
        recs = self.records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["version"], "0.1.0")
        self.assertEqual(r["tool"]["name"], "capocantiere")
        self.assertEqual(set(r), {"version", "id", "timestamp", "tool", "files", "metadata"})
        f = r["files"][0]
        self.assertEqual(f["path"], "src/app.js")
        conv = f["conversations"][0]
        self.assertEqual(conv["contributor"], {"type": "ai", "model_id": "copilot/fake-model"})
        # one run of added lines, trimmed of the trailing "}": lines 1-6
        self.assertEqual([(x["start_line"], x["end_line"]) for x in conv["ranges"]], [(1, 6)])
        self.assertTrue(conv["ranges"][0]["content_hash"].startswith("sha1:"))
        meta = r["metadata"]["dev.capocantiere"]
        self.assertEqual((meta["job"], meta["worker"], meta["model"], meta["title"]), (jid, "copilot", "fake-model", "write code"))
        self.assertEqual(len(meta["fingerprints"]["src/app.js"]), 4)  # "}" and the blank line are trivial
        self.assertEqual(len(meta["patch_sha256"]), 64)
        # the record lives in the repo's .git, never in the working tree
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertNotIn("capocantiere", st)

    def test_discard_records_nothing(self):
        jid, _ = self.delegate(self.write_task({"src/app.js": self.APP}), worker="copilot", fallback=False)
        self.server.ok("discard", {"job_id": jid})
        self.assertEqual(self.records(), [])

    def test_blame_after_apply(self):
        jid = self.apply_job({"src/app.js": self.APP})
        text = self.blame("src/app.js")
        self.assertIn("src/app.js:1-7  7/7 lines from workers", text)
        self.assertIn('  1-7  %s copilot/fake-model ' % jid, text)
        self.assertIn('"write code"', text)
        self.assertEqual(len(text.split("\n")), 2)
        text = self.blame("main.txt")
        self.assertIn("0/1 lines from workers (no applied job ever touched this file)", text)
        self.assertIn("  1-1  not from a worker", text)

    def commit(self, msg="work"):
        git = lambda *a: subprocess.run(["git", *a], cwd=self.repo, check=True, capture_output=True)
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)

    def test_blame_after_commit_moves_and_edits(self):
        jid = self.apply_job({"src/app.js": self.APP})
        self.commit()
        # by hand: a header, the functions swapped, one line edited, a new function
        self.write_file("src/app.js", "// header written by hand\n"
                                      "function mul(a, b) {\n  return a * b;\n}\n\n"
                                      "function add(a, b) {\n  return a + b + 0;\n}\n"
                                      "function sub(a, b) { return a - b; }\n")
        text = self.blame("src/app.js")
        self.assertIn("src/app.js:1-9  5/9 lines from workers", text)
        lines = text.split("\n")[1:]
        self.assertEqual(lines[0], "  1-1  not from a worker")
        self.assertTrue(lines[1].startswith("  2-6  %s copilot/fake-model" % jid), lines[1])
        self.assertEqual(lines[2], "  7-9  not from a worker")
        # a sub-range
        self.assertEqual(self.blame("src/app.js", start=7, end=8).split("\n")[1:], ["  7-8  not from a worker"])

    def test_blame_two_jobs_on_one_file(self):
        first = self.apply_job({"src/app.js": self.APP}, worker="copilot", title="add and mul")
        # no commit in between: the file the first job created is untracked, and the second job still sees it
        more = self.APP + "\nfunction sub(a, b) {\n  return a - b;\n}\n"
        second = self.apply_job({"src/app.js": more}, worker="vibe", title="sub")
        text = self.blame(os.path.join(self.repo, "src", "app.js"))  # an absolute path works too
        lines = text.split("\n")[1:]
        self.assertTrue(lines[0].startswith("  1-8  %s copilot/fake-model" % first), text)
        self.assertTrue(lines[1].startswith("  9-11  %s vibe/devstral-fake-2512" % second), text)
        self.assertEqual(len(self.records()), 2)
        meta = self.records()[1]["metadata"]["dev.capocantiere"]
        self.assertEqual(len(meta["fingerprints"]["src/app.js"]), 2)  # only what the second job added

    def test_base_includes_untracked_files(self):
        self.write_file(".gitignore", "secret.txt\n")
        self.write_file("notes.txt", "untracked notes\n")
        self.write_file("secret.txt", "ignored\n")
        with open(os.path.join(self.repo, "big.bin"), "wb") as f:
            f.write(b"x" * (6 * 2**20))
        self.write_file("main.txt", "hello, edited\n")
        before = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        jid, text = self.delegate("add a file FAKE:probe PROBE:notes.txt PROBE:.gitignore PROBE:secret.txt PROBE:big.bin",
                                  worker="copilot", fallback=False)
        self.assertIn('PROBE {"notes.txt": true, ".gitignore": true, "secret.txt": false, "big.bin": false}', text)
        self.assertIn("Untracked files over 5 MB left out of the job's base: big.bin.", text)
        self.assertIn("base: HEAD + uncommitted changes", text)
        # only the worker's own change is in the diff, not the files of the base
        self.assertIn("fake-copilot.txt | 1 +", text)
        self.assertNotIn("notes.txt |", text)
        # the user's index, files and stashes are untouched
        after = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(before, after)
        stash = subprocess.run(["git", "stash", "list"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(stash, "")
        self.assertIn("Applied (clean)", self.server.ok("apply", {"job_id": jid}))

    def test_clean_repo_starts_from_head(self):
        jid, text = self.delegate("add a file", worker="copilot", fallback=False)
        self.assertIn("base: HEAD (", text)

    def test_ranges_follow_edits_made_before_apply(self):
        if RUNTIME == "node":
            self.skipTest("attribution is Python-only")
        body = ["const v%d = %d;" % (i, i) for i in range(1, 9)]
        self.write_file("lib.js", "\n".join(body) + "\n")
        self.commit()
        edited = body[:4] + ["const sum = v4 + v5;"] + body[4:]
        jid, _ = self.delegate(self.write_task({"lib.js": "\n".join(edited) + "\n"}), worker="copilot", fallback=False)
        # meanwhile the user adds two lines on top: the patch applies with an offset
        self.write_file("lib.js", "// top one\n// top two\n" + "\n".join(body) + "\n")
        self.assertIn("Applied (clean)", self.server.ok("apply", {"job_id": jid}))
        conv = self.records()[0]["files"][0]["conversations"][0]
        self.assertEqual([(x["start_line"], x["end_line"]) for x in conv["ranges"]], [(7, 7)])

    def test_survival(self):
        if RUNTIME == "node":
            self.skipTest("attribution is Python-only")
        jid = self.apply_job({"src/app.js": self.APP})
        sys.path.insert(0, os.path.dirname(RUN_SH))
        try:
            import attribution
        finally:
            sys.path.pop(0)
        self.assertEqual(attribution.survival(self.repo, jid), (4, 4))
        self.write_file("src/app.js", self.APP.replace("a * b", "b * a"))
        self.assertEqual(attribution.survival(self.repo, jid), (3, 4))
        self.assertIsNone(attribution.survival(self.repo, "j-none"))

    def test_trivial_lines(self):
        sys.path.insert(0, os.path.dirname(RUN_SH))
        try:
            import attribution
        finally:
            sys.path.pop(0)
        for line in ("", "   ", "}", "});", "  ]", "x=1", "fi", "*/"):
            self.assertTrue(attribution.trivial(line), line)
        for line in ("end", "return x;", "pass", "  n += 10"):
            self.assertFalse(attribution.trivial(line), line)
        self.assertEqual(attribution.fingerprint("  return x;\t"), attribution.fingerprint("return x;"))

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

    # ------------------------------------------------------------ isolation hook

    def test_job_commits_are_never_signed(self):
        cfg = os.path.join(self.tmp, "gitconfig")
        with open(cfg, "w") as f:
            f.write("[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = false\n")  # signing would always fail
        self.env["GIT_CONFIG_GLOBAL"] = cfg
        jid, text = self.delegate("add a file", worker="copilot")
        self.assertIn("status: done", text)
        self.assertIn("fake-copilot.txt | 1 +", text)

    def test_isolation_none_is_the_default(self):
        jid, text = self.delegate("add a file", worker="copilot")
        self.assertIn("status: done", text)
        self.assertEqual(os.path.basename(self.calls("copilot")[0]["argv"][0]), "-p")  # run unwrapped

    def test_unknown_isolation_backend_skips_the_worker(self):
        self.write_config(copilot={"isolation": "chroot"})
        jid, text = self.delegate("add a file")
        self.assertIn('copilot skipped: isolation "chroot" is unknown', text)
        self.assertIn("worker: vibe", text)
        self.assertEqual(self.calls("copilot"), [])

    # ------------------------------------------------------------ credentials and isolation backends

    SANDBOX_FAKES = os.path.join(FAKES, "sandbox")

    def use_sandbox_fakes(self, only=None):
        """Put the fake bwrap / xdg-dbus-proxy / sandbox-exec first on PATH. With only, PATH holds nothing else
        than the basic tools and those fakes, so the other sandbox tools look missing."""
        if only is None:
            self.env["PATH"] = self.SANDBOX_FAKES + os.pathsep + self.env["PATH"]
            return
        d = os.path.join(self.tmp, "bin")
        os.makedirs(d)
        for tool in ("sh", "dirname", "python3", "git", "env", "true"):
            os.symlink(shutil.which(tool), os.path.join(d, tool))
        for tool in only:
            os.symlink(os.path.join(self.SANDBOX_FAKES, tool), os.path.join(d, tool))
        self.env["PATH"] = d

    def token_file(self, worker, value, mode=0o600):
        d = os.path.join(self.tmp, "state", "capocantiere", "credentials")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, worker)
        with open(path, "w") as f:
            f.write(value + "\n")
        os.chmod(path, mode)
        return path

    def test_token_is_injected_only_into_its_worker(self):
        self.write_config(copilot={"credentials": "token"})
        self.env["CAPOCANTIERE_COPILOT_TOKEN"] = "gh-fine-grained"
        self.assertIn("authenticated: yes (COPILOT_GITHUB_TOKEN token from CAPOCANTIERE_COPILOT_TOKEN)", self.server.ok("workers"))
        self.delegate("add a file", worker="copilot", fallback=False)
        self.delegate("add a file", worker="vibe", fallback=False)
        copilot, vibe = self.calls("copilot")[0]["env"], self.calls("vibe")[0]["env"]
        self.assertEqual(copilot.get("COPILOT_GITHUB_TOKEN"), "gh-fine-grained")
        self.assertNotIn("CAPOCANTIERE_COPILOT_TOKEN", copilot)
        self.assertNotIn("COPILOT_GITHUB_TOKEN", vibe)
        self.assertNotIn("CAPOCANTIERE_COPILOT_TOKEN", vibe)

    def test_token_file(self):
        self.write_config(vibe={"credentials": "token"})
        self.token_file("vibe", "mistral-minimal")
        self.delegate("add a file", worker="vibe", fallback=False)
        self.assertEqual(self.calls("vibe")[0]["env"].get("MISTRAL_API_KEY"), "mistral-minimal")

    def test_token_problems_skip_the_worker(self):
        self.write_config(copilot={"credentials": "token"})
        jid, text = self.delegate("add a file")
        self.assertIn("copilot skipped: no token: put it in", text)
        self.assertIn('only the "Copilot Requests" permission', text)
        self.assertIn("worker: vibe", text)
        path = self.token_file("copilot", "x", mode=0o644)
        jid, text = self.delegate("add a file")
        self.assertIn("copilot skipped: %s is readable by other users" % path, text)
        self.write_config(copilot={"credentials": "private-home"})
        jid, text = self.delegate("add a file")
        self.assertIn('copilot skipped: credentials "private-home" is not available yet', text)
        self.assertEqual(self.calls("copilot"), [])

    def test_bwrap_with_token_credentials(self):
        self.use_sandbox_fakes()
        self.write_config(copilot={"isolation": "bwrap", "credentials": "token", "isolation_env": ["FAKE_LOG"]})
        self.token_file("copilot", "gh-fine-grained")
        jid, text = self.delegate("add a file", worker="copilot", fallback=False)
        self.assertIn("status: done", text)
        self.assertIn("fake-copilot.txt | 1 +", text)
        bw = self.calls("bwrap")[0]
        opts, wt = bw["argv"], self.calls("copilot")[0]["cwd"]
        joined = " ".join(opts)
        for expected in ("--unshare-all --share-net", "--ro-bind / /", "--tmpfs /tmp", "--bind %s %s" % (wt, wt),
                         "--ro-bind {0} {0}".format(os.path.realpath(os.path.join(self.repo, ".git"))),
                         "--bind {0} {0}".format(os.path.realpath(os.path.join(self.repo, ".git", "objects")))):
            self.assertIn(expected, joined)
        self.assertIn("--tmpfs " + os.path.realpath(os.path.expanduser("~")).rsplit("/", 1)[0], joined)
        self.assertEqual(bw["command"][0], os.path.join(FAKES, "copilot"))
        env = self.calls("copilot")[0]["env"]
        self.assertEqual(env.get("COPILOT_GITHUB_TOKEN"), "gh-fine-grained")
        self.assertNotIn("MISTRAL_API_KEY", env)  # another worker's credential, set in the server's environment
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", env)  # token credentials: no D-Bus at all
        self.assertEqual(self.calls("xdg-dbus-proxy"), [])

    def test_bwrap_with_host_credentials_uses_the_dbus_proxy(self):
        self.use_sandbox_fakes()
        runtime = os.path.join(self.tmp, "run")
        os.makedirs(runtime)
        self.env.update(XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS="unix:path=%s/real-bus" % runtime)
        self.write_config(copilot={"isolation": "auto", "isolation_env": ["FAKE_LOG"]})
        jid, text = self.delegate("add a file", worker="copilot", fallback=False)
        self.assertIn("status: done", text)
        proxy = self.calls("xdg-dbus-proxy")[0]["argv"]
        self.assertEqual(proxy[0], "unix:path=%s/real-bus" % runtime)
        self.assertIn("--filter", proxy)
        self.assertEqual([a for a in proxy if a.startswith("--talk=")],
                         ["--talk=org.freedesktop.secrets", "--talk=org.kde.kwalletd6", "--talk=org.kde.kwalletd5"])
        opts = self.calls("bwrap")[0]["argv"]
        self.assertIn("--tmpfs", opts)
        self.assertIn(runtime, opts)  # the real runtime dir (and its bus) is hidden ...
        i = opts.index(proxy[1])
        self.assertEqual(opts[i - 1:i + 2], ["--bind", proxy[1], os.path.join(runtime, "bus")])  # ... the proxy shows up there
        env = self.calls("copilot")[0]["env"]
        self.assertEqual(env.get("DBUS_SESSION_BUS_ADDRESS"), "unix:path=%s/bus" % runtime)
        self.assertFalse(os.path.exists(os.path.dirname(proxy[1])))  # the proxy's folder is removed after the run

    def test_keyring_names_are_configurable(self):
        self.use_sandbox_fakes()
        runtime = os.path.join(self.tmp, "run")
        os.makedirs(runtime)
        self.env.update(XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent")
        cfg_path = os.path.join(self.tmp, "config", "capocantiere", "workers.json")
        self.write_config(copilot={"isolation": "bwrap", "isolation_env": ["FAKE_LOG"]})
        with open(cfg_path) as f:
            cfg = json.load(f)
        cfg["keyring_dbus_names"] = ["org.keepassxc.KeePassXC.MainWindow"]
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        self.delegate("add a file", worker="copilot", fallback=False)
        self.assertEqual([a for a in self.calls("xdg-dbus-proxy")[0]["argv"] if a.startswith("--talk=")],
                         ["--talk=org.keepassxc.KeePassXC.MainWindow"])

    def test_missing_dbus_proxy_skips_host_credentials(self):
        self.use_sandbox_fakes(only=["bwrap"])
        self.write_config(copilot={"isolation": "bwrap"}, vibe={"isolation": "bwrap", "credentials": "token",
                                                              "isolation_env": ["FAKE_LOG"]})
        self.token_file("vibe", "mistral-minimal")
        jid, text = self.delegate("add a file")
        self.assertIn("copilot skipped: bwrap needs xdg-dbus-proxy, which is not installed (see /capocantiere:doctor)", text)
        self.assertIn("worker: vibe", text)  # token credentials need no D-Bus
        self.assertIn("status: done", text)
        self.assertEqual(self.calls("copilot"), [])
        report = self.server.ok("workers")
        self.assertIn("credentials: host; isolation: bwrap; can read the secrets of your system keyring", report)
        self.assertIn("⛔ unusable: bwrap needs xdg-dbus-proxy", report)
        self.assertIn("credentials: token; isolation: bwrap; sees its own credential", report)

    def test_missing_bwrap_skips_the_worker(self):
        self.use_sandbox_fakes(only=[])
        self.write_config(copilot={"isolation": "bwrap", "credentials": "token"})
        self.token_file("copilot", "x")
        jid, text = self.delegate("add a file")
        self.assertIn("copilot skipped: bwrap needs bwrap, which is not installed", text)
        self.assertIn("worker: vibe", text)

    def test_unusable_bwrap_skips_the_worker(self):
        self.use_sandbox_fakes()
        self.env["FAKE_BWRAP_FAIL"] = "1"
        self.write_config(copilot={"isolation": "bwrap", "credentials": "token"})
        self.token_file("copilot", "x")
        jid, text = self.delegate("add a file")
        self.assertIn("copilot skipped: bwrap cannot create a sandbox here: bwrap: setting up uid map", text)
        self.assertIn("worker: vibe", text)

    def test_isolation_auto_on_windows_skips(self):
        self.env["CAPOCANTIERE_PLATFORM"] = "win32"
        self.write_config(copilot={"isolation": "auto"})
        jid, text = self.delegate("add a file")
        self.assertIn('copilot skipped: isolation "auto": no isolation backend exists for win32 yet (run the workers '
                      'inside WSL2', text)
        self.assertIn("worker: vibe", text)

    def test_backend_of_another_system_skips(self):
        self.write_config(copilot={"isolation": "seatbelt"})
        jid, text = self.delegate("add a file")
        self.assertIn('copilot skipped: isolation "seatbelt" does not work on linux', text)

    def test_seatbelt_on_macos(self):
        self.use_sandbox_fakes()
        self.env["CAPOCANTIERE_PLATFORM"] = "darwin"
        self.write_config(copilot={"isolation": "auto", "isolation_env": ["FAKE_LOG"]})
        self.assertIn("seatbelt (EXPERIMENTAL, untested)", self.server.ok("workers"))
        jid, text = self.delegate("add a file", worker="copilot", fallback=False)
        self.assertIn("status: done", text)
        sb = self.calls("sandbox-exec")[0]
        wt = os.path.realpath(self.calls("copilot")[0]["cwd"])
        profile = sb["profile"]
        self.assertTrue(profile.startswith("(version 1)\n(deny default)"))
        self.assertIn('(subpath "%s")' % wt, profile.split("(allow file-write*", 1)[1].split("\n", 1)[0])
        self.assertIn('(deny file-read* (subpath "/Users")', profile)
        self.assertIn('(global-name "com.apple.SecurityServer")', profile)
        self.assertNotIn("launchservicesd", profile)
        self.assertNotIn("appleevents", profile.lower())
        self.assertEqual(sb["argv"][0], os.path.join(FAKES, "copilot"))

    def test_seatbelt_missing(self):
        self.use_sandbox_fakes(only=[])
        self.env["CAPOCANTIERE_PLATFORM"] = "darwin"
        self.write_config(copilot={"isolation": "seatbelt"})
        jid, text = self.delegate("add a file")
        self.assertIn("copilot skipped: seatbelt needs sandbox-exec, which is not installed", text)

    def test_doctor(self):
        self.use_sandbox_fakes(only=["bwrap"])
        self.write_config(copilot={"isolation": "auto"}, vibe={"isolation": "auto", "credentials": "token"})
        self.token_file("vibe", "x")
        text = self.server.ok("workers", {"doctor": True})
        self.assertIn("System: Linux", text)
        self.assertIn("xdg-dbus-proxy: MISSING", text)
        self.assertIn("bwrap sandbox test: ok", text)
        self.assertRegex(text, r"To install: .*xdg-dbus-proxy")
        self.assertIn("isolation: auto — auto (bwrap) needs xdg-dbus-proxy", text)  # copilot: host credentials
        self.assertIn('only the "Copilot Requests" permission', text)  # ... so the token is suggested
        self.assertIn("credentials: token — ok: MISTRAL_API_KEY token from", text)
        self.assertIn("isolation: auto → bwrap; sees its own credential", text)
        self.assertIn("Nothing was installed or changed", text)

    def test_doctor_windows(self):
        self.env["CAPOCANTIERE_PLATFORM"] = "win32"
        self.assertIn("run the workers inside WSL2", self.server.ok("workers", {"doctor": True}))

    @unittest.skipUnless(shutil.which("bwrap") and subprocess.run(
        ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--", "true"], capture_output=True).returncode == 0,
        "bubblewrap is not usable on this system")
    def test_real_bwrap(self):
        """The real bubblewrap around a fake worker: the job works, the rest of the disk is hidden."""
        decoy = os.path.join(self.tmp, "decoy-secret")
        with open(decoy, "w") as f:
            f.write("secret\n")
        open(self.log, "w").close()
        self.write_config(copilot={"isolation": "bwrap", "credentials": "token", "isolation_env": ["FAKE_LOG"],
                                   "isolation_rw": [self.log]})
        self.token_file("copilot", "gh-fine-grained")
        main = os.path.join(self.repo, "main.txt")
        jid, text = self.delegate("add a file FAKE:probe PROBE:%s PROBE:%s" % (decoy, main), worker="copilot",
                                  fallback=False)
        self.assertIn("status: done", text)
        self.assertIn("fake-copilot.txt | 1 +", text)
        self.assertIn('PROBE {"%s": false, "%s": false}' % (decoy, main), text)
        self.assertEqual(self.calls("copilot")[0]["env"].get("COPILOT_GITHUB_TOKEN"), "gh-fine-grained")

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
