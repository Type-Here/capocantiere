# Isolation of worker processes: the hook through which every worker run goes.
#
# Setting "isolation" (global, and per worker to override it):
#   "none"      runs are not isolated (the default)
#   "auto"      the backend of the current system: bwrap on Linux, seatbelt on macOS; none exists on Windows
#   "bwrap"     Linux, bubblewrap (https://github.com/containers/bubblewrap)
#   "seatbelt"  macOS, sandbox-exec with a profile generated per run. EXPERIMENTAL: not tested by the authors.
# It is separate from the "sandbox" option some CLIs have of their own (copilot --sandbox, agy --sandbox).
#
# When isolation is requested and the backend cannot work (missing tool, wrong system), the worker is skipped
# with a note: a run never starts unisolated when isolation was asked for.
#
# What a sandboxed run sees:
#   - the system read-only; the user's home folder and other users' data hidden
#   - the job's worktree read-write (an analysis's repo read-only); of the main repo's .git only what git needs:
#     read-only, except this worktree's admin dir and the object store (refs stay read-only)
#   - the worker CLI's own config/auth folders read-write, its install folder read-only
#   - the network (the model APIs need it)
#   - an environment rebuilt from scratch: only what the CLI needs
#   - credentials: "host" (the CLI's own login, in the system keyring) reaches the keyring only. On Linux through
#     xdg-dbus-proxy, which lets through the keyring services and nothing else of the session D-Bus; with
#     "token" there is no D-Bus at all. On macOS the Keychain is reachable (per-item access control applies).
# Standard library only.

import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

BACKENDS = ("bwrap", "seatbelt")
SETTINGS = ("none", "auto") + BACKENDS
NATIVE = {"linux": "bwrap", "darwin": "seatbelt"}
EXPERIMENTAL = {"seatbelt"}

# Session D-Bus names that hold the system keyring, let through with "host" credentials (setting "keyring_dbus_names").
# Secret Service (GNOME Keyring, KeePassXC, KDE Wallet 6) and KDE Wallet's own API (used by some keyring libraries).
DEFAULT_KEYRING_NAMES = ["org.freedesktop.secrets", "org.kde.kwalletd6", "org.kde.kwalletd5"]

# Environment variables kept in every sandboxed run (prefixes end with "_").
BASE_ENV = ["PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "LANG", "LANGUAGE", "LC_",
            "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"]
# Kept only with "host" credentials: keyring libraries pick their backend from the desktop session.
DESKTOP_ENV = ["XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION", "KDE_FULL_SESSION",
               "KDE_SESSION_VERSION", "GNOME_KEYRING_CONTROL"]

SEATBELT_BIN = "/usr/bin/sandbox-exec"


def seatbelt_bin():
    return SEATBELT_BIN if os.path.exists(SEATBELT_BIN) else shutil.which("sandbox-exec")

# macOS services the CLIs need (DNS, TLS trust, Keychain, preferences, logging, notifications), and nothing that
# starts processes outside the sandbox (launchd's job submission, LaunchServices, Apple Events).
SEATBELT_MACH = [
    "com.apple.SecurityServer", "com.apple.securityd.xpc", "com.apple.security.syspolicy", "com.apple.trustd",
    "com.apple.trustd.agent", "com.apple.ocspd", "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership", "com.apple.system.DirectoryService.libinfo_v1",
    "com.apple.dnssd.service", "com.apple.mDNSResponder", "com.apple.networkd", "com.apple.nehelper",
    "com.apple.symptomsd", "com.apple.usymptomsd", "com.apple.cfprefsd.daemon", "com.apple.cfprefsd.agent",
    "com.apple.system.notification_center", "com.apple.system.logger", "com.apple.logd", "com.apple.diagnosticd",
    "com.apple.analyticsd", "com.apple.CoreServices.coreservicesd", "com.apple.lsd.mapdb", "com.apple.FSEvents",
    "com.apple.SystemConfiguration.configd", "com.apple.SystemConfiguration.DNSConfiguration", "com.apple.distributed_notifications@Uv3",
    "com.apple.bsd.dirhelper", "com.apple.coreservices.quarantine-resolver", "com.apple.fonts",
]


def system():
    """linux, darwin, win32, ... CAPOCANTIERE_PLATFORM overrides it (for tests)."""
    p = os.environ.get("CAPOCANTIERE_PLATFORM") or sys.platform
    return "linux" if p.startswith("linux") else p


