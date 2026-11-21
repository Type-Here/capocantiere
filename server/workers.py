# Workers: the external coding-agent CLIs that run the tasks Claude delegates.
#
# Each worker is an adapter that knows how to build the command line for its CLI, how to read
# its output (final answer, error, usage) and how to recognize quota/rate-limit errors.
# Per-worker settings come from built-in defaults, then the user's config file, then
# environment variables (CAPOCANTIERE_<WORKER>_<SETTING>), in that order.

import json
import os
import re
import threading
import time
from datetime import datetime

import isolation
from scores import DEFAULT_TIER, TIERS
from common import (CONFIG_FILE, HERE, SETTINGS_FILE, STATE_DIR, boolean, env, iso_now, not_found, now_ms, num,
                    read_json, run, write_json)

# ---------------------------------------------------------------- config

GLOBAL_DEFAULTS = {
    "order": ["copilot", "vibe", "codex", "agy"],  # preference order when no worker is requested
    "fallback": True,  # on quota/rate-limit errors or exhausted budget, move on to the next worker
    "max_parallel": 4,  # jobs running at once, across all workers
    "isolation": "none",  # none | auto | bwrap (Linux) | seatbelt (macOS, experimental); see isolation.py
    # D-Bus names that reach the system keyring, let into the sandbox with "host" credentials (Linux)
    "keyring_dbus_names": list(isolation.DEFAULT_KEYRING_NAMES),
    # How a worker is chosen when none is requested: "scores" (trust scores, Thompson sampling; `order` only
    # breaks ties while there is no history) or "order" (always the preference order). See scores.py.
    "selection": "scores",
    "half_life_days": 30,  # trust score events lose half their weight every N days
}

# Settings every worker has. Adapters add their own on top.
COMMON_DEFAULTS = {
    "enabled": True,
    "bin": "",
    "model": "",  # empty = the CLI's default
    "analyze_model": "",  # empty = same as model
    "effort": "",
    "daily_budget": 50,  # runs per 24h
    "max_parallel": 2,
    "timeout_min": 20,
    "analyze_timeout_min": 8,
    "extra_args": [],
    "env": {},  # extra environment variables for the CLI (e.g. an API key variable)
    "strengths": [],
    "isolation": "",  # empty = the global setting
    # How the CLI authenticates: "host" = its own login (e.g. `copilot login`, the keyring), "token" = a minimal
    # credential the server injects into this worker only, from the plugin options (userConfig <worker>_token,
    # stored by Claude Code in the system's secure storage) or from <state>/credentials/<worker> (mode 600),
    # "private-home" = a config/auth folder of the CLI's own kept for capocantiere only (<state>/homes/<worker>),
    # with a login made there (workers that support it: codex). Empty = "token" if the plugin options hold a token
    # for this worker, otherwise "host".
    "credentials": "",
    "isolation_rw": [],  # extra paths the worker may write when isolated
    "isolation_ro": [],  # extra paths the worker may read when isolated
    "isolation_env": [],  # extra environment variables kept when isolated (names, or prefixes ending in "_")
    # Models per capability tier, e.g. {"fast": "m1", "standard": "m2", "strong": ["m3", "m4"]}; each model is
    # a separate arm for the trust scores. Empty = the worker serves every tier with its model setting.
    "tiers": {},
}

CREDENTIALS = ("host", "token", "private-home")
SELECTIONS = ("scores", "order")
CREDENTIALS_DIR = os.path.join(STATE_DIR, "credentials")
HOMES_DIR = os.path.join(STATE_DIR, "homes")

# Old environment variables of the gemini-delegate server, mapped to agy settings.
LEGACY_AGY_ENV = {
    "GEMINI_DELEGATE_AGY_BIN": "bin",
    "GEMINI_DELEGATE_MODEL": "model",
    "GEMINI_DELEGATE_ANALYZE_MODEL": "analyze_model",
    "GEMINI_DELEGATE_EFFORT": "effort",
    "GEMINI_DELEGATE_DAILY_BUDGET": "daily_budget",
    "GEMINI_DELEGATE_MAX_PARALLEL": "max_parallel",
    "GEMINI_DELEGATE_TASK_TIMEOUT_MIN": "timeout_min",
    "GEMINI_DELEGATE_ANALYZE_TIMEOUT_MIN": "analyze_timeout_min",
    "GEMINI_DELEGATE_SANDBOX": "sandbox",
    "GEMINI_DELEGATE_SKIP_PERMISSIONS": "skip_permissions",
    "GEMINI_DELEGATE_EXTRA_ARGS": "extra_args",
}


def coerce(value, like):
    """Convert an environment string to the type of the default value."""
    if isinstance(like, bool):
        return boolean(value, like)
    if isinstance(like, (int, float)):
        return num(value, like)
    if isinstance(like, list):
        sep = "," if "," in value else " "
        return [x.strip() for x in value.split(sep) if x.strip()]
    if isinstance(like, dict):
        try:
            v = json.loads(value)
            return v if isinstance(v, dict) else like
        except ValueError:
            return like
    return value


def merge_settings(defaults, override):
    out = dict(defaults)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue  # comments
        out[k] = v
    return out


_config_cache = {"mtime": None, "data": None}
_config_lock = threading.Lock()


def load_config():
    """The effective config: {"order", "fallback", "max_parallel", "workers": {id: settings}, "errors": [...]}.

    The file is re-read when it changes, so edits apply without restarting the session.
    """
    with _config_lock:
        try:
            mtime = os.stat(CONFIG_FILE).st_mtime
        except OSError:
            mtime = None
        if _config_cache["data"] is not None and _config_cache["mtime"] == mtime:
            return _config_cache["data"]
        errors = []
        user = {}
        if mtime is not None:
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    user = json.load(f)
                if not isinstance(user, dict):
                    raise ValueError("the top level must be an object")
            except (OSError, ValueError) as e:
                errors.append("%s is not valid JSON (%s): using the built-in defaults." % (CONFIG_FILE, e))
                user = {}
        cfg = merge_settings(GLOBAL_DEFAULTS, {k: v for k, v in user.items() if k != "workers"})
        for key, like in GLOBAL_DEFAULTS.items():
            v = env.get("CAPOCANTIERE_" + key.upper())
            if v:
                cfg[key] = coerce(v, like)
        user_workers = user.get("workers") or {}
        for wid in user_workers:
            if wid not in ADAPTERS and not wid.startswith("_"):
                errors.append('Unknown worker "%s" in %s (known: %s).' % (wid, CONFIG_FILE, ", ".join(ADAPTERS)))
        workers = {}
        for wid, cls in ADAPTERS.items():
            defaults = merge_settings(COMMON_DEFAULTS, cls.DEFAULTS)
            s = merge_settings(defaults, user_workers.get(wid))
            if wid == "agy":
                for var, key in LEGACY_AGY_ENV.items():
                    if env.get(var):
                        s[key] = coerce(env[var], defaults[key])
            for key, like in defaults.items():
                v = env.get("CAPOCANTIERE_%s_%s" % (wid.upper(), key.upper()))
                if v:
                    s[key] = coerce(v, like)
            s["isolation"] = s.get("isolation") or cfg["isolation"]  # effective value
            workers[wid] = s
        if cfg["selection"] not in SELECTIONS:
            errors.append('"selection": "%s" is not valid (use %s): using "scores".' % (cfg["selection"], " or ".join(SELECTIONS)))
            cfg["selection"] = "scores"
        for wid, s in workers.items():
            t = s.get("tiers")
            if not isinstance(t, dict) or any(k not in TIERS for k in t if not k.startswith("_")):
                errors.append('"tiers" of %s must be an object with keys among %s: ignored.' % (wid, ", ".join(TIERS)))
                s["tiers"] = {}
            else:
                s["tiers"] = {k: v for k, v in t.items() if not k.startswith("_") and v}
        cfg["order"] = [w for w in cfg["order"] if w in ADAPTERS] + [w for w in ADAPTERS if w not in cfg["order"]]
        cfg["workers"] = workers
        cfg["errors"] = errors
        _config_cache.update(mtime=mtime, data=cfg)
        return cfg


