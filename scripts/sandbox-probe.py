#!/usr/bin/env python3
"""Check the isolation backends of the capocantiere server on this machine, with the real worker CLIs.

    python3 scripts/sandbox-probe.py                  # offline checks only (no model calls)
    python3 scripts/sandbox-probe.py --prompt         # also one tiny prompt per worker and variant, after confirmation
    python3 scripts/sandbox-probe.py --workers vibe   # only some workers

It builds the sandboxes with the server's own code (plugins/capocantiere/server/isolation.py), so what it checks is
what the server runs. Variants:

  Linux
    none        no isolation: the baseline, which shows that the checks do detect leaks
    bwrap-token bwrap with "token" credentials: no D-Bus at all
    bwrap-host  bwrap with "host" credentials: the session D-Bus through xdg-dbus-proxy, keyring names only
  macOS (EXPERIMENTAL backend: this probe is how it gets tested)
    none        baseline
    seatbelt    sandbox-exec with the profile the server generates ("host" credentials: Keychain reachable)

Real calls (--prompt) send "Reply with exactly: ok" with no tools: for copilot that is 1 premium request each.
bwrap-token runs a prompt only for workers with a token configured (see `workers` doctor=true).
Everything the probe creates is removed at the end: a repo and worktree under ~/.cache, a decoy file in the home
folder, a decoy environment variable, and on macOS a decoy Keychain item. Standard library only.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugins", "capocantiere", "server"))
import isolation  # noqa: E402
import workers as W  # noqa: E402

HOME = os.path.expanduser("~")
SYSTEM = isolation.system()
RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR") or ""
KEYCHAIN_SERVICE = "capocantiere-probe"

PROMPTS = {
    "copilot": (["-p", "Reply with exactly: ok", "-s", "--no-ask-user", "--no-auto-update", "--no-color",
                 "--disable-builtin-mcps"], "1 premium request"),
    "vibe": (["--prompt", "Reply with exactly: ok", "--output", "text", "--max-turns", "1", "--enabled-tools", "read_file"],
             "free on the Free plan (a few thousand tokens)"),
}

# Runs inside each variant; prints one JSON object. argv: system, home, runtime_dir, worktree, main_repo, secret_file
CHECKS = r'''
import json, os, socket, ssl, subprocess, sys, urllib.request
system, home, runtime_dir, wt, main_repo, secret_file = sys.argv[1:7]
r = {}
def readable(p):
    try:
        if os.path.isdir(p):
            os.listdir(p)
        else:
            open(p, "rb").read(1)
        return True
    except OSError:
        return False
def cmd(*a):
    try:
        p = subprocess.run(list(a), capture_output=True, text=True, timeout=20)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
r["secret_file_readable"] = readable(secret_file)
r["ssh_readable"] = readable(os.path.join(home, ".ssh"))
r["claude_readable"] = readable(os.path.join(home, ".claude"))
r["env_secret_visible"] = "CAPO_PROBE_SECRET" in os.environ
# The main repo's working tree must be invisible; a write may land in a tmpfs that hides it,
# so the probe also checks the real file afterwards (main_repo_modified).
r["main_repo_readable"] = readable(os.path.join(main_repo, "main.txt"))
try:
    with open(os.path.join(main_repo, "main.txt"), "a") as f:
        f.write("written from the sandbox\n")
except OSError:
    pass
if system == "linux":
    r["dbus_socket_visible"] = os.path.exists(os.path.join(runtime_dir, "bus"))
    r["systemd_private_visible"] = os.path.exists(os.path.join(runtime_dir, "systemd", "private"))
    c, out = cmd("systemd-run", "--user", "--quiet", "--collect", "true")
    r["systemd_run_user"] = c == 0
    r["systemd_run_note"] = out[:120]
    c, out = cmd("busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames")
    names = sorted(n for n in out.replace('"', " ").split() if "." in n and not n.startswith(":")) if c == 0 else []
    r["dbus_other_services"] = [n for n in names if n != "org.freedesktop.DBus" and not n.startswith(
        ("org.freedesktop.secrets", "org.kde.kwalletd"))]
    c, out = cmd("busctl", "--user", "introspect", "org.freedesktop.secrets", "/org/freedesktop/secrets")
    r["keyring_reachable"] = c == 0
else:
    c, out = cmd("security", "find-generic-password", "-s", "capocantiere-probe", "-w")
    r["keychain_decoy_readable"] = c == 0
    c, out = cmd("launchctl", "print", "gui/%d" % os.getuid())
    r["launchctl_gui_domain"] = c == 0
    c, out = cmd("open", "-g", "-j", "-R", "/System/Applications")
    r["open_launchservices"] = c == 0
try:
    socket.getaddrinfo("api.github.com", 443)
    socket.getaddrinfo("api.mistral.ai", 443)
    r["dns"] = True
except OSError as e:
    r["dns"] = False
    r["dns_error"] = str(e)[:120]
try:
    urllib.request.urlopen(urllib.request.Request("https://api.github.com", method="HEAD"), timeout=10, context=ssl.create_default_context())
    r["tls"] = True
except urllib.error.HTTPError:
    r["tls"] = True  # got an HTTP answer over TLS
except Exception as e:
    r["tls"] = False
    r["tls_error"] = str(e)[:120]
def git(*a):
    p = subprocess.run(["git", *a], cwd=wt, capture_output=True, text=True)
    return p.returncode, (p.stderr or p.stdout).strip()[:160]
c, out = git("status", "--porcelain")
r["git_status"] = c == 0
if c:
    r["git_status_error"] = out
try:
    with open(os.path.join(wt, "probe-edit.txt"), "w") as f:
        f.write("edited inside the sandbox\n")
    r["worktree_writable"] = True
except OSError:
    r["worktree_writable"] = False
c, out = git("add", "probe-edit.txt")
r["git_add"] = c == 0
if c:
    r["git_add_error"] = out
c, out = git("update-ref", "refs/heads/probe-moved", "HEAD")
r["git_refs_writable"] = c == 0
print(json.dumps(r))
'''


def run(argv, env=None, timeout=120, cwd=None):
    t = time.time()
    try:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr, time.time() - t
    except FileNotFoundError as e:
        return 127, "", str(e), time.time() - t
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout, time.time() - t


class Layout:
    """A main repo and a job worktree laid out like the server does, plus the decoys."""

    def __init__(self):
        os.makedirs(os.path.join(HOME, ".cache"), exist_ok=True)
        self.base = tempfile.mkdtemp(prefix="capocantiere-probe-", dir=os.path.join(HOME, ".cache"))
        self.repo = os.path.join(self.base, "repo")
        self.wt = os.path.join(self.base, "worktrees", "repo-probe")
        os.makedirs(self.repo)
        g = lambda *a, cwd=self.repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
        with open(os.path.join(self.repo, "main.txt"), "w") as f:
            f.write("hello\n")
        g("init", "-q")
        g("add", ".")
        g("-c", "user.name=probe", "-c", "user.email=probe@localhost", "-c", "commit.gpgsign=false", "commit", "-qm", "init")
        g("worktree", "add", "-q", "-b", "capocantiere/probe", self.wt)
        self.secret_file = os.path.join(HOME, ".capocantiere-probe-secret")
        with open(self.secret_file, "w") as f:
            f.write(secrets.token_hex(16) + "\n")
        os.chmod(self.secret_file, 0o600)
        self.env_secret = secrets.token_hex(16)
        self.keychain = False
        if SYSTEM == "darwin":
            self.keychain = run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", "probe",
                                 "-w", secrets.token_hex(8)])[0] == 0

    def reset(self):
        subprocess.run(["git", "reset", "-q", "--hard"], cwd=self.wt, capture_output=True)
        subprocess.run(["git", "clean", "-qfd"], cwd=self.wt, capture_output=True)
        subprocess.run(["git", "update-ref", "-d", "refs/heads/probe-moved"], cwd=self.repo, capture_output=True)

    def server_commit(self):
        """What the server does after a run: stage and commit outside the sandbox."""
        a = subprocess.run(["git", "add", "-A"], cwd=self.wt, capture_output=True, text=True)
        c = subprocess.run(["git", "-c", "user.name=probe", "-c", "user.email=probe@localhost", "-c", "commit.gpgsign=false",
                            "commit", "-qm", "probe"], cwd=self.wt, capture_output=True, text=True)
        return a.returncode == 0 and c.returncode in (0, 1)

    def cleanup(self):
        subprocess.run(["git", "worktree", "remove", "--force", self.wt], cwd=self.repo, capture_output=True)
        shutil.rmtree(self.base, ignore_errors=True)
        try:
            os.remove(self.secret_file)
        except OSError:
            pass
        if self.keychain:
            run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", "probe"])


def variants():
    if SYSTEM == "linux":
        return [("none", "none", "host"), ("bwrap-token", "bwrap", "token"), ("bwrap-host", "bwrap", "host")]
    if SYSTEM == "darwin":
        return [("none", "none", "host"), ("seatbelt", "seatbelt", "host")]
    return [("none", "none", "host")]


def request(layout, worker, args, credentials):
    """The isolation request the server would build for this worker, running args in the job's worktree."""
    if worker:
        w = W.get_worker(worker)
        w.s = dict(w.s, credentials=credentials)
        req = w.isolation_request({"cwd": layout.wt, "auto_approve": True}, {"argv": args, "cleanup": []})
        req["cmd"] = shutil.which(w.bin) or w.bin
    else:
        req = {"cmd": sys.executable, "args": args, "cwd": layout.wt, "writable": True, "env": {}, "pass_env": [],
               "rw": [], "ro": [], "credentials": credentials, "bin_path": sys.executable,
               "keyring_names": W.load_config().get("keyring_dbus_names")}
    return req