def resolve(setting):
    """The backend to use for a setting: None for no isolation. Raises ValueError if it cannot be resolved."""
    s = (setting or "none").strip()
    if s == "none":
        return None
    if s == "auto":
        b = NATIVE.get(system())
        if not b:
            raise ValueError('isolation "auto": no isolation backend exists for %s yet%s' % (
                system(), " (run the workers inside WSL2 to use the Linux one)" if system() == "win32" else ""))
        return b
    if s not in BACKENDS:
        raise ValueError('isolation "%s" is unknown (use one of: %s)' % (s, ", ".join(SETTINGS)))
    if NATIVE.get(system()) != s:
        raise ValueError('isolation "%s" does not work on %s' % (s, system()))
    return s


_check_cache = {}  # (backend, tool path) -> (checked_at, problem)


def _which(name):
    return shutil.which(name)


def _bwrap_works(path):
    """Unprivileged user namespaces can be disabled (sysctl) or restricted (Ubuntu's AppArmor policy)."""
    key = ("bwrap", path)
    hit = _check_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        p = subprocess.run([path, "--unshare-all", "--ro-bind", "/", "/", "--", "true"],
                           capture_output=True, text=True, timeout=15)
        problem = None if p.returncode == 0 else "bwrap cannot create a sandbox here: %s" % (
            (p.stderr or p.stdout).strip()[:200] or "exit %s" % p.returncode)
    except (OSError, subprocess.TimeoutExpired) as e:
        problem = "bwrap cannot create a sandbox here: %s" % e
    _check_cache[key] = (time.time(), problem)
    return problem


def missing(setting, credentials="host"):
    """What the backend needs and the system lacks: a list of (tool, why). Empty when nothing is missing."""
    try:
        b = resolve(setting)
    except ValueError:
        return []
    out = []
    if b == "bwrap":
        if not _which("bwrap"):
            out.append(("bwrap", "isolation"))
        if credentials == "host" and not _which("xdg-dbus-proxy"):
            out.append(("xdg-dbus-proxy", 'the keyring with "host" credentials'))
    elif b == "seatbelt" and not seatbelt_bin():
        out.append(("sandbox-exec", "isolation"))
    return out


def problem(setting, credentials="host"):
    """Why runs cannot be isolated as configured (the worker is then skipped), or None."""
    try:
        b = resolve(setting)
    except ValueError as e:
        return str(e)
    if b is None:
        return None
    lacks = missing(setting, credentials)
    if lacks:
        return "%s needs %s, which is not installed (see /capocantiere:doctor)" % (
            setting if setting == b else "%s (%s)" % (setting, b), " and ".join(t for t, _ in lacks))
    if b == "bwrap":
        if credentials == "host":
            addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or ""
            if not addr:
                return ('isolation with "host" credentials needs the session D-Bus to reach the keyring, and '
                        'DBUS_SESSION_BUS_ADDRESS is not set: use "token" credentials')
        return _bwrap_works(_which("bwrap"))
    return None


# ---------------------------------------------------------------- preparing a run


class Prepared:
    """What to actually run: cmd, args, and env (the full environment, or None to inherit plus extra_env)."""

    def __init__(self, cmd, args, env=None, extra_env=None, cleanup=None):
        self.cmd, self.args, self.env, self.extra_env = cmd, args, env, extra_env
        self._cleanup = cleanup or []

    def close(self):
        for f in reversed(self._cleanup):
            try:
                f()
            except Exception:
                pass
        self._cleanup = []


def prepare(setting, req):
    """Wrap one worker run. req keys:
        cmd, args, cwd            the worker command and its working directory
        writable                  True if cwd may be written (a job's worktree), False for an analysis
        env                       variables to add (the worker's env setting, injected token)
        pass_env                  names (or prefixes ending in "_") to keep from the server's environment
        rw, ro                    extra paths (worker config/auth folders, per-run temp files, user settings)
        credentials               "host" or "token"
        keyring_names             D-Bus names reaching the keyring ("host" credentials, Linux)
        bin_path                  the resolved worker binary (its install folder is made visible)
    Returns a Prepared; call close() after the run.
    """
    b = resolve(setting)
    if b is None:
        return Prepared(req["cmd"], list(req["args"]), extra_env=req.get("env") or None)
    p = problem(setting, req.get("credentials") or "host")
    if p:
        raise RuntimeError(p)
    return (_bwrap if b == "bwrap" else _seatbelt)(req)