def get_worker(wid):
    cfg = load_config()
    if wid not in ADAPTERS:
        raise RuntimeError('Unknown worker "%s". Workers: %s.' % (wid, ", ".join(ADAPTERS)))
    return ADAPTERS[wid](cfg["workers"][wid])


def all_workers():
    cfg = load_config()
    return [ADAPTERS[w](cfg["workers"][w]) for w in cfg["order"]]


def example_config():
    """The config file with every setting at its default, for the README and `workers`."""
    out = {"_comment": "Capocantiere workers. Remove what you don't change; missing keys use the defaults."}
    out.update(GLOBAL_DEFAULTS)
    out["workers"] = {wid: merge_settings(COMMON_DEFAULTS, cls.DEFAULTS) for wid, cls in ADAPTERS.items()}
    return out


# ---------------------------------------------------------------- worker interface


class Worker:
    """Base adapter. Subclasses set id/label/DEFAULTS and implement invocation() and parse()."""

    id = ""
    label = ""
    DEFAULTS = {}
    QUOTA_RE = re.compile(r"(\b429\b|rate.?limit|quota|too many requests)", re.I)
    WARNING = ""  # shown in `workers` and at session start when the worker is enabled
    OWN_TIMEOUT = False  # True if the CLI enforces the timeout itself (then the server adds a grace minute)
    INSTALL_HINT = ""
    TOKEN_ENV = ""  # the variable a "token" credential is injected as
    TOKEN_HINT = ""  # how to get a minimal token
    CONFIG_DIRS = []  # (environment variable that overrides it, default): the CLI's own config/auth folders
    PASS_ENV = []  # variables the CLI reads, kept when isolated ("host" credentials only for HOST_ENV)
    HOST_ENV = []  # variables that carry the user's own login (kept with "host" credentials only)
    PRIVATE_HOME_ENV = ""  # the variable that points the CLI at another config/auth folder ("private-home")
    _version_cache = {}  # bin -> (checked_at, installed, text)

    def __init__(self, settings):
        self.s = settings

    @property
    def enabled(self):
        return bool(self.s.get("enabled"))

    @property
    def bin(self):
        return self.s.get("bin") or self.id

    def version(self):
        """(installed, first line of --version). Cached for a minute."""
        hit = Worker._version_cache.get(self.bin)
        if hit and time.time() - hit[0] < 60:
            return hit[1], hit[2]
        r = run(self.bin, ["--version"], timeout_ms=15_000, extra_env=self.run_env())
        if not_found(r):
            installed, text = False, "not found"
        else:
            installed = True
            text = ((r["stdout"] or r["stderr"]).strip().split("\n")[0]) or "ok"
        Worker._version_cache[self.bin] = (time.time(), installed, text)
        return installed, text

    # -- credentials

    @property
    def credentials(self):
        c = self.s.get("credentials")
        if c:
            return c
        return "token" if self.TOKEN_ENV and self.option_token() else "host"

    def token_file(self):
        return os.path.join(CREDENTIALS_DIR, self.id)

    def option_key(self):
        """The plugin option (userConfig key in plugin.json) that holds this worker's token."""
        return "%s_token" % self.id

    def token_var(self):
        """Where the MCP server receives the plugin option: .mcp.json sets it from ${user_config.<key>}, which
        also shadows any variable of the same name in the user's shell."""
        return "CAPOCANTIERE_%s_TOKEN" % self.id.upper()

    def option_token(self):
        """The token from the plugin options, or None. The MCP server gets it as token_var(); hooks get it from
        Claude Code as CLAUDE_PLUGIN_OPTION_<KEY> (the hook drops token_var() inherited from the shell)."""
        for var in (self.token_var(), "CLAUDE_PLUGIN_OPTION_" + self.option_key().upper(),
                    "CLAUDE_PLUGIN_OPTION_" + self.option_key()):
            v = (env.get(var) or "").strip()
            if v and not v.startswith("${"):  # an unexpanded placeholder is not a token
                return v
        return None

    def token(self):
        """(token, where it was read) or (None, why not)."""
        v = self.option_token()
        if v:
            return v, "the plugin options (%s)" % self.option_key()
        path = self.token_file()
        try:
            st = os.stat(path)
        except OSError:
            return None, ("no token: set %s in the plugin options (/plugin, capocantiere, Configure options) or put it "
                          "in %s (chmod 600). %s" % (self.option_key(), path, self.TOKEN_HINT))
        if os.name == "posix" and st.st_mode & 0o077:
            return None, "%s is readable by other users: run chmod 600 on it" % path
        try:
            with open(path, encoding="utf-8") as f:
                v = f.read().strip()
        except OSError as e:
            return None, "cannot read %s: %s" % (path, e)
        return (v, path) if v else (None, "%s is empty" % path)

    def private_home(self):
        return os.path.join(HOMES_DIR, self.id)

    def login_command(self):
        """How the user logs the CLI in inside its private home."""
        return "%s=%s %s login" % (self.PRIVATE_HOME_ENV, self.private_home(), self.bin)

    def private_home_problem(self):
        """Why the private home cannot be used (not set up, not logged in), or None. Adapters override it."""
        return None

    def uses_keyring(self):
        """True if the login lives in the system keyring: an isolated run then reaches it through the D-Bus proxy."""
        return self.credentials == "host"

    def sandbox_credentials(self):
        """What isolation.py is told: "host" (keyring, D-Bus proxy) or "file" (no D-Bus at all)."""
        return "host" if self.uses_keyring() else "file"

    def credentials_problem(self):
        c = self.credentials
        if c not in CREDENTIALS:
            return 'unknown credentials "%s" (use %s)' % (c, ", ".join(CREDENTIALS))
        if c == "private-home":
            if not self.PRIVATE_HOME_ENV:
                return 'credentials "private-home" is not supported by %s (use host or token)' % self.id
            return self.private_home_problem()
        if c == "token":
            if not self.TOKEN_ENV:
                return 'credentials "token" is not supported by %s' % self.id
            t, why = self.token()
            if not t:
                return why
        return None

    def run_env(self):
        """Variables added to the CLI's environment: the env setting, plus the token with "token" credentials."""
        e = self.env()
        if self.credentials == "token" and self.TOKEN_ENV:
            t, _ = self.token()
            if t:
                e[self.TOKEN_ENV] = t
        elif self.credentials == "private-home" and self.PRIVATE_HOME_ENV:
            e[self.PRIVATE_HOME_ENV] = self.private_home()
        return e

    def config_dirs(self):
        """The CLI's config/auth folders, made writable when isolated. With "private-home", only that folder."""
        if self.credentials == "private-home" and self.PRIVATE_HOME_ENV:
            return [self.private_home()]
        return [env.get(var) or os.path.expanduser(default) if var else os.path.expanduser(default)
                for var, default in self.CONFIG_DIRS]

    def isolation_request(self, spec, inv):
        """The worker's part of an isolation.prepare() request (see isolation.py)."""
        import shutil
        keep = list(self.PASS_ENV) + list(self.s.get("isolation_env") or [])
        if self.credentials == "host":
            keep += self.HOST_ENV
        return {
            "cmd": self.bin, "args": inv["argv"], "cwd": spec["cwd"], "writable": bool(spec.get("auto_approve")),
            "env": self.run_env(), "pass_env": keep, "credentials": self.sandbox_credentials(),
            "rw": self.config_dirs() + list(inv.get("cleanup") or []) + list(self.s.get("isolation_rw") or []),
            "ro": list(self.s.get("isolation_ro") or []),
            "keyring_names": load_config().get("keyring_dbus_names") or isolation.DEFAULT_KEYRING_NAMES,
            "bin_path": shutil.which(self.bin),
        }

    def auth_status(self):
        """(True/False/None, note). None = cannot be checked without a real call."""
        if self.credentials != "host":
            p = self.credentials_problem()
            if p:
                return False, p
            if self.credentials == "private-home":
                return self.private_home_status()
            return True, "%s token from %s" % (self.TOKEN_ENV, self.token()[1])
        return self.host_auth_status()

    def private_home_status(self):
        return None, "private home %s: not verifiable without a real call" % self.private_home()

    def host_auth_status(self):
        return None, "not verifiable without a real call"

    def warnings(self):
        """Problems in the CLI's own setup that the user should fix (shown by `workers`)."""
        return [self.WARNING] if self.WARNING else []

    def blocker(self):
        """A reason why runs would certainly fail (the server then skips this worker), or None."""
        return self.credentials_problem() or isolation.problem(self.s.get("isolation") or "none", self.sandbox_credentials())

    def isolation_notes(self):
        """The limits of the current isolation setup, one line for `workers`."""
        setting = self.s.get("isolation") or "none"
        try:
            b = isolation.resolve(setting)
        except ValueError:
            return ""
        if b is None:
            return "none (the worker runs with all your permissions)"
        parts = ["%s%s%s" % ("auto → " if setting == "auto" else "", b,
                             " (EXPERIMENTAL, untested)" if b in isolation.EXPERIMENTAL else "")]
        if self.uses_keyring():
            parts.append("can read the secrets of your system keyring" if b == "bwrap"
                         else "can read the Keychain items its own program is allowed to read")
        parts.append("sees its own credential, has free network access")
        return "; ".join(parts)

    def env(self):
        return {str(k): str(v) for k, v in (self.s.get("env") or {}).items()}

    def resolve_model(self, role, requested=None, effort=None):
        """(model, effort, source). model "" means the CLI's default."""
        for value, src in ((requested, "requested"),
                           (self.s.get("analyze_model") if role == "analyze" else None, "config"),
                           (self.s.get("model"), "config")):
            if value and value != "auto":
                return value, effort or self.s.get("effort") or "", src
        return "", effort or self.s.get("effort") or "", "CLI default"

    def arm_models(self, role, tier=None, model=None):
        """The model requests (for resolve_model) this worker offers for a tier, one arm each.

        None = the worker's usual rules. An explicit model wins. A worker with "tiers" serves only the tiers it
        lists when one is requested; without "tiers" it serves every tier with its model setting.
        """
        if model:
            return [model]
        tiers = self.s.get("tiers") or {}
        if tiers:
            v = tiers.get(tier or DEFAULT_TIER.get(role, "standard"))
            if v:
                return [str(x) for x in v] if isinstance(v, list) else [str(v)]
            if tier:
                return []
        return [None]

    def timeout_min(self, role):
        return self.s["analyze_timeout_min"] if role == "analyze" else self.s["timeout_min"]

    def invocation(self, spec):
        """spec: role, prompt, cwd, model, effort, timeout_min, auto_approve, tmp_dir.

        Returns {"argv": [...], "env": {...}, "input": str or None}.
        """
        raise NotImplementedError

    def parse(self, r, spec):
        """r is the result of common.run. Returns ok/status/response/error/usage/turns/conversation_id/duration."""
        raise NotImplementedError

    def soft_denied(self, r):
        return [l for l in (r["stderr"] or "").split("\n")
                if re.search(r"denied|not approved|requires approval|soft-den", l, re.I)][:10]

    def failure_note(self, model, error):
        """Extra text after a failed run (e.g. a model marked unavailable)."""
        return ""

    def models_line(self):
        parts = []
        for role in ("analyze", "implement"):
            try:
                model, _, source = self.resolve_model(role)
                parts.append("%s → %s" % (role, model or "CLI default") + ("" if source == "CLI default" else " (%s)" % source))
            except Exception as e:
                parts.append("%s → ERROR: %s" % (role, e))
        return "; ".join(parts)