def launch(layout, setting, req, timeout=120):
    """Run a request through isolation.prepare, with the decoy variable in the server's environment."""
    os.environ["CAPO_PROBE_SECRET"] = layout.env_secret
    try:
        try:
            prep = isolation.prepare(setting, req)
        except Exception as e:
            return 125, "", "isolation.prepare failed: %s" % e, 0
        try:
            env = prep.env if prep.env is not None else dict(os.environ, **(prep.extra_env or {}))
            return run([prep.cmd] + prep.args, env=env, timeout=timeout, cwd=req["cwd"])
        finally:
            prep.close()
    finally:
        os.environ.pop("CAPO_PROBE_SECRET", None)


def proxy_log_summary(path):
    """Destinations the sandboxed process tried to reach through the D-Bus proxy, and which were blocked."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return {}
    reached, blocked = set(), set()
    for i, l in enumerate(lines):
        m = re.match(r"C\d+: -> (\S+) call (\S+)", l)
        if not m or m.group(1) == "org.freedesktop.DBus":
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        (blocked if nxt.startswith(("*HIDDEN*", "*DENIED*")) else reached).add(m.group(1))
    return {"dbus_reached": sorted(reached), "dbus_blocked": sorted(blocked - reached)}


def confirm(question, assume_yes):
    if assume_yes:
        return True
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def show(v, good):
    """good: the safe/working value, or None when the value is just informative."""
    if v is None:
        return "—"
    if isinstance(v, list):
        return ", ".join(v) or "none"
    if not isinstance(v, bool):
        return str(v)
    if good is None:
        return "yes" if v else "no"
    return "ok" if v == good else ("LEAK" if good is False else "FAIL")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prompt", action="store_true", help="also send one tiny real prompt per worker and variant")
    ap.add_argument("--yes", action="store_true", help="don't ask before each real call (only if already agreed)")
    ap.add_argument("--workers", default="copilot,vibe")
    ap.add_argument("--variants", help="comma-separated subset of: " + ", ".join(v[0] for v in variants()))
    ap.add_argument("--json", help="also write the raw results to this file")
    args = ap.parse_args()
    workers = [w for w in args.workers.split(",") if w in PROMPTS and shutil.which(W.get_worker(w).bin)]

    print("system:", SYSTEM)
    if SYSTEM == "linux":
        for p in ("/proc/sys/kernel/unprivileged_userns_clone", "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"):
            print(os.path.basename(p), "=", open(p).read().strip() if os.path.exists(p) else "(absent)")
        print("bwrap:", run(["bwrap", "--version"])[1].strip() or "not installed")
        print("xdg-dbus-proxy:", shutil.which("xdg-dbus-proxy") or "not installed")
    print("workers:", ", ".join(workers) or "none installed")
    print()

    layout = Layout()
    logdir = tempfile.mkdtemp(prefix="capocantiere-probe-log-")
    results = []
    try:
        wanted = set(args.variants.split(",")) if args.variants else None
        for name, setting, creds in variants():
            if wanted is not None and name not in wanted:
                continue
            problem = isolation.problem(setting, creds)
            if problem:
                results.append({"variant": name, "skipped": problem})
                continue
            isolation.DbusProxy.log_file = os.path.join(logdir, name + "-checks.log")
            layout.reset()
            req = request(layout, None, ["-I", "-c", CHECKS, SYSTEM, HOME, RUNTIME_DIR, layout.wt, layout.repo,
                                         layout.secret_file], creds)
            code, out, err, secs = launch(layout, setting, req)
            row = {"variant": name}
            try:
                row.update(json.loads(out.strip().splitlines()[-1]))
            except (ValueError, IndexError):
                row["start_error"] = (err or out).strip()[:300] or "exit %s" % code
            with open(os.path.join(layout.repo, "main.txt")) as f:
                row["main_repo_modified"] = f.read() != "hello\n"
            subprocess.run(["git", "checkout", "-q", "--", "main.txt"], cwd=layout.repo, capture_output=True)
            row["server_commit_after"] = layout.server_commit()
            results.append(row)
            for w in workers:
                wrow = {"variant": name, "worker": w}
                isolation.DbusProxy.log_file = os.path.join(logdir, "%s-%s.log" % (name, w))
                code, out, err, secs = launch(layout, setting, request(layout, w, ["--version"], creds))
                wrow["version"] = code == 0
                wrow["version_s"] = round(secs, 2)
                if code:
                    wrow["version_error"] = (err or out).strip()[:200]
                if args.prompt and code == 0:
                    if creds == "token" and not W.get_worker(w).token()[0]:
                        wrow["prompt_skipped"] = "no token configured"
                    elif confirm("Send one tiny prompt to %s in variant %s (%s)?" % (w, name, PROMPTS[w][1]), args.yes):
                        code, out, err, secs = launch(layout, setting, request(layout, w, PROMPTS[w][0], creds), timeout=180)
                        wrow["prompt"] = code == 0 and "ok" in out.lower()
                        wrow["prompt_s"] = round(secs, 1)
                        if not wrow["prompt"]:
                            wrow["prompt_error"] = ("exit %s: " % code + (err or out).strip())[:300]
                wrow.update(proxy_log_summary(isolation.DbusProxy.log_file))
                results.append(wrow)
    finally:
        isolation.DbusProxy.log_file = None
        layout.cleanup()
        shutil.rmtree(logdir, ignore_errors=True)

    # ---- report
    print("Offline checks (LEAK = let through; in 'none' it shows that the checks work)")
    cols = [("secret_file_readable", False), ("ssh_readable", False), ("claude_readable", False),
            ("env_secret_visible", False), ("main_repo_readable", False), ("main_repo_modified", False)]
    if SYSTEM == "linux":
        cols += [("dbus_socket_visible", None), ("systemd_private_visible", False), ("systemd_run_user", False),
                 ("dbus_other_services", None), ("keyring_reachable", None)]
    elif SYSTEM == "darwin":
        cols += [("keychain_decoy_readable", None), ("launchctl_gui_domain", None), ("open_launchservices", False)]
    cols += [("dns", True), ("tls", True), ("worktree_writable", True), ("git_status", True), ("git_add", True),
             ("git_refs_writable", False), ("server_commit_after", True)]
    for r in results:
        if "worker" in r:
            continue
        print("\n[%s]" % r["variant"])
        if r.get("skipped"):
            print("  skipped: " + r["skipped"])
            continue
        if r.get("start_error"):
            print("  did not start: " + r["start_error"])
            continue
        for c, good in cols:
            print("  %-26s %s" % (c, show(r.get(c), good)))
        for k in ("systemd_run_note", "dns_error", "tls_error", "git_status_error", "git_add_error"):
            if r.get(k):
                print("  %s: %s" % (k, r[k]))
    print()
    print("Workers")
    print("| variant | worker | --version | time (s) | prompt | prompt time (s) | D-Bus reached | D-Bus blocked |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        if "worker" not in r:
            continue
        prompt = show(r.get("prompt"), True) if "prompt" in r else r.get("prompt_skipped") or "not sent"
        print("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["variant"], r["worker"], show(r.get("version"), True), r.get("version_s", "—"), prompt,
            r.get("prompt_s", "—"), ", ".join(r.get("dbus_reached") or []) or "—",
            ", ".join(r.get("dbus_blocked") or []) or "—"))
    for r in results:
        for k in ("version_error", "prompt_error"):
            if r.get(k):
                print("  %s/%s %s: %s" % (r["variant"], r["worker"], k, r[k]))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()