def _keep_env(req, extra_names=()):
    names = list(BASE_ENV) + list(req.get("pass_env") or []) + list(extra_names)
    out = {}
    for k, v in os.environ.items():
        if re.match(r"^CAPOCANTIERE_.*_TOKEN$", k):
            continue
        if any(k.startswith(n) if n.endswith("_") else k == n for n in names):
            out[k] = v
    out.update(req.get("env") or {})
    return out


def _real(p):
    return os.path.realpath(os.path.expanduser(p))


def install_dirs(path):
    """Read-only folders a worker binary needs: its location, its real location and, for a script, its interpreter's
    installation (e.g. a uv/pipx venv and the Python it points to)."""
    if not path:
        return []
    dirs = {os.path.dirname(os.path.abspath(path)), os.path.dirname(_real(path))}
    try:
        with open(_real(path), "rb") as f:
            first = f.readline(512)
        if first.startswith(b"#!"):
            interp = first[2:].split()[0].decode()
            if os.path.basename(interp) != "env":
                dirs.add(os.path.dirname(os.path.dirname(interp)))  # the venv (or prefix)
                dirs.add(os.path.dirname(os.path.dirname(_real(interp))))  # the Python it links to
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    return sorted(d for d in dirs if d and d != "/")


def git_paths(cwd):
    """For a git worktree: (admin dir of this worktree, common .git dir). (None, None) for a normal repo or no repo."""
    dotgit = os.path.join(cwd, ".git")
    if not os.path.isfile(dotgit):
        return None, None
    try:
        with open(dotgit, encoding="utf-8") as f:
            m = re.match(r"gitdir:\s*(.+)", f.read().strip())
        admin = _real(os.path.join(cwd, m.group(1))) if m else None
        with open(os.path.join(admin, "commondir"), encoding="utf-8") as f:
            common = _real(os.path.join(admin, f.read().strip()))
        return admin, common
    except (OSError, AttributeError, TypeError):
        return None, None


def _runtime_dir():
    d = os.environ.get("XDG_RUNTIME_DIR")
    return d if d and os.path.isdir(d) else None