# ---------------------------------------------------------------- agy (Antigravity CLI)
#
# The model list comes from `agy models`, which only shows what the account can use, so new
# models are picked up without updating the plugin. Slugs are sorted into capability groups by
# the rules in model-groups.json.

with open(os.path.join(HERE, "model-groups.json"), encoding="utf-8") as _f:
    MODEL_RULES = json.load(_f)
GROUP_IDS = [g["id"] for g in MODEL_RULES["groups"]]
OTHER_GROUP = {"id": "other", "description": "Not classified. Never chosen automatically, only when requested by slug or group."}
MODELS_FILE = os.path.join(STATE_DIR, "models.json")
MODELS_TTL_MS = 6 * 3600_000
LEVELS = ("low", "medium", "high", "xhigh", "max")
LEVEL_RE = re.compile(r"-(%s)$" % "|".join(LEVELS))
MODEL_ERROR_RE = re.compile(
    r"not recognized as a known model|model.{0,80}(not available|unavailable|not allowed|not supported|not entitled|permission denied)",
    re.I,
)
MODELS_LOCK = threading.Lock()


def parse_models(stdout):
    """`agy models` prints one "slug<TAB>Display name" line per model."""
    out = []
    for line in stdout.split("\n"):
        if "\t" not in line:
            continue
        slug, name = line.split("\t", 1)
        if slug.strip():
            out.append({"slug": slug.strip(), "name": name.strip() or slug.strip()})
    return out


def glob_re(pattern):
    return re.compile("^" + ".*".join(re.escape(x) for x in pattern.split("*")) + "$", re.I)


def group_of(slug):
    for g in MODEL_RULES["groups"]:
        for i, pat in enumerate(g["match"]):
            if re.search(pat, slug, re.I):
                return g["id"], i
    return "other", 0


def version_key(name):
    """Newest first: "Gemini 3.8 Flash" -> (-3, -8, 0, 0)."""
    m = re.search(r"\d+(?:\.\d+)*", name)
    parts = [int(x) for x in m.group().split(".")][:4] if m else []
    return tuple(-x for x in parts + [0] * (4 - len(parts)))


def catalog(cache, settings):
    """Usable models by group, best first, plus the ones excluded by settings."""
    unavailable = cache.get("unavailable") or {}
    excl = [glob_re(p) for p in settings.get("exclude") or []]
    groups = {gid: [] for gid in GROUP_IDS + ["other"]}
    excluded = []
    for m in cache.get("models") or []:
        if m["slug"] in unavailable:
            continue
        if any(rx.match(m["slug"]) for rx in excl):
            excluded.append(m)
            continue
        gid, idx = group_of(m["slug"])
        groups[gid].append(dict(m, rank=(idx,) + version_key(m["name"])))
    for lst in groups.values():
        lst.sort(key=lambda m: m["rank"])
    return {"groups": groups, "excluded": excluded}


def groups_hint(cat):
    parts = ["%s: %s" % (g, ", ".join(m["slug"] for m in ms)) for g, ms in cat["groups"].items() if ms]
    return "Available — " + ("; ".join(parts) or "none") + ". See the models tool."