class DbusProxy:
    """xdg-dbus-proxy filtering the session bus down to a few names. The proxy lives until close()."""

    log_file = None  # set by scripts/sandbox-probe.py: the proxy then logs every message (and what it blocked)

    def __init__(self, names):
        addr = os.environ["DBUS_SESSION_BUS_ADDRESS"]
        self.dir = tempfile.mkdtemp(prefix="capocantiere-dbus-", dir=_runtime_dir())
        self.path = os.path.join(self.dir, "bus")
        out = open(self.log_file, "a") if self.log_file else subprocess.DEVNULL
        r, w = os.pipe()
        try:
            self.proc = subprocess.Popen(
                [_which("xdg-dbus-proxy"), addr, self.path, "--filter", "--fd=%d" % w]
                + (["--log"] if self.log_file else []) + ["--talk=%s" % n for n in names],
                pass_fds=(w,), stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True)
        finally:
            os.close(w)
            if self.log_file:
                out.close()
        self.sync = r
        # the proxy writes one byte when its socket is ready, and exits when the other end of the pipe is closed
        if not select.select([r], [], [], 10)[0] or not os.read(r, 1):
            self.close()
            raise RuntimeError("xdg-dbus-proxy did not start")

    def close(self):
        if self.sync is not None:
            os.close(self.sync)
            self.sync = None
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        except AttributeError:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def _bwrap(req):
    home = _real("~")
    runtime = _runtime_dir()
    hidden = ["/home", "/root", "/mnt", "/media", "/run/media", "/srv", "/var/tmp", os.path.dirname(home), home]
    if runtime:
        hidden.append(runtime)
    a = ["--die-with-parent", "--unshare-all", "--share-net", "--new-session",
         "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    seen = set()
    for d in sorted({_real(d) for d in hidden if os.path.isdir(d)}, key=len):
        if d != "/" and not any(d == x or d.startswith(x + "/") for x in seen):
            a += ["--tmpfs", d]
            seen.add(d)

    binds = []  # (path, writable)
    for d in install_dirs(req.get("bin_path")):
        binds.append((d, False))
    for d in req.get("ro") or []:
        binds.append((_real(d), False))
    cwd = _real(req["cwd"])
    binds.append((cwd, bool(req.get("writable"))))
    admin, common = git_paths(cwd)
    if common:
        binds += [(common, False), (os.path.join(common, "objects"), True)]
        if admin:
            binds.append((admin, bool(req.get("writable"))))
    for d in req.get("rw") or []:
        binds.append((_real(d), True))
    tmp = tempfile.mkdtemp(prefix="capocantiere-run-")
    binds.append((tmp, True))
    # parents before children, so a writable folder inside a read-only one stays writable
    for path, rw in sorted(binds, key=lambda b: (len(b[0]), b[1])):
        if os.path.exists(path):
            a += ["--bind" if rw else "--ro-bind", path, path]

    env = _keep_env(req, DESKTOP_ENV if req.get("credentials") == "host" else ())
    env["TMPDIR"] = tmp
    cleanup = [lambda: shutil.rmtree(tmp, ignore_errors=True)]
    if runtime:
        env["XDG_RUNTIME_DIR"] = runtime
    if req.get("credentials") == "host":
        proxy = DbusProxy(req.get("keyring_names") or DEFAULT_KEYRING_NAMES)
        cleanup.append(proxy.close)
        bus = os.path.join(runtime or "/tmp", "bus")
        a += ["--bind", proxy.path, bus]
        env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + bus
    a += ["--chdir", req["cwd"], "--", req["cmd"]] + list(req["args"])
    return Prepared(_which("bwrap"), a, env=env, cleanup=cleanup)


# ---------------------------------------------------------------- macOS seatbelt (experimental)


def _sb_str(s):
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def seatbelt_profile(req, tmp):
    """The SBPL profile for one run. Later rules win over earlier ones."""
    home = _real("~")
    cwd = _real(req["cwd"])
    admin, common = git_paths(cwd)
    readable = [cwd] + install_dirs(req.get("bin_path")) + [_real(d) for d in req.get("ro") or []]
    writable = [tmp] + [_real(d) for d in req.get("rw") or []]
    if req.get("writable"):
        writable.append(cwd)
    if common:
        readable.append(common)
        writable.append(os.path.join(common, "objects"))
        if admin and req.get("writable"):
            writable.append(admin)
    if req.get("credentials") == "host":
        writable.append(os.path.join(home, "Library", "Keychains"))  # the login keychain (per-item access control)
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow process-exec)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow iokit-get-properties)",
        "(allow ipc-posix-shm-read-data ipc-posix-shm-write-data ipc-posix-shm-write-create)",
        "(allow ipc-posix-sem)",
        "(allow pseudo-tty)",
        "(allow file-ioctl)",
        "(allow network*)",
        "(allow system-socket)",
        "; the system is readable; home folders and other volumes are not",
        "(allow file-read*)",
        "(deny file-read* (subpath \"/Users\") (subpath \"/Volumes\") (subpath %s))" % _sb_str(home),
        "(allow file-read-metadata (literal \"/Users\") (literal %s))" % _sb_str(home),
        "(allow file-read* %s)" % " ".join("(subpath %s)" % _sb_str(d) for d in readable + writable),
        "(allow file-write* %s)" % " ".join("(subpath %s)" % _sb_str(d) for d in writable),
        "(allow file-write-data (literal \"/dev/null\") (literal \"/dev/zero\") (literal \"/dev/tty\") (literal \"/dev/dtracehelper\"))",
        "(allow file-write* (regex #\"^/dev/fd/\") (regex #\"^/dev/ttys\"))",
        "(allow mach-lookup %s)" % " ".join("(global-name %s)" % _sb_str(n) for n in SEATBELT_MACH),
    ]
    return "\n".join(l for l in lines if l) + "\n"


def _seatbelt(req):
    tmp = tempfile.mkdtemp(prefix="capocantiere-run-")
    tmp = _real(tmp)  # /var/folders/... is a symlink to /private/var/folders/...
    profile = seatbelt_profile(req, tmp)
    env = _keep_env(req, ("__CF_USER_TEXT_ENCODING",))
    env["TMPDIR"] = tmp
    a = ["-p", profile, req["cmd"]] + list(req["args"])
    return Prepared(seatbelt_bin(), a, env=env, cleanup=[lambda: shutil.rmtree(tmp, ignore_errors=True)])