class AgyWorker(Worker):
    id = "agy"
    label = "Antigravity CLI (Gemini)"
    DEFAULTS = {
        "enabled": False,
        "bin": "agy",
        "daily_budget": 150,
        "max_parallel": 3,
        # How agy authenticates (https://antigravity.google/docs/install.md):
        #   "api_key": Gemini API key from AI Studio in GEMINI_API_KEY; each run gets
        #              --settings '{"modelProvider":"gemini"}' (per session, never saved)
        #   "adc":     Google Cloud Application Default Credentials (AGY_ADC_AUTH=true)
        #   "login":   the signed-in Google account (consumer subscription): see WARNING
        "auth": "api_key",
        "sandbox": True,  # pass --sandbox
        # Inside the isolated worktree agy may edit files and run commands without asking:
        # it's the only way to work headless. The main repo stays protected.
        "skip_permissions": True,
        "strengths": ["analysis", "boilerplate", "tests", "docs"],
    }
    QUOTA_RE = re.compile(r"(429|RESOURCE_EXHAUSTED|rate ?limit|quota)", re.I)
    OWN_TIMEOUT = True  # agy stops itself (--print-timeout): the server only kills it after a grace minute
    WARNING = ("According to the official Antigravity FAQ, running `agy -p` inside third-party agents is not "
               "supported on consumer plans and can get the account suspended. Enable this worker only with "
               '"auth": "api_key" (a Gemini API key from AI Studio, the default) or "adc" (Google Cloud), never '
               'with "login" on a consumer subscription.')
    INSTALL_HINT = "Install Antigravity CLI: https://antigravity.google/docs/cli"
    AUTH_MODES = ("api_key", "adc", "login")
    TOKEN_ENV = "GEMINI_API_KEY"
    TOKEN_HINT = "Use a Gemini API key from AI Studio (https://aistudio.google.com/apikey)."
    CONFIG_DIRS = [("", "~/.gemini")]
    PASS_ENV = ["AGY_", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"]
    HOST_ENV = ["GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"]

    def config_dirs(self):
        dirs = super().config_dirs()
        if self.s.get("auth") == "adc":
            dirs.append(os.path.expanduser("~/.config/gcloud"))  # the ADC file, refreshed by the client
        return dirs

    def env(self):
        e = super().env()
        if self.s.get("auth") == "adc":
            e.setdefault("AGY_ADC_AUTH", "true")
        return e

    def blocker(self):
        base = super().blocker()
        if base:
            return base
        auth = self.s.get("auth")
        if auth not in self.AUTH_MODES:
            return 'unknown auth "%s" (use %s)' % (auth, ", ".join(self.AUTH_MODES))
        if self.credentials == "token":
            if auth != "api_key":
                return 'credentials "token" needs "auth": "api_key"'
        elif auth == "api_key" and not (env.get("GEMINI_API_KEY") or super().env().get("GEMINI_API_KEY")):
            return 'auth is "api_key" but GEMINI_API_KEY is not set (export it, put it under "env" in the config, or use "token" credentials)'
        return None

    def host_auth_status(self):
        auth = self.s.get("auth")
        problem = self.blocker()
        if problem:
            return False, problem
        if auth == "api_key":
            return True, "Gemini API key (GEMINI_API_KEY)"
        if auth == "adc":
            return None, "Google Cloud ADC (AGY_ADC_AUTH=true): not verifiable without a real call"
        return None, "signed-in Google account: not verifiable without a real call"

    def warnings(self):
        if not self.enabled or self.s.get("auth") == "login":
            return [self.WARNING]
        return []

    def arm_models(self, role, tier=None, model=None):
        """Without "tiers" in the config, a tier is the agy group of the same name."""
        if not model and tier and not self.s.get("tiers") and tier in GROUP_IDS:
            return [tier]
        return super().arm_models(role, tier, model)

    # -- models

    def model_cache(self, refresh=False):
        """The cached model list, re-read from `agy models` when stale. Never raises."""
        with MODELS_LOCK:
            cache = read_json(MODELS_FILE, {})
            if refresh or not cache.get("models") or now_ms() - (cache.get("fetchedAt") or 0) > MODELS_TTL_MS:
                r = run(self.bin, ["models"], timeout_ms=30_000, extra_env=self.run_env())
                models = parse_models(r["stdout"]) if r["code"] == 0 else []
                if models:
                    # an explicit refresh also forgets models marked unavailable
                    cache = {"fetchedAt": now_ms(), "models": models,
                             "unavailable": {} if refresh else cache.get("unavailable") or {}}
                    write_json(MODELS_FILE, cache)
                else:
                    cache = dict(cache, fetchError=(r["stderr"] or r["stdout"]).strip()[:500] or "`agy models` failed")
            return cache

    def mark_unavailable(self, slug):
        with MODELS_LOCK:
            cache = read_json(MODELS_FILE, {})
            cache.setdefault("unavailable", {})[slug] = iso_now()
            write_json(MODELS_FILE, cache)

    def resolve_model(self, role, requested=None, effort=None):
        """Precedence: requested (tool argument) > worker config > settings (models tool) > automatic.

        Each of them may be a slug or a group id; a group follows new models automatically.
        """
        cache = self.model_cache()
        settings = read_json(SETTINGS_FILE, {})
        cat = catalog(cache, settings)
        known = {m["slug"] for m in cache.get("models") or []}
        cfg_model = (self.s.get("analyze_model") or self.s.get("model")) if role == "analyze" else self.s.get("model")
        choice = source = None
        for value, src in ((requested, "requested"), (cfg_model, "config"), (settings.get(role), "settings")):
            if value and value != "auto":
                choice, source = value, src
                break
        if choice is None:
            model, source = "", "agy default"
            for gid in MODEL_RULES["defaults"][role]:
                if cat["groups"][gid]:
                    model, source = cat["groups"][gid][0]["slug"], "auto, group " + gid
                    break
        elif choice in cat["groups"]:
            if not cat["groups"][choice]:
                raise RuntimeError('No agy model available in group "%s". %s' % (choice, groups_hint(cat)))
            model, source = cat["groups"][choice][0]["slug"], "%s, group %s" % (source, choice)
        elif not known or choice in known:
            model = choice  # list unknown (agy models failed): pass it through and let agy decide
        else:
            raise RuntimeError('Unknown agy model "%s". %s' % (choice, groups_hint(cat)))

        effort = effort or self.s.get("effort") or ""
        if effort and model and LEVEL_RE.search(model):
            # the level is part of the slug and agy rejects --effort with it:
            # pick the variant of the same model closest to the requested level
            base = LEVEL_RE.sub("", model)
            want = LEVELS.index(effort) if effort in LEVELS else None
            variants = [lv for lv in LEVELS if "%s-%s" % (base, lv) in known]
            if want is not None and variants:
                model = "%s-%s" % (base, min(variants, key=lambda lv: (abs(LEVELS.index(lv) - want), -LEVELS.index(lv))))
            effort = ""
        return model, effort, source

    def models_report(self, refresh=False):
        cache = self.model_cache(refresh)
        if not cache.get("models"):
            return "Could not read the agy model list (%s). Runs will use agy's default model." % (
                cache.get("fetchError") or "no output from `agy models`")
        settings = read_json(SETTINGS_FILE, {})
        cat = catalog(cache, settings)
        when = datetime.fromtimestamp(cache["fetchedAt"] / 1000).strftime("%Y-%m-%d %H:%M")
        lines = ["Models this account can use through agy (read %s):" % when]
        width = max(len(m["slug"]) for m in cache["models"])
        for g in MODEL_RULES["groups"] + [OTHER_GROUP]:
            ms = cat["groups"][g["id"]]
            if g is OTHER_GROUP and not ms:
                continue
            lines += ["", "%s — %s" % (g["id"], g["description"])]
            if not ms:
                lines.append("    (none available)")
            for i, m in enumerate(ms):
                lines.append("  %s %s  %s" % ("*" if i == 0 else " ", m["slug"].ljust(width), m["name"]))
        lines += ["", "* = the model used when that group is requested."]
        if cat["excluded"]:
            lines.append("Excluded by settings: " + ", ".join(m["slug"] for m in cat["excluded"]))
        if cache.get("unavailable"):
            lines.append("Marked unavailable after failing for this account (a refresh clears this): "
                         + ", ".join(sorted(cache["unavailable"])))
        lines += ["", "In use: " + self.models_line()]
        lines.append("Settings file: %s (exclude: %s)" % (SETTINGS_FILE, ", ".join(settings.get("exclude") or []) or "none"))
        return "\n".join(lines)

    def update_model_settings(self, a):
        settings = read_json(SETTINGS_FILE, {})
        known = {m["slug"] for m in self.model_cache().get("models") or []}
        for role in ("analyze", "implement"):
            if role not in a:
                continue
            v = (a[role] or "").strip()
            if not v or v == "auto":
                settings.pop(role, None)
            elif v in GROUP_IDS or v == "other" or not known or v in known:
                settings[role] = v
            else:
                raise RuntimeError('Unknown model or group "%s". Groups: %s.' % (v, ", ".join(GROUP_IDS + ["other"])))
        if "exclude" in a:
            settings["exclude"] = [str(x) for x in a["exclude"] or [] if str(x).strip()]
        write_json(SETTINGS_FILE, settings)

    def failure_note(self, model, error):
        """If agy rejected the model for this account, remember it so the next runs pick another."""
        if not model or not MODEL_ERROR_RE.search(error or ""):
            return ""
        self.mark_unavailable(model)
        return "\nModel %s has been marked unavailable; the next runs will pick another one (models refresh=true clears this)." % model

    # -- runs

    def invocation(self, spec):
        a = ["-p", spec["prompt"], "--output-format", "json"]
        if self.s.get("auth") == "api_key":
            a += ["--settings", json.dumps({"modelProvider": "gemini"})]
        if spec.get("model"):
            a += ["--model", spec["model"]]
        if spec.get("effort"):
            a += ["--effort", spec["effort"]]
        if spec.get("timeout_min"):
            a += ["--print-timeout", "%sm" % spec["timeout_min"]]
        if spec.get("auto_approve") and self.s.get("skip_permissions"):
            a.append("--dangerously-skip-permissions")
        if self.s.get("sandbox"):
            a.append("--sandbox")
        return {"argv": a + list(self.s.get("extra_args") or []), "env": self.run_env(), "input": None}

    @staticmethod
    def _envelope(stdout):
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

    def parse(self, r, spec):
        envl = self._envelope(r["stdout"]) or {}
        status = envl.get("status") or ("SUCCESS" if r["code"] == 0 else "ERROR")
        ok = r["code"] == 0 and status == "SUCCESS" and not r["timed_out"]
        response = envl.get("response")
        if response is None:
            response = "" if envl else r["stdout"].strip()
        return {
            "ok": ok,
            "status": status,
            "response": response,
            "error": str(envl["error"]) if envl.get("error") else "",
            "usage": envl.get("usage") or None,
            "turns": envl.get("num_turns") or 0,
            "conversation_id": envl.get("conversation_id") or None,
            "duration": envl.get("duration_seconds"),
        }


# ---------------------------------------------------------------- shared output helpers


def _tmp_file(spec, suffix):
    """A private temp file for one run (prompt, usage report). Deleted by the server after parse()."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="capocantiere-", suffix=suffix)
    os.close(fd)
    return path


def _sum_keys(obj, pattern):
    """Sum every numeric value whose key matches pattern, anywhere in a JSON structure."""
    total = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and pattern.search(k):
                total += v
            else:
                total += _sum_keys(v, pattern)
    elif isinstance(obj, list):
        for v in obj:
            total += _sum_keys(v, pattern)
    return total


# ---------------------------------------------------------------- copilot (GitHub Copilot CLI)
#
# Flags checked against `copilot --help` of GitHub Copilot CLI 1.0.114:
#   -p <prompt>                 non-interactive run; tool requests that would prompt are denied
#   -s                          print only the agent's response
#   --allow-all-tools           auto-approve tools; WITHOUT --allow-all-paths file access stays
#                               limited to the working directory (and the temp dir)
#   --deny-tool <rule>          wins over every allow
#   --usage-output-file <file>  final usage as JSON (totalNanoAiu, totalPremiumRequestCost, ...)
# An untrusted folder in -p mode only skips its MCP servers and repo hooks, so worktrees need no trust
# (and not trusting them keeps a repo's hooks from running).


class CopilotWorker(Worker):
    id = "copilot"
    label = "GitHub Copilot CLI"
    DEFAULTS = {
        "bin": "copilot",
        "daily_budget": 40,
        "max_parallel": 2,
        # Rules always denied while auto-approving in the worktree (shell commands are not limited to the
        # working directory by path checks, so the dangerous ones are named here).
        "deny_tools": ["shell(git push)", "shell(git remote)", "shell(git config)", "shell(git worktree)",
                       "shell(git branch)", "shell(git checkout)", "shell(git switch)", "shell(git reset)",
                       "shell(git rebase)", "shell(gh)", "shell(sudo)"],
        "disable_builtin_mcps": True,  # the GitHub MCP server could act on the user's GitHub account
        "sandbox": False,  # --sandbox: OS-level sandbox for shell commands (Linux: needs bwrap, slirp4netns, ...)
        "max_ai_credits": 0,  # --max-ai-credits per run; 0 = no limit
        # Hide other workers' secrets from the agent's shell commands.
        "secret_env_vars": ["MISTRAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
        "strengths": ["tests", "boilerplate", "refactor", "docs"],
    }
    QUOTA_RE = re.compile(r"(quota|rate.?limit|\b429\b|too many requests|100% used|premium requests?.{0,40}(limit|exhausted|exceeded)|"
                          r"exceeded.{0,40}(limit|credits|allowance)|out of (ai )?credits)", re.I)
    INSTALL_HINT = "Install GitHub Copilot CLI (https://docs.github.com/copilot/how-tos/copilot-cli) and run `copilot login`."
    TOKEN_ENV = "COPILOT_GITHUB_TOKEN"
    TOKEN_HINT = ('Create a fine-grained personal access token with only the "Copilot Requests" permission '
                  "(https://github.com/settings/personal-access-tokens/new).")
    CONFIG_DIRS = [("COPILOT_HOME", "~/.copilot"), ("", "~/.cache/copilot")]
    PASS_ENV = ["COPILOT_HOME", "COPILOT_AUTO_UPDATE", "GH_HOST"]
    HOST_ENV = ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]

    def host_auth_status(self):
        for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            if env.get(var) or self.env().get(var):
                return True, "token in %s" % var
        home = env.get("COPILOT_HOME") or os.path.join(os.path.expanduser("~"), ".copilot")
        try:
            with open(os.path.join(home, "config.json"), encoding="utf-8") as f:
                text = f.read()  # JSON with // comments: look for the key, never read the values
        except OSError:
            return False, "no %s/config.json: run `copilot login`" % home
        if re.search(r'"lastLoggedInUser"\s*:\s*\{', text):
            return True, "logged in, per %s/config.json" % home
        return False, "run `copilot login`"

    def invocation(self, spec):
        usage_file = _tmp_file(spec, ".json")
        a = ["-p", spec["prompt"], "-s", "--no-ask-user", "--no-auto-update", "--no-color",
             "--usage-output-file", usage_file]
        if spec.get("model"):
            a += ["--model", spec["model"]]
        if spec.get("effort"):
            a += ["--reasoning-effort", spec["effort"]]
        if self.s.get("disable_builtin_mcps"):
            a.append("--disable-builtin-mcps")
        if self.s.get("max_ai_credits"):
            a += ["--max-ai-credits", str(self.s["max_ai_credits"])]
        if self.s.get("secret_env_vars"):
            a.append("--secret-env-vars=" + ",".join(self.s["secret_env_vars"]))
        if spec.get("auto_approve"):
            # isolated worktree: approve tools, keep the path check, deny the dangerous commands
            a.append("--allow-all-tools")
            for rule in self.s.get("deny_tools") or []:
                a += ["--deny-tool", rule]
            if self.s.get("sandbox"):
                a.append("--sandbox")
        else:
            # analysis in the main repo: nothing is approved (read-only commands run anyway) and edits are denied
            a += ["--deny-tool", "write"]
        return {"argv": a + list(self.s.get("extra_args") or []), "env": self.run_env(), "input": None,
                "cleanup": [usage_file], "usage_file": usage_file}

    def parse(self, r, spec):
        usage = cost = model = None
        try:
            with open(spec["usage_file"], encoding="utf-8") as f:
                u = json.load(f)
            inp = _sum_keys(u.get("modelMetrics"), re.compile(r"^input.?tokens$", re.I))
            out = _sum_keys(u.get("modelMetrics"), re.compile(r"^output.?tokens$", re.I))
            usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out,
                     "premium_requests": u.get("totalPremiumRequestCost")}
            if u.get("totalNanoAiu"):
                cost = round(u["totalNanoAiu"] / 1e9, 4)  # AI credits
            # modelMetrics is keyed by the model that actually answered: take the busiest one
            mm = u.get("modelMetrics")
            if isinstance(mm, dict) and mm:
                model = max(mm, key=lambda k: _sum_keys(mm[k], re.compile(r"tokens$", re.I)))
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            pass
        response = r["stdout"].strip()
        ok = r["code"] == 0 and not r["timed_out"]
        return {"ok": ok, "status": "SUCCESS" if ok else "ERROR (exit %s)" % r["code"], "response": response,
                "error": "" if ok else (r["stderr"].strip()[-1500:] or response[-1500:]), "usage": usage,
                "cost": cost, "turns": 0, "conversation_id": None, "duration": None, "model": model}


# ---------------------------------------------------------------- vibe (Mistral Vibe CLI)
#
# Flags checked against `vibe --help`, `vibe config schema` and the source of vibe 3.1.3:
#   --prompt-file <f>           headless run with the prompt read from a file (no argv length limit)
#   --output json               the messages, as JSON, at the end of the run
#   --output-dir <dir>          writes export.json (token usage, cost, stop reason, outcome) on every exit
#   --workdir, --max-turns, --max-price (USD), --max-tokens, --time-limit <s> (exit 3 when reached)
#   --model <alias>, --thinking <level>          headless mode only, not saved
#   --auto-approve              approve every tool call (the auto-approve agent)
#   --enabled-tools <t>         repeatable; in headless mode it disables every other tool
# Without --auto-approve the default agent is "accept-edits", which still edits files: analyses therefore
# run with read_file and bash only, and bash then runs only its read-only allowlist (anything else needs an
# approval a headless run cannot give, so it is denied). With --auto-approve every command runs: vibe 3.1.3
# ignores [tools.bash] denylist then (checked), so only isolation keeps a job's git commands off the main repo. Paths outside the working directory follow `outside_workdir` in
# ~/.vibe/config.toml ("ask" by default, which --auto-approve approves): "deny" is recommended.
# An untrusted working directory is still usable; --trust only makes vibe load the repo's AGENTS.md and
# project config. Exit codes: 0 finished, 1 usage/config error, 2 infrastructure failure (the model API
# after its retries: HTTP 429 = rate limit, 402 = the account has no credit left), 3 stopped on a limit
# (turn_limit, token_limit, price_limit, deadline, ...). export.json: outcome, stop_reason,
# error {message, code}, usage {input_tokens, output_tokens, ...}, cost_usd, config (the resolved config:
# active_model is an alias of one of config.models, "" when not pinned).
# The cost is computed from the prices in the config, so --max-price is only an extra guard: the real
# budget is the number of runs per worker.


class VibeWorker(Worker):
    id = "vibe"
    label = "Mistral Vibe CLI"
    DEFAULTS = {
        "bin": "vibe",
        "daily_budget": 60,
        "max_parallel": 2,
        "max_turns": 40,
        "max_price": 0.5,  # USD cap per run (--max-price); 0 = no cap
        "max_tokens": 0,  # prompt + completion tokens cap per run (--max-tokens); 0 = no cap
        # --trust: trust the worktree for this run only. Off: it would load the repo's AGENTS.md and Vibe config,
        # which come from the repo (or from an earlier worker in the same worktree), not from you.
        "trust": False,
        # The only tools enabled for analyses. vibe 3.1 has no grep tool: it searches with bash, which in a run
        # without --auto-approve only runs its read-only allowlist (rg, grep, ls, git diff/log/status, ...) and
        # denies anything else, since a headless run cannot ask for approval.
        "analyze_tools": ["read_file", "bash"],
        "analyze_max_tokens": 200000,  # prompt + completion tokens cap per analysis (--max-tokens); 0 = max_tokens
        "disabled_tools": [],  # tools disabled in implementation runs (--disabled-tools)
        "strengths": ["analysis", "docs", "boilerplate", "tests"],
    }
    # The per-run caps (--max-price, --max-tokens) are not quota errors: a fallback would not help.
    QUOTA_RE = re.compile(r"(\b429\b|\b402\b|rate.?limit|quota|too many requests|payment required|usage.?limit|"
                          r"no credits?( left)?|credits?.{0,20}(exhausted|left)|capacity exceeded|requests? limit)", re.I)
    OWN_TIMEOUT = True  # --time-limit
    INSTALL_HINT = "Install Mistral Vibe (`uv tool install mistral-vibe`) and log in (run `vibe` once) or set MISTRAL_API_KEY."
    TOKEN_ENV = "MISTRAL_API_KEY"
    TOKEN_HINT = "Use a Mistral API key (https://console.mistral.ai/api-keys)."
    CONFIG_DIRS = [("VIBE_HOME", "~/.vibe")]
    PASS_ENV = ["VIBE_"]
    HOST_ENV = ["MISTRAL_API_KEY"]

    @staticmethod
    def home():
        return env.get("VIBE_HOME") or os.path.join(os.path.expanduser("~"), ".vibe")

    def host_auth_status(self):
        if env.get("MISTRAL_API_KEY") or self.env().get("MISTRAL_API_KEY"):
            return True, "MISTRAL_API_KEY set"
        try:
            with open(os.path.join(self.home(), ".env"), encoding="utf-8") as f:
                if re.search(r"^\s*(export\s+)?MISTRAL_API_KEY\s*=\s*\S", f.read(), re.M):
                    return True, "MISTRAL_API_KEY in %s/.env" % self.home()
        except OSError:
            pass
        # Signed in with a Mistral account (browser login): vibe caches the account's plan here.
        try:
            with open(os.path.join(self.home(), "whoami_cache.json"), encoding="utf-8") as f:
                m = re.search(r'"plan_name"\s*:\s*"([^"]*)"', f.read())
            if m:
                return True, "Mistral account, plan %s (per %s/whoami_cache.json)" % (m.group(1), self.home())
        except OSError:
            pass
        return False, "not signed in: run `vibe` once to log in with your Mistral account, or set MISTRAL_API_KEY"

    def warnings(self):
        try:
            with open(os.path.join(self.home(), "config.toml"), encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""
        out = []
        if not re.search(r'^\s*outside_workdir\s*=\s*"deny"', text, re.M):
            out.append('%s/config.toml does not set outside_workdir = "deny": with auto-approve, vibe may touch files '
                       'outside the job\'s worktree. Add that line (it also applies to your interactive sessions).' % self.home())
        if "grep" in (self.s.get("analyze_tools") or []):
            out.append('analyze_tools lists "grep", which vibe 3.1+ no longer has (it searches with bash): use '
                       '["read_file", "bash"]; without auto-approval bash only runs read-only commands.')
        if (self.s.get("isolation") or "none") == "none":
            out.append("vibe auto-approves every command in a job, git included (it ignores its bash denylist when "
                       'auto-approving): only isolation keeps it from changing the main repo\'s branches or pushing. '
                       'See /capocantiere:doctor.')
        if re.search(r"^\s*auto_worktree\s*=\s*true", text, re.M):
            out.append("%s/config.toml sets auto_worktree = true: vibe would edit its own worktree instead of the "
                       "job's, and every job would end with no changes. Set it to false." % self.home())
        return out

    def invocation(self, spec):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="capocantiere-vibe-")
        prompt_file = os.path.join(tmp, "prompt.md")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(spec["prompt"])
        out_dir = os.path.join(tmp, "out")
        a = ["--prompt-file", prompt_file, "--output", "json", "--output-dir", out_dir, "--workdir", spec["cwd"],
             "--max-turns", str(self.s["max_turns"])]
        if self.s.get("max_price"):
            a += ["--max-price", str(self.s["max_price"])]
        max_tokens = (not spec.get("auto_approve") and self.s.get("analyze_max_tokens")) or self.s.get("max_tokens")
        if max_tokens:
            a += ["--max-tokens", str(max_tokens)]
        if spec.get("timeout_min"):
            a += ["--time-limit", str(max(1, int(round(spec["timeout_min"] * 60))))]
        if spec.get("model"):
            a += ["--model", spec["model"]]
        if spec.get("effort"):
            a += ["--thinking", spec["effort"]]
        if self.s.get("trust"):
            a.append("--trust")
        if spec.get("auto_approve"):
            a.append("--auto-approve")
            for t in self.s.get("disabled_tools") or []:
                a += ["--disabled-tools", t]
        else:
            for t in self.s.get("analyze_tools") or ["read_file"]:
                a += ["--enabled-tools", t]
        return {"argv": a + list(self.s.get("extra_args") or []), "env": self.run_env(), "input": None,
                "cleanup": [tmp], "export_file": os.path.join(out_dir, "export.json")}

    @staticmethod
    def _messages(stdout):
        try:
            v = json.loads(stdout)
        except ValueError:
            return None
        if isinstance(v, dict):
            v = v.get("messages")
        return v if isinstance(v, list) else None

    @staticmethod
    def _text(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content).strip()
        return ""

    @staticmethod
    def _export(path):
        """{outcome, stop, error, usage, cost} from export.json (see the header comment)."""
        try:
            with open(path, encoding="utf-8") as f:
                e = json.load(f)
        except (OSError, ValueError):
            e = None
        if not isinstance(e, dict):
            return {"outcome": "", "stop": "", "error": "", "usage": None, "cost": None, "model": None}
        u = e.get("usage") if isinstance(e.get("usage"), dict) else {}
        inp, out = u.get("input_tokens") or 0, u.get("output_tokens") or 0
        usage = {"input_tokens": inp, "output_tokens": out, "total_tokens": u.get("total_tokens") or inp + out} if u else None
        err = e.get("error") if isinstance(e.get("error"), dict) else {}
        error = " ".join(str(x) for x in (err.get("code"), err.get("message")) if x)
        cost = e.get("cost_usd")
        return {"outcome": str(e.get("outcome") or ""), "stop": str(e.get("stop_reason") or ""), "error": error,
                "usage": usage, "cost": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                "model": VibeWorker._export_model(e.get("config"))}

    @staticmethod
    def _export_model(config):
        """The model the run used: export.json's resolved config names the active model's alias ("" = not
        pinned, i.e. vibe's default, which the config does not spell out)."""
        if not isinstance(config, dict):
            return None
        alias = config.get("active_model")
        if not isinstance(alias, str) or not alias:
            return None
        for m in config.get("models") or []:
            if isinstance(m, dict) and m.get("alias") == alias and m.get("name"):
                return str(m["name"])
        return alias

    def parse(self, r, spec):
        messages = self._messages(r["stdout"])
        response, turns = "", 0
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "assistant":
                turns += 1
                t = self._text(m.get("content"))
                if t:
                    response = t
        if messages is None:
            response = r["stdout"].strip()
        x = self._export(spec.get("export_file"))
        ok = r["code"] == 0 and not r["timed_out"] and messages is not None
        if ok:
            status, error = "SUCCESS", ""
        elif r["code"] == 3:
            # stopped on a limit: the work left so far is still collected
            status = {"deadline": "TIME_LIMIT"}.get(x["outcome"], (x["outcome"] or "LIMIT").upper())
            error = ("time limit reached (vibe --time-limit)" if x["outcome"] == "deadline"
                     else "vibe stopped on a limit: %s" % (x["outcome"] or "unknown"))
        else:
            status = (x["outcome"] or "ERROR").upper() + " (exit %s)" % r["code"]
            error = "\n".join(t for t in (x["error"], r["stderr"].strip()[-1500:]) if t) or (
                "unexpected output (no JSON messages)" if r["code"] == 0 else "")
        if not ok and (x["outcome"] or x["stop"]):
            error += "\noutcome: %s%s" % (x["outcome"] or "?", " / stop reason: " + x["stop"] if x["stop"] else "")
        return {"ok": ok, "status": status, "response": response, "error": error.strip(), "usage": x["usage"],
                "cost": x["cost"], "turns": turns, "conversation_id": None, "duration": None, "model": x["model"]}


# ---------------------------------------------------------------- codex (OpenAI Codex CLI)
#
# Flags checked against `codex exec --help` of codex-cli 0.171.0:
#   exec -                       non-interactive run, the prompt read from stdin (no argv length limit)
#   --json                       events as JSONL on stdout: thread.started {thread_id}, item.completed {item: {type:
#                                agent_message|command_execution|file_change|..., text}}, turn.completed {usage:
#                                {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens}},
#                                turn.failed {error: {message}}, error {message}
#   -o, --output-last-message <f> the agent's last message
#   -s, --sandbox read-only|workspace-write|danger-full-access   Codex's own sandbox for the commands it runs
#   -m, --model; -c key=value (TOML) for any config setting (model_reasoning_effort, ...)
#   --ephemeral                  no session files written; --ignore-rules skips user/project execpolicy .rules
# exec never asks for approval: a command its sandbox blocks just fails. workspace-write makes the working directory
# and /tmp writable, keeps .git read-only and turns the network off (network_access below). A project's own
# .codex/config.toml is only read for projects the user trusted: worktrees are never trusted (no --trust).
#
# Codex's sandbox nests inside ours (bwrap): checked on Arch Linux, kernel 7.2, both work together, so both stay
# on. Where the kernel forbids nested user namespaces Codex's sandbox cannot start: "sandbox": false then runs it
# with danger-full-access, which is only allowed when the server's own isolation is on.
#
# Why both sandboxes: Codex's limits what its commands WRITE (workspace and /tmp only, so not ~/.codex either) and
# their network, but lets them read the whole disk; ours hides the home folder, /run and the main repo's objects.
#
# Credentials: "host" (the default) uses your own login in ~/.codex (or $CODEX_HOME), mounted read-write so Codex can
# refresh its token. With cli_auth_credentials_store = "file" the login is auth.json and an isolated run needs no
# D-Bus; with "keyring"/"auto" it goes through the keyring proxy like the other workers. "private-home" gives Codex
# a CODEX_HOME of its own (<state>/homes/codex) with a separate login there, file-based. "token" gives it
# CODEX_API_KEY (an OpenAI API key, honored by `codex exec`).


class CodexWorker(Worker):
    id = "codex"
    label = "OpenAI Codex CLI"
    DEFAULTS = {
        "enabled": False,  # opt-in
        "bin": "codex",
        "daily_budget": 30,
        "max_parallel": 1,  # conservative: one run at a time per ChatGPT login (raise it if you like)
        "credentials": "",
        "sandbox": True,  # Codex's own sandbox (read-only for analyses, workspace-write for jobs)
        "network_access": False,  # network for the commands Codex runs in a job (workspace-write)
        "strengths": ["refactor", "tests", "analysis"],
    }
    QUOTA_RE = re.compile(r"(\b429\b|rate.?limit|quota|too many requests|usage.?limit|usage_limit_reached|"
                          r"insufficient_quota|hit your usage limit)", re.I)
    INSTALL_HINT = "Install OpenAI Codex CLI (https://developers.openai.com/codex/cli) and log in (see the workers tool)."
    TOKEN_ENV = "CODEX_API_KEY"
    TOKEN_HINT = "Use an OpenAI API key (https://platform.openai.com/api-keys)."
    CONFIG_DIRS = [("CODEX_HOME", "~/.codex")]
    PASS_ENV = []
    HOST_ENV = ["CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY"]
    PRIVATE_HOME_ENV = "CODEX_HOME"
    EFFORTS = {"max": "xhigh"}  # capocantiere's levels that Codex names differently

    def home(self):
        if self.credentials == "private-home":
            return self.private_home()
        return env.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")

    def _config_text(self):
        try:
            with open(os.path.join(self.home(), "config.toml"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def login_command(self):
        # the -c makes sure the login goes to auth.json even before config.toml exists
        return "CODEX_HOME=%s %s -c 'cli_auth_credentials_store=\"file\"' login" % (self.private_home(), self.bin)

    def file_store(self):
        return bool(re.search(r'^\s*cli_auth_credentials_store\s*=\s*"file"', self._config_text(), re.M))

    def uses_keyring(self):
        return self.credentials == "host" and not self.file_store()

    def ensure_private_home(self):
        """Create the private home with the one setting it needs. Never touches an existing config."""
        home = self.private_home()
        os.makedirs(home, mode=0o700, exist_ok=True)
        cfg = os.path.join(home, "config.toml")
        if not os.path.exists(cfg):
            fd = os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write('# Codex config for capocantiere runs only (CODEX_HOME). Log in with:\n#   %s\n'
                        'cli_auth_credentials_store = "file"\n' % self.login_command())

    def private_home_problem(self):
        home = self.private_home()
        if not os.path.isdir(home) and not self.enabled:
            return "private home %s not set up: run `mkdir -p -m 700 %s && %s`" % (home, home, self.login_command())
        try:
            self.ensure_private_home()  # only once the worker is enabled: nothing is created for unused workers
        except OSError as e:
            return "cannot create the private home %s: %s" % (home, e)
        if not self.file_store():
            return ('%s/config.toml must set cli_auth_credentials_store = "file" (an isolated run cannot reach the '
                    'keyring): add it, then log in again with `%s`' % (home, self.login_command()))
        auth = os.path.join(home, "auth.json")
        try:
            st = os.stat(auth)
        except OSError:
            return ("not logged in: run `%s` (a ChatGPT account, any plan; add --device-auth without a browser, or "
                    "use --with-api-key)" % self.login_command())
        if os.name == "posix" and st.st_mode & 0o077:
            return "%s is readable by other users: run chmod 600 on it" % auth
        return None

    def private_home_status(self):
        try:
            with open(os.path.join(self.private_home(), "auth.json"), encoding="utf-8") as f:
                mode = json.load(f).get("auth_mode")  # never read the tokens
        except (OSError, ValueError, AttributeError):
            mode = None
        what = {"chatgpt": "ChatGPT login", "apikey": "API key", "api_key": "API key"}.get(str(mode).lower(), "login")
        return True, "%s in private home %s" % (what, self.private_home())

    def host_auth_status(self):
        for var in ("CODEX_API_KEY", "OPENAI_API_KEY"):
            if env.get(var) or self.env().get(var):
                return True, "%s set" % var
        if os.path.exists(os.path.join(self.home(), "auth.json")):
            return True, "logged in, per %s/auth.json" % self.home()
        if self.file_store():
            return False, "not logged in: run `codex login` (no %s/auth.json)" % self.home()
        return None, "no %s/auth.json: the login may be in the keyring, or missing (`codex login status`)" % self.home()

    def blocker(self):
        base = super().blocker()
        if base:
            return base
        if not self.s.get("sandbox") and (self.s.get("isolation") or "none") == "none":
            return '"sandbox": false needs the server\'s isolation (set "isolation"): Codex would run unsandboxed'
        return None

    def warnings(self):
        out = []
        if self.uses_keyring() and (self.s.get("isolation") or "none") != "none":
            out.append('the codex login is in the keyring: isolated runs reach it through the D-Bus proxy, and can read '
                       'the rest of the keyring. Add cli_auth_credentials_store = "file" to %s/config.toml and run '
                       '`codex login` again to keep D-Bus out of the sandbox.' % self.home())
        return out

    def config_model(self):
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', self._config_text(), re.M)
        return m.group(1) if m else None

    def invocation(self, spec):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="capocantiere-codex-")
        last = os.path.join(tmp, "last-message.md")
        job = bool(spec.get("auto_approve"))
        sandbox = ("workspace-write" if job else "read-only") if self.s.get("sandbox") else "danger-full-access"
        a = ["exec", "--json", "-o", last, "--sandbox", sandbox, "--cd", spec["cwd"], "--ephemeral",
             "--color", "never", "--skip-git-repo-check"]
        if self.credentials == "private-home":
            a += ["-c", 'cli_auth_credentials_store="file"']
        if job and self.s.get("sandbox"):
            a += ["-c", "sandbox_workspace_write.network_access=%s" % ("true" if self.s.get("network_access") else "false")]
        if spec.get("model"):
            a += ["--model", spec["model"]]
        if spec.get("effort"):
            a += ["-c", 'model_reasoning_effort="%s"' % self.EFFORTS.get(spec["effort"], spec["effort"])]
        a += list(self.s.get("extra_args") or []) + ["-"]
        return {"argv": a, "env": self.run_env(), "input": spec["prompt"], "cleanup": [tmp], "last_file": last}

    @staticmethod
    def _events(stdout):
        out = []
        for line in (stdout or "").split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    v = json.loads(line)
                except ValueError:
                    continue
                if isinstance(v, dict):
                    out.append(v)
        return out

    def parse(self, r, spec):
        events = self._events(r["stdout"])
        response, errors, thread, turns = "", [], None, 0
        inp = cached = outp = 0
        for e in events:
            t = e.get("type")
            if t == "thread.started":
                thread = e.get("thread_id")
            elif t == "item.completed" and isinstance(e.get("item"), dict):
                it = e["item"]
                if it.get("type") == "agent_message" and it.get("text"):
                    response = it["text"]
                    turns += 1
            elif t == "turn.completed" and isinstance(e.get("usage"), dict):
                u = e["usage"]
                inp += u.get("input_tokens") or 0
                cached += u.get("cached_input_tokens") or 0
                outp += u.get("output_tokens") or 0
            elif t == "turn.failed":
                err = e.get("error")
                errors.append(str(err.get("message") if isinstance(err, dict) else err or "turn failed"))
            elif t == "error" and e.get("message"):
                errors.append(str(e["message"]))
        try:
            with open(spec.get("last_file") or "", encoding="utf-8") as f:
                response = f.read().strip() or response
        except OSError:
            pass
        ok = r["code"] == 0 and not r["timed_out"] and not any(e.get("type") == "turn.failed" for e in events)
        usage = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": outp,
                 "total_tokens": inp + outp} if (inp or outp) else None
        error = ""
        if not ok:
            error = "\n".join(dict.fromkeys(errors)) or r["stderr"].strip()[-1500:] or (
                "no events from codex" if not events else "")
        return {"ok": ok, "status": "SUCCESS" if ok else "ERROR (exit %s)" % r["code"], "response": response,
                "error": error, "usage": usage, "cost": None, "turns": turns, "conversation_id": thread,
                "duration": None, "model": spec.get("model") or self.config_model()}


# Registry. The preference order comes from the config ("order").
ADAPTERS = {"copilot": CopilotWorker, "vibe": VibeWorker, "codex": CodexWorker, "agy": AgyWorker}
