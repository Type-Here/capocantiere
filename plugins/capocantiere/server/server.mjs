#!/usr/bin/env node
// capocantiere: MCP server that lets Claude Code (the "manager") delegate work to external
// coding-agent CLIs ("workers": GitHub Copilot CLI, Mistral Vibe, Antigravity CLI).
//
// Node port of server.py (+ common.py, workers.py), with the same behavior and the same state files,
// so you can switch between the two runtimes without losing jobs. The Python version is the
// reference covered by tests/e2e.py; keep the two in sync.
//
// - Every code-writing task runs in an isolated git worktree, on a dedicated branch.
//   The main repo is never touched until Claude calls `apply`.
// - Jobs are asynchronous and can run in parallel (with a queue, and a limit per worker).
// - Usage is tracked per worker so each one's daily budget isn't exceeded. On quota/rate-limit
//   errors the job can move on to the next worker (fallback).
//
// Zero dependencies: only Node >= 18 and git are required.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

const VERSION = "2.0.0";
const TERMINAL = new Set(["done", "no_changes", "failed", "applied", "discarded", "interrupted"]);
const env = process.env;

// ================================================================ common (common.py)

function num(v, d) {
  if (v === undefined || v === null || v === "") return d;
  const n = Number(v);
  return Number.isNaN(n) ? d : n;
}
function bool(v, d) {
  if (v === undefined || v === null || v === "") return d;
  if (typeof v === "boolean") return v;
  return /^(1|true|yes|on)$/i.test(String(v));
}
const xdg = (v, ...dflt) => env[v] || path.join(os.homedir(), ...dflt);

// New names (CAPOCANTIERE_*) win; the old GEMINI_DELEGATE_* variables are still honored.
const STATE_DIR =
  env.CAPOCANTIERE_STATE_DIR || env.GEMINI_DELEGATE_STATE_DIR || path.join(xdg("XDG_STATE_HOME", ".local", "state"), "capocantiere");
const WORKTREE_DIR =
  env.CAPOCANTIERE_WORKTREE_DIR || env.GEMINI_DELEGATE_WORKTREE_DIR || path.join(xdg("XDG_CACHE_HOME", ".cache"), "capocantiere", "worktrees");
const CONFIG_FILE = env.CAPOCANTIERE_CONFIG || path.join(xdg("XDG_CONFIG_HOME", ".config"), "capocantiere", "workers.json");
const LEGACY_STATE_DIR = path.join(xdg("XDG_STATE_HOME", ".local", "state"), "gemini-delegate");

const JOBS_DIR = path.join(STATE_DIR, "jobs");
const PATCH_DIR = path.join(STATE_DIR, "patches");
const USAGE_FILE = path.join(STATE_DIR, "usage.jsonl");
const SETTINGS_FILE = path.join(STATE_DIR, "settings.json");

function readJson(file, dflt) {
  try {
    const v = JSON.parse(fs.readFileSync(file, "utf8"));
    return v && typeof v === "object" && !Array.isArray(v) ? v : dflt;
  } catch {
    return dflt;
  }
}

function writeJson(file, data) {
  const tmp = file + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2));
  fs.renameSync(tmp, file);
}

// Move the state of the old gemini-delegate server to the new folder, once.
// Old worktrees stay where they are: git keeps their absolute path, and the jobs point to it.
function migrateLegacyState() {
  if (fs.existsSync(STATE_DIR) || !fs.existsSync(LEGACY_STATE_DIR)) return;
  if (env.CAPOCANTIERE_STATE_DIR || env.GEMINI_DELEGATE_STATE_DIR) return; // explicit state dir: don't touch anything
  fs.mkdirSync(path.dirname(STATE_DIR), { recursive: true });
  try {
    fs.renameSync(LEGACY_STATE_DIR, STATE_DIR);
  } catch {
    fs.cpSync(LEGACY_STATE_DIR, STATE_DIR, { recursive: true });
  }
  const jobsDir = path.join(STATE_DIR, "jobs");
  const oldPatches = path.join(LEGACY_STATE_DIR, "patches");
  for (const f of fs.existsSync(jobsDir) ? fs.readdirSync(jobsDir) : []) {
    const p = path.join(jobsDir, f);
    const j = readJson(p, null);
    if (!j) continue;
    if ((j.patchFile || "").startsWith(oldPatches)) j.patchFile = PATCH_DIR + j.patchFile.slice(oldPatches.length);
    if (!j.worker) j.worker = "agy"; // every job before workers existed ran on agy
    writeJson(p, j);
  }
}

function ensureDirs() {
  migrateLegacyState();
  for (const d of [JOBS_DIR, PATCH_DIR, WORKTREE_DIR]) fs.mkdirSync(d, { recursive: true });
}

const isoNow = () => new Date().toISOString();
const truncate = (s, n) =>
  !s ? "" : s.length <= n ? s : s.slice(0, n) + `\n… [truncated: ${s.length - n} characters omitted]`;
const tail = (s, n) => (!s ? "" : s.length <= n ? s : "…" + s.slice(-n));
const pad2 = (x) => String(x).padStart(2, "0");

function newId() {
  const d = new Date();
  return `j${pad2(d.getMonth() + 1)}${pad2(d.getDate())}-${pad2(d.getHours())}${pad2(d.getMinutes())}-${crypto.randomBytes(2).toString("hex")}`;
}

// Signal the child and everything it started (its own process group on POSIX).
function killTree(child, sig) {
  try {
    if (process.platform !== "win32") process.kill(-child.pid, sig);
    else child.kill(sig);
  } catch {
    try { child.kill(sig); } catch {}
  }
}

// Run a command; never throws. Resolves to { code, stdout, stderr, error, timedOut }.
function run(cmd, args, { cwd, input, timeoutMs, onSpawn, binary = false, extraEnv } = {}) {
  return new Promise((resolve) => {
    let child;
    let done = false;
    const finish = (r) => { if (!done) { done = true; resolve(r); } };
    try {
      child = spawn(cmd, args, {
        cwd,
        env: extraEnv && Object.keys(extraEnv).length ? { ...process.env, ...extraEnv } : process.env,
        stdio: ["pipe", "pipe", "pipe"],
        detached: process.platform !== "win32",
      });
    } catch (e) {
      return finish({ code: -1, stdout: binary ? Buffer.alloc(0) : "", stderr: String(e), error: e, timedOut: false });
    }
    onSpawn?.(child);
    const out = [];
    const err = [];
    let timedOut = false;
    let timer;
    if (timeoutMs) {
      timer = setTimeout(() => {
        timedOut = true;
        killTree(child, "SIGTERM");
        setTimeout(() => killTree(child, "SIGKILL"), 5000).unref();
      }, timeoutMs);
    }
    const collect = () => {
      const o = Buffer.concat(out);
      return { stdout: binary ? o : o.toString("utf8"), stderr: Buffer.concat(err).toString("utf8") };
    };
    child.stdout.on("data", (d) => out.push(d));
    child.stderr.on("data", (d) => err.push(d));
    child.stdin.on("error", () => {}); // EPIPE when the process exits without reading its input
    child.on("error", (e) => {
      clearTimeout(timer);
      const c = collect();
      finish({ code: -1, stdout: c.stdout, stderr: c.stderr + String(e), error: e, timedOut });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      finish({ code: code ?? -1, ...collect(), error: null, timedOut });
    });
    if (input !== undefined && input !== null) child.stdin.end(input);
    else child.stdin.end();
  });
}

const notFound = (r) => r.error?.code === "ENOENT";

async function git(cwd, ...args) {
  return gitEnv(cwd, undefined, ...args);
}

async function gitEnv(cwd, extraEnv, ...args) {
  const r = await run("git", args, { cwd, extraEnv });
  if (r.code !== 0) throw new Error(`git ${args.join(" ")} failed: ${r.stderr.trim() || r.stdout.trim()}`);
  return r.stdout;
}

// serialize git operations that touch worktrees/branches
let gitLock = Promise.resolve();
function withGitLock(fn) {
  const p = gitLock.then(fn, fn);
  gitLock = p.catch(() => {});
  return p;
}

async function repoRoot(dir) {
  const start = dir || env.CLAUDE_PROJECT_DIR || process.cwd();
  const r = await run("git", ["rev-parse", "--show-toplevel"], { cwd: start });
  if (r.code !== 0) throw new Error(`"${start}" is not a git repository. capocantiere requires git.`);
  return r.stdout.trim();
}

async function currentRepo() {
  try { return await repoRoot(); } catch { return null; }
}

function tmpDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

// Sum every numeric value whose key matches pattern, anywhere in a JSON structure.
function sumKeys(obj, pattern) {
  let total = 0;
  if (Array.isArray(obj)) {
    for (const v of obj) total += sumKeys(v, pattern);
  } else if (obj && typeof obj === "object") {
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === "number" && pattern.test(k)) total += v;
      else total += sumKeys(v, pattern);
    }
  }
  return total;
}

// ================================================================ config (workers.py)

const GLOBAL_DEFAULTS = {
  order: ["copilot", "vibe", "agy"], // preference order when no worker is requested
  fallback: true, // on quota/rate-limit errors or exhausted budget, move on to the next worker
  max_parallel: 4, // jobs running at once, across all workers
  isolation: "none", // Python server only (isolation.py): the Node server refuses any other value
};

// Settings every worker has. Adapters add their own on top.
const COMMON_DEFAULTS = {
  enabled: true,
  bin: "",
  model: "", // empty = the CLI's default
  analyze_model: "", // empty = same as model
  effort: "",
  daily_budget: 50, // runs per 24h
  max_parallel: 2,
  timeout_min: 20,
  analyze_timeout_min: 8,
  extra_args: [],
  env: {}, // extra environment variables for the CLI (e.g. an API key variable)
  strengths: [],
};

// Old environment variables of the gemini-delegate server, mapped to agy settings.
const LEGACY_AGY_ENV = {
  GEMINI_DELEGATE_AGY_BIN: "bin",
  GEMINI_DELEGATE_MODEL: "model",
  GEMINI_DELEGATE_ANALYZE_MODEL: "analyze_model",
  GEMINI_DELEGATE_EFFORT: "effort",
  GEMINI_DELEGATE_DAILY_BUDGET: "daily_budget",
  GEMINI_DELEGATE_MAX_PARALLEL: "max_parallel",
  GEMINI_DELEGATE_TASK_TIMEOUT_MIN: "timeout_min",
  GEMINI_DELEGATE_ANALYZE_TIMEOUT_MIN: "analyze_timeout_min",
  GEMINI_DELEGATE_SANDBOX: "sandbox",
  GEMINI_DELEGATE_SKIP_PERMISSIONS: "skip_permissions",
  GEMINI_DELEGATE_EXTRA_ARGS: "extra_args",
};

// Convert an environment string to the type of the default value.
function coerce(value, like) {
  if (typeof like === "boolean") return bool(value, like);
  if (typeof like === "number") return num(value, like);
  if (Array.isArray(like)) {
    const sep = value.includes(",") ? "," : " ";
    return value.split(sep).map((x) => x.trim()).filter(Boolean);
  }
  if (like && typeof like === "object") {
    try {
      const v = JSON.parse(value);
      return v && typeof v === "object" && !Array.isArray(v) ? v : like;
    } catch {
      return like;
    }
  }
  return value;
}

function mergeSettings(defaults, override) {
  const out = { ...defaults };
  for (const [k, v] of Object.entries(override || {})) {
    if (k.startsWith("_")) continue; // comments
    out[k] = v;
  }
  return out;
}

const configCache = { mtime: undefined, data: null };

// The effective config: { order, fallback, max_parallel, workers: {id: settings}, errors: [...] }.
// The file is re-read when it changes, so edits apply without restarting the session.
function loadConfig() {
  let mtime = null;
  try { mtime = fs.statSync(CONFIG_FILE).mtimeMs; } catch {}
  if (configCache.data && configCache.mtime === mtime) return configCache.data;
  const errors = [];
  let user = {};
  if (mtime !== null) {
    try {
      user = JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
      if (!user || typeof user !== "object" || Array.isArray(user)) throw new Error("the top level must be an object");
    } catch (e) {
      errors.push(`${CONFIG_FILE} is not valid JSON (${e.message}): using the built-in defaults.`);
      user = {};
    }
  }
  const { workers: userWorkersRaw, ...userGlobal } = user;
  const cfg = mergeSettings(GLOBAL_DEFAULTS, userGlobal);
  for (const [key, like] of Object.entries(GLOBAL_DEFAULTS)) {
    const v = env["CAPOCANTIERE_" + key.toUpperCase()];
    if (v) cfg[key] = coerce(v, like);
  }
  const userWorkers = userWorkersRaw && typeof userWorkersRaw === "object" ? userWorkersRaw : {};
  for (const wid of Object.keys(userWorkers)) {
    if (!ADAPTERS[wid] && !wid.startsWith("_"))
      errors.push(`Unknown worker "${wid}" in ${CONFIG_FILE} (known: ${Object.keys(ADAPTERS).join(", ")}).`);
  }
  const workers = {};
  for (const [wid, cls] of Object.entries(ADAPTERS)) {
    const defaults = mergeSettings(COMMON_DEFAULTS, cls.DEFAULTS);
    const s = mergeSettings(defaults, userWorkers[wid]);
    if (wid === "agy") {
      for (const [v, key] of Object.entries(LEGACY_AGY_ENV)) if (env[v]) s[key] = coerce(env[v], defaults[key]);
    }
    for (const [key, like] of Object.entries(defaults)) {
      const v = env[`CAPOCANTIERE_${wid.toUpperCase()}_${key.toUpperCase()}`];
      if (v) s[key] = coerce(v, like);
    }
    workers[wid] = s;
  }
  // Isolation of worker processes is implemented in the Python server only (isolation.py). Refuse rather
  // than run a worker without the isolation the user asked for.
  for (const [where, v] of [["global", userGlobal.isolation], ...Object.entries(userWorkers).map(([w, s]) => [w, s?.isolation])]) {
    if (v && v !== "none")
      errors.push(`"isolation": "${v}" (${where}) is only supported by the Python server; the Node server will not run those workers.`);
  }
  for (const [w, s] of Object.entries(userWorkers)) {
    if (s?.credentials && s.credentials !== "host")
      errors.push(`"credentials": "${s.credentials}" (${w}) is only supported by the Python server; the Node server will not run that worker.`);
  }
  if (userGlobal.selection !== undefined && userGlobal.selection !== "order")
    errors.push(`"selection": "${userGlobal.selection}" is only supported by the Python server; the Node server always uses the configured order.`);
  if (Object.prototype.hasOwnProperty.call(userGlobal, "half_life_days"))
    errors.push(`"half_life_days" is only supported by the Python server (trust scores); ignored.`);
  for (const [w, s] of Object.entries(userWorkers)) {
    if (s && typeof s === "object" && Object.prototype.hasOwnProperty.call(s, "tiers"))
      errors.push(`"tiers" (${w}) is only supported by the Python server; the Node server uses the worker's model setting.`);
  }
  const order = Array.isArray(cfg.order) ? cfg.order : GLOBAL_DEFAULTS.order;
  cfg.order = [...order.filter((w) => ADAPTERS[w]), ...Object.keys(ADAPTERS).filter((w) => !order.includes(w))];
  cfg.workers = workers;
  cfg.errors = errors;
  configCache.mtime = mtime;
  configCache.data = cfg;
  return cfg;
}

function getWorker(wid) {
  const cfg = loadConfig();
  if (!ADAPTERS[wid]) throw new Error(`Unknown worker "${wid}". Workers: ${Object.keys(ADAPTERS).join(", ")}.`);
  return new ADAPTERS[wid](cfg.workers[wid]);
}

function allWorkers() {
  const cfg = loadConfig();
  return cfg.order.map((w) => new ADAPTERS[w](cfg.workers[w]));
}

// The config file with every setting at its default.
function exampleConfig() {
  const out = { _comment: "Capocantiere workers. Remove what you don't change; missing keys use the defaults.", ...GLOBAL_DEFAULTS };
  out.workers = Object.fromEntries(Object.entries(ADAPTERS).map(([wid, cls]) => [wid, mergeSettings(COMMON_DEFAULTS, cls.DEFAULTS)]));
  return out;
}

// ================================================================ worker interface

const versionCache = new Map(); // bin -> { at, installed, text }

class Worker {
  // Subclasses set the statics and implement invocation() and parse().
  static ID = "";
  static LABEL = "";
  static DEFAULTS = {};
  static QUOTA_RE = /(\b429\b|rate.?limit|quota|too many requests)/i;
  static WARNING = ""; // shown in `workers` and at session start
  static OWN_TIMEOUT = false; // true if the CLI enforces the timeout itself (then the server adds a grace minute)
  static INSTALL_HINT = "";

  constructor(settings) {
    this.s = settings;
  }
  get id() { return this.constructor.ID; }
  get label() { return this.constructor.LABEL; }
  get quotaRe() { return this.constructor.QUOTA_RE; }
  get ownTimeout() { return this.constructor.OWN_TIMEOUT; }
  get installHint() { return this.constructor.INSTALL_HINT; }
  get enabled() { return !!this.s.enabled; }
  get bin() { return this.s.bin || this.id; }

  // { installed, text }: first line of --version. Cached for a minute.
  async version() {
    const hit = versionCache.get(this.bin);
    if (hit && Date.now() - hit.at < 60_000) return hit;
    const r = await run(this.bin, ["--version"], { timeoutMs: 15_000, extraEnv: this.env() });
    const v = notFound(r)
      ? { at: Date.now(), installed: false, text: "not found" }
      : { at: Date.now(), installed: true, text: (r.stdout || r.stderr).trim().split("\n")[0] || "ok" };
    versionCache.set(this.bin, v);
    return v;
  }

  // [true/false/null, note]. null = cannot be checked without a real call.
  authStatus() { return [null, "not verifiable without a real call"]; }

  // Problems in the CLI's own setup that the user should fix (shown by `workers`).
  warnings() { return this.constructor.WARNING ? [this.constructor.WARNING] : []; }

  // A reason why runs would certainly fail (the server then skips this worker), or null.
  blocker() {
    const id = this.id.toUpperCase();
    const iso = process.env[`CAPOCANTIERE_${id}_ISOLATION`] || this.s.isolation || loadConfig().isolation;
    if (iso && iso !== "none") return `isolation "${iso}" is only supported by the Python server`;
    const cred = process.env[`CAPOCANTIERE_${id}_CREDENTIALS`] || this.s.credentials;
    if (cred && cred !== "host") return `credentials "${cred}" are only supported by the Python server`;
    return null;
  }

  env() {
    return Object.fromEntries(Object.entries(this.s.env || {}).map(([k, v]) => [String(k), String(v)]));
  }

  // { model, effort, source }. model "" means the CLI's default.
  async resolveModel(role, requested, effort) {
    const candidates = [[requested, "requested"], [role === "analyze" ? this.s.analyze_model : null, "config"], [this.s.model, "config"]];
    for (const [value, src] of candidates) {
      if (value && value !== "auto") return { model: value, effort: effort || this.s.effort || "", source: src };
    }
    return { model: "", effort: effort || this.s.effort || "", source: "CLI default" };
  }

  timeoutMin(role) { return role === "analyze" ? this.s.analyze_timeout_min : this.s.timeout_min; }

  // spec: role, prompt, cwd, model, effort, timeout_min, auto_approve. Returns { argv, env, input, cleanup, ... }.
  invocation(spec) { throw new Error("not implemented"); }

  // r is the result of run(). Returns ok/status/response/error/usage/cost/turns/conversation_id/duration.
  parse(r, spec) { throw new Error("not implemented"); }

  softDenied(r) {
    return (r.stderr || "").split("\n").filter((l) => /denied|not approved|requires approval|soft-den/i.test(l)).slice(0, 10);
  }

  // Extra text after a failed run (e.g. a model marked unavailable).
  failureNote(model, error) { return ""; }

  async modelsLine() {
    const parts = [];
    for (const role of ["analyze", "implement"]) {
      try {
        const r = await this.resolveModel(role);
        parts.push(`${role} → ${r.model || "CLI default"}` + (r.source === "CLI default" ? "" : ` (${r.source})`));
      } catch (e) {
        parts.push(`${role} → ERROR: ${e.message}`);
      }
    }
    return parts.join("; ");
  }
}

// ---------------------------------------------------------------- agy (Antigravity CLI)
//
// The model list comes from `agy models`, which only shows what the account can use, so new
// models are picked up without updating the plugin. Slugs are sorted into capability groups by
// the rules in model-groups.json.

const MODEL_RULES = JSON.parse(fs.readFileSync(new URL("./model-groups.json", import.meta.url), "utf8"));
const GROUP_IDS = MODEL_RULES.groups.map((g) => g.id);
const OTHER_GROUP = { id: "other", description: "Not classified. Never chosen automatically, only when requested by slug or group." };
const MODELS_FILE = path.join(STATE_DIR, "models.json");
const MODELS_TTL_MS = 6 * 3600_000;
const LEVELS = ["low", "medium", "high", "xhigh", "max"];
const LEVEL_RE = new RegExp(`-(${LEVELS.join("|")})$`);
const MODEL_ERROR_RE =
  /not recognized as a known model|model.{0,80}(not available|unavailable|not allowed|not supported|not entitled|permission denied)/i;

// `agy models` prints one "slug<TAB>Display name" line per model.
function parseModels(stdout) {
  const out = [];
  for (const line of stdout.split("\n")) {
    const i = line.indexOf("\t");
    if (i < 0) continue;
    const slug = line.slice(0, i).trim();
    if (slug) out.push({ slug, name: line.slice(i + 1).trim() || slug });
  }
  return out;
}

const globRe = (p) => new RegExp("^" + p.split("*").map((x) => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join(".*") + "$", "i");

function groupOf(slug) {
  for (const g of MODEL_RULES.groups)
    for (let i = 0; i < g.match.length; i++) if (new RegExp(g.match[i], "i").test(slug)) return [g.id, i];
  return ["other", 0];
}

// Newest first: "Gemini 3.8 Flash" -> [-3, -8, 0, 0].
function versionKey(name) {
  const m = name.match(/\d+(?:\.\d+)*/);
  const parts = m ? m[0].split(".").slice(0, 4).map(Number) : [];
  while (parts.length < 4) parts.push(0);
  return parts.map((x) => -x);
}

function compareRank(a, b) {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
}

// Usable models by group, best first, plus the ones excluded by settings.
function catalog(cache, settings) {
  const unavailable = cache.unavailable || {};
  const excl = (settings.exclude || []).map(globRe);
  const groups = Object.fromEntries([...GROUP_IDS, "other"].map((g) => [g, []]));
  const excluded = [];
  for (const m of cache.models || []) {
    if (unavailable[m.slug]) continue;
    if (excl.some((rx) => rx.test(m.slug))) { excluded.push(m); continue; }
    const [gid, idx] = groupOf(m.slug);
    groups[gid].push({ ...m, rank: [idx, ...versionKey(m.name)] });
  }
  for (const list of Object.values(groups)) list.sort((x, y) => compareRank(x.rank, y.rank));
  return { groups, excluded };
}

function groupsHint(cat) {
  const parts = Object.entries(cat.groups).filter(([, ms]) => ms.length).map(([g, ms]) => `${g}: ${ms.map((m) => m.slug).join(", ")}`);
  return `Available — ${parts.join("; ") || "none"}. See the models tool.`;
}

let modelFetch = null;

class AgyWorker extends Worker {
  static ID = "agy";
  static LABEL = "Antigravity CLI (Gemini)";
  static DEFAULTS = {
    enabled: false,
    bin: "agy",
    daily_budget: 150,
    max_parallel: 3,
    // How agy authenticates (https://antigravity.google/docs/install.md):
    //   "api_key": Gemini API key from AI Studio in GEMINI_API_KEY; each run gets
    //              --settings '{"modelProvider":"gemini"}' (per session, never saved)
    //   "adc":     Google Cloud Application Default Credentials (AGY_ADC_AUTH=true)
    //   "login":   the signed-in Google account (consumer subscription): see WARNING
    auth: "api_key",
    sandbox: true, // pass --sandbox
    // Inside the isolated worktree agy may edit files and run commands without asking:
    // it's the only way to work headless. The main repo stays protected.
    skip_permissions: true,
    strengths: ["analysis", "boilerplate", "tests", "docs"],
  };
  static QUOTA_RE = /(429|RESOURCE_EXHAUSTED|rate ?limit|quota)/i;
  static OWN_TIMEOUT = true; // agy stops itself (--print-timeout): the server only kills it after a grace minute
  static WARNING =
    "According to the official Antigravity FAQ, running `agy -p` inside third-party agents is not " +
    "supported on consumer plans and can get the account suspended. Enable this worker only with " +
    '"auth": "api_key" (a Gemini API key from AI Studio, the default) or "adc" (Google Cloud), never ' +
    'with "login" on a consumer subscription.';
  static INSTALL_HINT = "Install Antigravity CLI: https://antigravity.google/docs/cli";
  static AUTH_MODES = ["api_key", "adc", "login"];

  env() {
    const e = super.env();
    if (this.s.auth === "adc" && !("AGY_ADC_AUTH" in e)) e.AGY_ADC_AUTH = "true";
    return e;
  }

  blocker() {
    const base = super.blocker();
    if (base) return base;
    const auth = this.s.auth;
    if (!AgyWorker.AUTH_MODES.includes(auth)) return `unknown auth "${auth}" (use ${AgyWorker.AUTH_MODES.join(", ")})`;
    if (auth === "api_key" && !(env.GEMINI_API_KEY || super.env().GEMINI_API_KEY))
      return 'auth is "api_key" but GEMINI_API_KEY is not set (export it, or put it under "env" in the config)';
    return null;
  }

  authStatus() {
    const problem = this.blocker();
    if (problem) return [false, problem];
    if (this.s.auth === "api_key") return [true, "Gemini API key (GEMINI_API_KEY)"];
    if (this.s.auth === "adc") return [null, "Google Cloud ADC (AGY_ADC_AUTH=true): not verifiable without a real call"];
    return [null, "signed-in Google account: not verifiable without a real call"];
  }

  warnings() {
    return !this.enabled || this.s.auth === "login" ? [AgyWorker.WARNING] : [];
  }

  // -- models

  // The cached model list, re-read from `agy models` when stale. Never throws.
  async modelCache(refresh = false) {
    const cache = readJson(MODELS_FILE, {});
    if (!refresh && cache.models?.length && Date.now() - (cache.fetchedAt || 0) <= MODELS_TTL_MS) return cache;
    if (!modelFetch) {
      modelFetch = (async () => {
        const r = await run(this.bin, ["models"], { timeoutMs: 30_000, extraEnv: this.env() });
        const models = r.code === 0 ? parseModels(r.stdout) : [];
        const current = readJson(MODELS_FILE, {});
        if (models.length) {
          // an explicit refresh also forgets models marked unavailable
          const fresh = { fetchedAt: Date.now(), models, unavailable: refresh ? {} : current.unavailable || {} };
          writeJson(MODELS_FILE, fresh);
          return fresh;
        }
        return { ...current, fetchError: (r.stderr || r.stdout).trim().slice(0, 500) || "`agy models` failed" };
      })().finally(() => { modelFetch = null; });
    }
    return modelFetch;
  }

  markUnavailable(slug) {
    const cache = readJson(MODELS_FILE, {});
    cache.unavailable = { ...(cache.unavailable || {}), [slug]: isoNow() };
    writeJson(MODELS_FILE, cache);
  }

  // Precedence: requested (tool argument) > worker config > settings (models tool) > automatic.
  // Each of them may be a slug or a group id; a group follows new models automatically.
  async resolveModel(role, requested, effort) {
    const cache = await this.modelCache();
    const settings = readJson(SETTINGS_FILE, {});
    const cat = catalog(cache, settings);
    const known = new Set((cache.models || []).map((m) => m.slug));
    const cfgModel = role === "analyze" ? this.s.analyze_model || this.s.model : this.s.model;
    let choice = null;
    let source = null;
    for (const [value, src] of [[requested, "requested"], [cfgModel, "config"], [settings[role], "settings"]]) {
      if (value && value !== "auto") { choice = value; source = src; break; }
    }
    let model;
    if (choice === null) {
      model = "";
      source = "agy default";
      for (const gid of MODEL_RULES.defaults[role]) {
        if (cat.groups[gid].length) { model = cat.groups[gid][0].slug; source = `auto, group ${gid}`; break; }
      }
    } else if (Object.hasOwn(cat.groups, choice)) {
      if (!cat.groups[choice].length) throw new Error(`No agy model available in group "${choice}". ${groupsHint(cat)}`);
      model = cat.groups[choice][0].slug;
      source = `${source}, group ${choice}`;
    } else if (!known.size || known.has(choice)) {
      model = choice; // list unknown (agy models failed): pass it through and let agy decide
    } else {
      throw new Error(`Unknown agy model "${choice}". ${groupsHint(cat)}`);
    }

    effort = effort || this.s.effort || "";
    if (effort && model && LEVEL_RE.test(model)) {
      // the level is part of the slug and agy rejects --effort with it:
      // pick the variant of the same model closest to the requested level
      const base = model.replace(LEVEL_RE, "");
      const want = LEVELS.indexOf(effort);
      const variants = LEVELS.filter((lv) => known.has(`${base}-${lv}`));
      if (want >= 0 && variants.length) {
        const dist = (lv) => [Math.abs(LEVELS.indexOf(lv) - want), -LEVELS.indexOf(lv)];
        variants.sort((x, y) => compareRank(dist(x), dist(y)));
        model = `${base}-${variants[0]}`;
      }
      effort = "";
    }
    return { model, effort, source };
  }

  async modelsReport(refresh = false) {
    const cache = await this.modelCache(refresh);
    if (!cache.models?.length)
      return `Could not read the agy model list (${cache.fetchError || "no output from `agy models`"}). Runs will use agy's default model.`;
    const settings = readJson(SETTINGS_FILE, {});
    const cat = catalog(cache, settings);
    const d = new Date(cache.fetchedAt);
    const when = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    const lines = [`Models this account can use through agy (read ${when}):`];
    const width = Math.max(...cache.models.map((m) => m.slug.length));
    for (const g of [...MODEL_RULES.groups, OTHER_GROUP]) {
      const ms = cat.groups[g.id];
      if (g === OTHER_GROUP && !ms.length) continue;
      lines.push("", `${g.id} — ${g.description}`);
      if (!ms.length) lines.push("    (none available)");
      ms.forEach((m, i) => lines.push(`  ${i === 0 ? "*" : " "} ${m.slug.padEnd(width)}  ${m.name}`));
    }
    lines.push("", "* = the model used when that group is requested.");
    if (cat.excluded.length) lines.push(`Excluded by settings: ${cat.excluded.map((m) => m.slug).join(", ")}`);
    if (Object.keys(cache.unavailable || {}).length)
      lines.push(`Marked unavailable after failing for this account (a refresh clears this): ${Object.keys(cache.unavailable).sort().join(", ")}`);
    lines.push("", `In use: ${await this.modelsLine()}`);
    lines.push(`Settings file: ${SETTINGS_FILE} (exclude: ${(settings.exclude || []).join(", ") || "none"})`);
    return lines.join("\n");
  }

  async updateModelSettings(a) {
    const settings = readJson(SETTINGS_FILE, {});
    const known = new Set(((await this.modelCache()).models || []).map((m) => m.slug));
    for (const role of ["analyze", "implement"]) {
      if (!(role in a)) continue;
      const v = (a[role] || "").trim();
      if (!v || v === "auto") delete settings[role];
      else if (GROUP_IDS.includes(v) || v === "other" || !known.size || known.has(v)) settings[role] = v;
      else throw new Error(`Unknown model or group "${v}". Groups: ${[...GROUP_IDS, "other"].join(", ")}.`);
    }
    if ("exclude" in a) settings.exclude = (a.exclude || []).map(String).filter((x) => x.trim());
    writeJson(SETTINGS_FILE, settings);
  }

  // If agy rejected the model for this account, remember it so the next runs pick another.
  failureNote(model, error) {
    if (!model || !MODEL_ERROR_RE.test(error || "")) return "";
    this.markUnavailable(model);
    return `\nModel ${model} has been marked unavailable; the next runs will pick another one (models refresh=true clears this).`;
  }

  // -- runs

  invocation(spec) {
    const a = ["-p", spec.prompt, "--output-format", "json"];
    if (this.s.auth === "api_key") a.push("--settings", JSON.stringify({ modelProvider: "gemini" }));
    if (spec.model) a.push("--model", spec.model);
    if (spec.effort) a.push("--effort", spec.effort);
    if (spec.timeout_min) a.push("--print-timeout", `${spec.timeout_min}m`);
    if (spec.auto_approve && this.s.skip_permissions) a.push("--dangerously-skip-permissions");
    if (this.s.sandbox) a.push("--sandbox");
    return { argv: [...a, ...(this.s.extra_args || [])], env: this.env(), input: null };
  }

  static envelope(stdout) {
    for (const l of stdout.trim().split("\n").reverse()) {
      const t = l.trim();
      if (!t.startsWith("{")) continue;
      try {
        const v = JSON.parse(t);
        if (v && typeof v === "object" && !Array.isArray(v)) return v;
      } catch {}
    }
    try {
      const v = JSON.parse(stdout);
      return v && typeof v === "object" && !Array.isArray(v) ? v : null;
    } catch {
      return null;
    }
  }

  parse(r, spec) {
    const envl = AgyWorker.envelope(r.stdout) || {};
    const hasEnvl = Object.keys(envl).length > 0;
    const status = envl.status || (r.code === 0 ? "SUCCESS" : "ERROR");
    const ok = r.code === 0 && status === "SUCCESS" && !r.timedOut;
    let response = envl.response;
    if (response === undefined || response === null) response = hasEnvl ? "" : r.stdout.trim();
    return {
      ok,
      status,
      response,
      error: envl.error ? String(envl.error) : "",
      usage: envl.usage || null,
      turns: envl.num_turns || 0,
      conversation_id: envl.conversation_id || null,
      duration: envl.duration_seconds ?? null,
    };
  }
}

// ---------------------------------------------------------------- copilot (GitHub Copilot CLI)
//
// Flags checked against `copilot --help` of GitHub Copilot CLI 1.0.114:
//   -p <prompt>                 non-interactive run; tool requests that would prompt are denied
//   -s                          print only the agent's response
//   --allow-all-tools           auto-approve tools; WITHOUT --allow-all-paths file access stays
//                               limited to the working directory (and the temp dir)
//   --deny-tool <rule>          wins over every allow
//   --usage-output-file <file>  final usage as JSON (totalNanoAiu, totalPremiumRequestCost, ...)
// An untrusted folder in -p mode only skips its MCP servers and repo hooks, so worktrees need no trust
// (and not trusting them keeps a repo's hooks from running).

class CopilotWorker extends Worker {
  static ID = "copilot";
  static LABEL = "GitHub Copilot CLI";
  static DEFAULTS = {
    bin: "copilot",
    daily_budget: 40,
    max_parallel: 2,
    // Rules always denied while auto-approving in the worktree (shell commands are not limited to the
    // working directory by path checks, so the dangerous ones are named here).
    deny_tools: ["shell(git push)", "shell(git remote)", "shell(git config)", "shell(git worktree)",
      "shell(git branch)", "shell(git checkout)", "shell(git switch)", "shell(git reset)",
      "shell(git rebase)", "shell(gh)", "shell(sudo)"],
    disable_builtin_mcps: true, // the GitHub MCP server could act on the user's GitHub account
    sandbox: false, // --sandbox: OS-level sandbox for shell commands (Linux: needs bwrap, slirp4netns, ...)
    max_ai_credits: 0, // --max-ai-credits per run; 0 = no limit
    // Hide other workers' secrets from the agent's shell commands.
    secret_env_vars: ["MISTRAL_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"],
    strengths: ["tests", "boilerplate", "refactor", "docs"],
  };
  static QUOTA_RE =
    /(quota|rate.?limit|\b429\b|too many requests|100% used|premium requests?.{0,40}(limit|exhausted|exceeded)|exceeded.{0,40}(limit|credits|allowance)|out of (ai )?credits)/i;
  static INSTALL_HINT = "Install GitHub Copilot CLI (https://docs.github.com/copilot/how-tos/copilot-cli) and run `copilot login`.";

  authStatus() {
    for (const v of ["COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"]) {
      if (env[v] || this.env()[v]) return [true, `token in ${v}`];
    }
    const home = env.COPILOT_HOME || path.join(os.homedir(), ".copilot");
    let text;
    try {
      text = fs.readFileSync(path.join(home, "config.json"), "utf8"); // JSON with // comments: look for the key, never read the values
    } catch {
      return [false, `no ${home}/config.json: run \`copilot login\``];
    }
    if (/"lastLoggedInUser"\s*:\s*\{/.test(text)) return [true, `logged in, per ${home}/config.json`];
    return [false, "run `copilot login`"];
  }

  invocation(spec) {
    const tmp = tmpDir("capocantiere-copilot-");
    const usageFile = path.join(tmp, "usage.json");
    const a = ["-p", spec.prompt, "-s", "--no-ask-user", "--no-auto-update", "--no-color", "--usage-output-file", usageFile];
    if (spec.model) a.push("--model", spec.model);
    if (spec.effort) a.push("--reasoning-effort", spec.effort);
    if (this.s.disable_builtin_mcps) a.push("--disable-builtin-mcps");
    if (this.s.max_ai_credits) a.push("--max-ai-credits", String(this.s.max_ai_credits));
    if (this.s.secret_env_vars?.length) a.push("--secret-env-vars=" + this.s.secret_env_vars.join(","));
    if (spec.auto_approve) {
      // isolated worktree: approve tools, keep the path check, deny the dangerous commands
      a.push("--allow-all-tools");
      for (const rule of this.s.deny_tools || []) a.push("--deny-tool", rule);
      if (this.s.sandbox) a.push("--sandbox");
    } else {
      // analysis in the main repo: nothing is approved (read-only commands run anyway) and edits are denied
      a.push("--deny-tool", "write");
    }
    return { argv: [...a, ...(this.s.extra_args || [])], env: this.env(), input: null, cleanup: [tmp], usage_file: usageFile };
  }

  parse(r, spec) {
    let usage = null;
    let cost = null;
    try {
      const u = JSON.parse(fs.readFileSync(spec.usage_file, "utf8"));
      const inp = sumKeys(u.modelMetrics, /^input.?tokens$/i);
      const out = sumKeys(u.modelMetrics, /^output.?tokens$/i);
      usage = { input_tokens: inp, output_tokens: out, total_tokens: inp + out, premium_requests: u.totalPremiumRequestCost ?? null };
      if (u.totalNanoAiu) cost = Math.round((u.totalNanoAiu / 1e9) * 10000) / 10000; // AI credits
    } catch {}
    const response = r.stdout.trim();
    const ok = r.code === 0 && !r.timedOut;
    return {
      ok,
      status: ok ? "SUCCESS" : `ERROR (exit ${r.code})`,
      response,
      error: ok ? "" : r.stderr.trim().slice(-1500) || response.slice(-1500),
      usage,
      cost,
      turns: 0,
      conversation_id: null,
      duration: null,
    };
  }
}

// ---------------------------------------------------------------- vibe (Mistral Vibe CLI)
//
// Flags checked against `vibe --help`, `vibe config schema` and the source of vibe 3.1.3:
//   --prompt-file <f>           headless run with the prompt read from a file (no argv length limit)
//   --output json               the messages, as JSON, at the end of the run
//   --output-dir <dir>          writes export.json (token usage, cost, stop reason, outcome) on every exit
//   --workdir, --max-turns, --max-price (USD), --max-tokens, --time-limit <s> (exit 3 when reached)
//   --model <alias>, --thinking <level>          headless mode only, not saved
//   --auto-approve              approve every tool call (the auto-approve agent)
//   --enabled-tools <t>         repeatable; in headless mode it disables every other tool
// Without --auto-approve the default agent is "accept-edits", which still edits files: analyses therefore
// run with read-only tools only. Paths outside the working directory follow `outside_workdir` in
// ~/.vibe/config.toml ("ask" by default, which --auto-approve approves): "deny" is recommended.
// An untrusted working directory is still usable; --trust only makes vibe load the repo's AGENTS.md and
// project config. Exit codes: 0 finished, 1 usage/config error, 2 infrastructure failure (the model API
// after its retries: HTTP 429 = rate limit, 402 = the account has no credit left), 3 stopped on a limit
// (turn_limit, token_limit, price_limit, deadline, ...). export.json: outcome, stop_reason,
// error {message, code}, usage {input_tokens, output_tokens, ...}, cost_usd.
// The cost is computed from the prices in the config, so --max-price is only an extra guard: the real
// budget is the number of runs per worker.

class VibeWorker extends Worker {
  static ID = "vibe";
  static LABEL = "Mistral Vibe CLI";
  static DEFAULTS = {
    bin: "vibe",
    daily_budget: 60,
    max_parallel: 2,
    max_turns: 40,
    max_price: 0.5, // USD cap per run (--max-price); 0 = no cap
    max_tokens: 0, // prompt + completion tokens cap per run (--max-tokens); 0 = no cap
    trust: true, // --trust: trust the worktree for this run only (loads the repo's AGENTS.md and Vibe config)
    analyze_tools: ["read_file", "grep"], // the only tools enabled for analyses
    disabled_tools: [], // tools disabled in implementation runs (--disabled-tools)
    strengths: ["analysis", "docs", "boilerplate", "tests"],
  };
  // The per-run caps (--max-price, --max-tokens) are not quota errors: a fallback would not help.
  static QUOTA_RE =
    /(\b429\b|\b402\b|rate.?limit|quota|too many requests|payment required|usage.?limit|no credits?( left)?|credits?.{0,20}(exhausted|left)|capacity exceeded|requests? limit)/i;
  static OWN_TIMEOUT = true; // --time-limit
  static INSTALL_HINT = "Install Mistral Vibe (`uv tool install mistral-vibe`) and log in (run `vibe` once) or set MISTRAL_API_KEY.";

  static home() {
    return env.VIBE_HOME || path.join(os.homedir(), ".vibe");
  }

  authStatus() {
    if (env.MISTRAL_API_KEY || this.env().MISTRAL_API_KEY) return [true, "MISTRAL_API_KEY set"];
    try {
      const text = fs.readFileSync(path.join(VibeWorker.home(), ".env"), "utf8");
      if (/^\s*(export\s+)?MISTRAL_API_KEY\s*=\s*\S/m.test(text)) return [true, `MISTRAL_API_KEY in ${VibeWorker.home()}/.env`];
    } catch {}
    // Signed in with a Mistral account (browser login): vibe caches the account's plan here.
    try {
      const m = fs.readFileSync(path.join(VibeWorker.home(), "whoami_cache.json"), "utf8").match(/"plan_name"\s*:\s*"([^"]*)"/);
      if (m) return [true, `Mistral account, plan ${m[1]} (per ${VibeWorker.home()}/whoami_cache.json)`];
    } catch {}
    return [false, "not signed in: run `vibe` once to log in with your Mistral account, or set MISTRAL_API_KEY"];
  }

  warnings() {
    let text = "";
    try { text = fs.readFileSync(path.join(VibeWorker.home(), "config.toml"), "utf8"); } catch {}
    const out = [];
    if (!/^\s*outside_workdir\s*=\s*"deny"/m.test(text))
      out.push(`${VibeWorker.home()}/config.toml does not set outside_workdir = "deny": with auto-approve, vibe may touch files ` +
        "outside the job's worktree. Add that line (it also applies to your interactive sessions).");
    if (/^\s*auto_worktree\s*=\s*true/m.test(text))
      out.push(`${VibeWorker.home()}/config.toml sets auto_worktree = true: vibe would edit its own worktree instead of the ` +
        "job's, and every job would end with no changes. Set it to false.");
    return out;
  }

  invocation(spec) {
    const tmp = tmpDir("capocantiere-vibe-");
    const promptFile = path.join(tmp, "prompt.md");
    fs.writeFileSync(promptFile, spec.prompt);
    const outDir = path.join(tmp, "out");
    const a = ["--prompt-file", promptFile, "--output", "json", "--output-dir", outDir, "--workdir", spec.cwd,
      "--max-turns", String(this.s.max_turns)];
    if (this.s.max_price) a.push("--max-price", String(this.s.max_price));
    if (this.s.max_tokens) a.push("--max-tokens", String(this.s.max_tokens));
    if (spec.timeout_min) a.push("--time-limit", String(Math.max(1, Math.round(spec.timeout_min * 60))));
    if (spec.model) a.push("--model", spec.model);
    if (spec.effort) a.push("--thinking", spec.effort);
    if (this.s.trust) a.push("--trust");
    if (spec.auto_approve) {
      a.push("--auto-approve");
      for (const t of this.s.disabled_tools || []) a.push("--disabled-tools", t);
    } else {
      for (const t of this.s.analyze_tools?.length ? this.s.analyze_tools : ["read_file"]) a.push("--enabled-tools", t);
    }
    return { argv: [...a, ...(this.s.extra_args || [])], env: this.env(), input: null, cleanup: [tmp],
      export_file: path.join(outDir, "export.json") };
  }

  static messages(stdout) {
    let v;
    try { v = JSON.parse(stdout); } catch { return null; }
    if (v && typeof v === "object" && !Array.isArray(v)) v = v.messages;
    return Array.isArray(v) ? v : null;
  }

  static text(content) {
    if (typeof content === "string") return content;
    if (Array.isArray(content)) return content.map((p) => (p && typeof p === "object" ? p.text || "" : String(p))).join("\n").trim();
    return "";
  }

  // { outcome, stop, error, usage, cost } from export.json (see the header comment).
  static exportInfo(file) {
    const none = { outcome: "", stop: "", error: "", usage: null, cost: null };
    let e;
    try { e = JSON.parse(fs.readFileSync(file, "utf8")); } catch { return none; }
    if (!e || typeof e !== "object" || Array.isArray(e)) return none;
    const u = e.usage && typeof e.usage === "object" ? e.usage : null;
    const inp = u?.input_tokens || 0;
    const out = u?.output_tokens || 0;
    const usage = u ? { input_tokens: inp, output_tokens: out, total_tokens: u.total_tokens || inp + out } : null;
    const err = e.error && typeof e.error === "object" ? e.error : {};
    const error = [err.code, err.message].filter(Boolean).map(String).join(" ");
    return { outcome: String(e.outcome || ""), stop: String(e.stop_reason || ""), error, usage,
      cost: typeof e.cost_usd === "number" ? e.cost_usd : null };
  }

  parse(r, spec) {
    const messages = VibeWorker.messages(r.stdout);
    let response = "";
    let turns = 0;
    for (const m of messages || []) {
      if (m && typeof m === "object" && m.role === "assistant") {
        turns++;
        const t = VibeWorker.text(m.content);
        if (t) response = t;
      }
    }
    if (messages === null) response = r.stdout.trim();
    const x = VibeWorker.exportInfo(spec.export_file);
    const ok = r.code === 0 && !r.timedOut && messages !== null;
    let status, error;
    if (ok) {
      status = "SUCCESS";
      error = "";
    } else if (r.code === 3) {
      // stopped on a limit: the work left so far is still collected
      status = x.outcome === "deadline" ? "TIME_LIMIT" : (x.outcome || "LIMIT").toUpperCase();
      error = x.outcome === "deadline" ? "time limit reached (vibe --time-limit)" : `vibe stopped on a limit: ${x.outcome || "unknown"}`;
    } else {
      status = `${(x.outcome || "ERROR").toUpperCase()} (exit ${r.code})`;
      error = [x.error, r.stderr.trim().slice(-1500)].filter(Boolean).join("\n") ||
        (r.code === 0 ? "unexpected output (no JSON messages)" : "");
    }
    if (!ok && (x.outcome || x.stop)) error += `\noutcome: ${x.outcome || "?"}${x.stop ? " / stop reason: " + x.stop : ""}`;
    return { ok, status, response, error: error.trim(), usage: x.usage, cost: x.cost, turns, conversation_id: null, duration: null };
  }
}

// Registry. The preference order comes from the config ("order").
const ADAPTERS = { copilot: CopilotWorker, vibe: VibeWorker, agy: AgyWorker };

// ================================================================ server (server.py)

// The worker chosen with `workers use=...` (/capocantiere:use). One server process = one Claude session.
const SESSION = { default: null };

// ---------------------------------------------------------------- usage / budget (per worker)

function recordUsage(entry) {
  const clean = Object.fromEntries(Object.entries(entry).filter(([k, v]) => k === "usage" || (v !== null && v !== undefined)));
  fs.appendFileSync(USAGE_FILE, JSON.stringify({ ts: Date.now(), ...clean }) + "\n");
}

const emptyUsage = () => ({ runs: 0, turns: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, failures: 0, quota_errors: 0, cost: 0 });

// { worker id: totals } over the last `hours`. Entries written before workers existed count as agy.
function usageByWorker(hours = 24) {
  const since = Date.now() - hours * 3600_000;
  const out = {};
  if (!fs.existsSync(USAGE_FILE)) return out;
  for (const line of fs.readFileSync(USAGE_FILE, "utf8").split("\n")) {
    if (!line.trim()) continue;
    let e;
    try { e = JSON.parse(line); } catch { continue; }
    if (!e || typeof e !== "object" || (e.ts || 0) < since) continue;
    const u = e.usage || {};
    const o = (out[e.worker || "agy"] ??= emptyUsage());
    o.runs++;
    o.turns += e.turns || 0;
    for (const k of ["input_tokens", "output_tokens", "total_tokens"]) o[k] += u[k] || 0;
    o.cost += e.cost || 0;
    if (!e.ok) o.failures++;
    if (e.quota) o.quota_errors++;
  }
  return out;
}

function activeJobs(workerId = null, statuses = ["running", "queued"]) {
  return [...jobs.values()].filter((j) => statuses.includes(j.status) && (workerId === null || j.worker === workerId));
}

function budgetState(w, usage) {
  const u = (usage ?? usageByWorker(24))[w.id] || emptyUsage();
  const committed = u.runs + activeJobs(w.id).length; // includes jobs still running or queued
  return { usage: u, committed, budget: w.s.daily_budget, left: Math.max(w.s.daily_budget - committed, 0) };
}

// ---------------------------------------------------------------- choosing a worker

function candidateOrder(requested, fallback) {
  const order = loadConfig().order;
  const first = requested || SESSION.default;
  if (!first) return order;
  const rest = order.filter((w) => w !== first);
  // an explicit request without fallback means exactly that worker
  return [first, ...(fallback ? rest : [])];
}

// Choose the worker for a run. Returns { worker, notes }. Throws if none is usable.
// Skips workers that are disabled, not installed, unusable or out of budget (unless force).
async function pickWorker(requested, force, fallback, exclude = []) {
  const cfg = loadConfig();
  if (requested && !ADAPTERS[requested])
    throw new Error(`Unknown worker "${requested}". Workers: ${Object.keys(ADAPTERS).join(", ")}.`);
  fallback = fallback === undefined || fallback === null ? cfg.fallback : fallback;
  const usage = usageByWorker(24);
  const notes = [];
  for (const wid of candidateOrder(requested, fallback)) {
    if (exclude.includes(wid)) continue;
    const w = getWorker(wid);
    if (!w.enabled) {
      if (wid === requested || wid === SESSION.default) notes.push(`${wid} is disabled in ${CONFIG_FILE}.`);
      continue;
    }
    if (!(await w.version()).installed) {
      notes.push(`${wid} skipped: command "${w.bin}" not found.`);
      continue;
    }
    const problem = w.blocker();
    if (problem) {
      notes.push(`${wid} skipped: ${problem}.`);
      continue;
    }
    const b = budgetState(w, usage);
    if (b.left <= 0 && !force) {
      notes.push(`${wid} skipped: daily budget used up (${b.committed}/${b.budget} runs in 24h, including active jobs).`);
      continue;
    }
    return { worker: w, notes };
  }
  throw new Error(
    `No worker available. ${notes.join(" ") || "All workers are disabled."} Do the work yourself, or retry with force=true if only the budget ` +
      "is the problem. See the workers tool."
  );
}

// ---------------------------------------------------------------- running a worker

async function runWorker(w, role, prompt, cwd, model, effort, timeoutMin, autoApprove, jobId = null, onSpawn = null) {
  const spec = { role, prompt, cwd, model, effort, timeout_min: timeoutMin, auto_approve: autoApprove };
  const inv = w.invocation(spec);
  for (const [k, v] of Object.entries(inv)) if (!["argv", "env", "input", "cleanup"].includes(k)) spec[k] = v;
  const started = Date.now();
  let r, nf, p;
  try {
    r = await run(w.bin, inv.argv, {
      cwd,
      input: inv.input,
      timeoutMs: Math.round((timeoutMin + (w.ownTimeout ? 1 : 0)) * 60_000),
      onSpawn,
      extraEnv: inv.env,
    });
    nf = notFound(r);
    p = nf ? { ok: false, status: "NOT_FOUND", response: "", error: "" } : w.parse(r, spec);
  } finally {
    for (const f of inv.cleanup || []) {
      try { fs.rmSync(f, { recursive: true, force: true }); } catch {}
    }
  }
  const ok = !!p.ok && !nf && !r.timedOut;
  const errText = [p.error, r.timedOut ? `timed out after ${timeoutMin} min` : "", ok ? "" : tail(r.stderr, 1500)]
    .filter(Boolean)
    .join("\n");
  const quota = !ok && (w.quotaRe.test(errText) || (!p.response && w.quotaRe.test(r.stdout || "")));
  const seconds = Math.round((Date.now() - started) / 1000);
  recordUsage({
    worker: w.id,
    kind: role,
    job: jobId,
    model: model || null,
    ok,
    quota,
    turns: p.turns || 0,
    usage: p.usage || null,
    cost: p.cost || null,
    seconds,
  });
  return {
    ok,
    notFound: nf,
    timedOut: r.timedOut,
    quota,
    status: p.status || (ok ? "SUCCESS" : "ERROR"),
    response: p.response || "",
    error: errText,
    conversationId: p.conversation_id ?? null,
    turns: p.turns || 0,
    usage: p.usage ?? null,
    cost: p.cost ?? null,
    durationSeconds: p.duration ?? seconds,
    softDenied: w.softDenied(r),
    stderrTail: tail(r.stderr, 3000),
  };
}

const notFoundMsg = (w) => `Command "${w.bin}" not found for worker ${w.id}. ${w.installHint}`;

// ---------------------------------------------------------------- delegation mode
//
// How much Claude should delegate: off | manual | auto | prefer (rules in modes.json).
// A global default plus per-repo overrides, saved in settings.json. Only "off" is enforced here;
// the others are instructions given to Claude at session start.

const MODE_RULES = JSON.parse(fs.readFileSync(new URL("./modes.json", import.meta.url), "utf8"));
const MODES = Object.keys(MODE_RULES.modes);

// Effective mode for a repo: { mode, source }.
function delegationMode(repo) {
  const s = readJson(SETTINGS_FILE, {});
  const m = repo ? (s.repoModes || {})[repo] : undefined;
  if (MODES.includes(m)) return { mode: m, source: "set for this repo" };
  if (MODES.includes(s.mode)) return { mode: s.mode, source: "global setting" };
  return { mode: MODE_RULES.default, source: "default" };
}

function ensureEnabled(repo) {
  const { mode, source } = delegationMode(repo);
  if (mode === "off")
    throw new Error(`Delegation is off (${source}). Do the work without workers; the user can change this with /capocantiere:mode.`);
}

function modeLine(repo) {
  const { mode, source } = delegationMode(repo);
  return `Delegation mode: ${mode} (${source}). ${MODE_RULES.modes[mode].instruction}`;
}

async function setMode(a) {
  const mode = (a.mode || "").trim();
  const scope = a.scope || "repo";
  if (![...MODES, "inherit"].includes(mode)) throw new Error(`Unknown mode "${mode}". Modes: ${MODES.join(", ")}, or inherit.`);
  const s = readJson(SETTINGS_FILE, {});
  if (scope === "global") {
    if (mode === "inherit") delete s.mode;
    else s.mode = mode;
  } else if (scope === "repo") {
    const repo = await repoRoot(a.repo_dir);
    const overrides = s.repoModes || {};
    if (mode === "inherit") delete overrides[repo];
    else overrides[repo] = mode;
    s.repoModes = overrides;
  } else {
    throw new Error('scope must be "repo" or "global"');
  }
  writeJson(SETTINGS_FILE, s);
}

function modeReport(repo) {
  const s = readJson(SETTINGS_FILE, {});
  const { mode, source } = delegationMode(repo);
  const lines = [
    `Delegation mode${repo ? " for " + repo : ""}: ${mode} (${source})`,
    `Global default: ${s.mode || MODE_RULES.default + " (built-in)"}`,
  ];
  const overrides = Object.entries(s.repoModes || {}).sort(([x], [y]) => x.localeCompare(y));
  if (overrides.length) lines.push(`Per-repo settings: ${overrides.map(([r, m]) => `${r} → ${m}`).join("; ")}`);
  lines.push("", "Modes:", ...MODES.map((m) => `  ${m.padEnd(7)} ${MODE_RULES.modes[m].description}`));
  lines.push("", `Now: ${MODE_RULES.modes[mode].instruction}`);
  return lines.join("\n");
}

// ---------------------------------------------------------------- prompts

function implementPrompt({ task, files, context, acceptance, testCommand }) {
  return [
    "You are an implementation engineer. A tech lead (another AI agent) has assigned you ONE specific task.",
    "You are working in an isolated git worktree: edit the files directly here. Do not commit, do not create branches, do not push.",
    "Rules:",
    "- Do ONLY what the task asks. No unrequested refactors, no new dependencies unless explicitly requested.",
    "- Follow the style and conventions of the existing code.",
    "- Write code comments and documentation in the language already used in the repository.",
    "- Stay inside this folder: do not read or modify files outside it.",
    "- Before finishing, delete any temporary files or build artifacts you created that are not part of the task (e.g. __pycache__, .pytest_cache, coverage reports).",
    "- If something is ambiguous, choose the most conservative interpretation and flag it in NOTES.",
    testCommand
      ? `- When you are done, run: ${testCommand} and fix any errors you introduced.`
      : "- If there is a quick, obvious way to verify your work (tests, typecheck, build), use it.",
    "",
    "## TASK",
    task.trim(),
    files?.length ? `\n## RELEVANT FILES\n${files.map((f) => `- ${f}`).join("\n")}` : "",
    context ? `\n## CONTEXT\n${context.trim()}` : "",
    acceptance ? `\n## ACCEPTANCE CRITERIA\n${acceptance.trim()}` : "",
    "",
    "## FINAL RESPONSE FORMAT (short, max ~25 lines)",
    "SUMMARY: what you did in 1-3 sentences",
    "FILES: list of files touched",
    "VERIFICATION: commands run and their outcome (or 'not verified')",
    "NOTES: doubts, assumptions, unfinished work (or 'none')",
  ]
    .filter((x) => x !== "")
    .join("\n");
}

function analyzePrompt({ question, paths, maxWords }) {
  return [
    "You are a code analyst in READ-ONLY mode. Do NOT modify, create or delete files. Do not run commands that change state.",
    "Your output will be read by another AI agent with little context to spare: be dense and precise, no preamble.",
    "Always cite files and line numbers (path:line) for every important claim.",
    `Maximum length: about ${maxWords} words.`,
    "",
    "## QUESTION",
    question.trim(),
    paths?.length ? `\n## WHERE TO LOOK\n${paths.map((p) => `- ${p}`).join("\n")}` : "",
  ]
    .filter((x) => x !== "")
    .join("\n");
}

// ---------------------------------------------------------------- jobs

const jobs = new Map(); // id -> job
const children = new Map(); // id -> ChildProcess
const queue = [];

const jobFile = (id) => path.join(JOBS_DIR, `${id}.json`);
function saveJob(job) {
  job.updatedAt = isoNow();
  writeJson(jobFile(job.id), job);
}

function loadJobs() {
  for (const f of fs.readdirSync(JOBS_DIR)) {
    if (!f.endsWith(".json")) continue;
    try {
      const j = JSON.parse(fs.readFileSync(path.join(JOBS_DIR, f), "utf8"));
      if (!j.worker) j.worker = "agy";
      if (j.status === "running" || j.status === "queued") {
        // the previous server died while the job was active
        j.status = "interrupted";
        j.error = (j.error ? j.error + "\n" : "") + "MCP server restarted while the job was running.";
        saveJob(j);
      }
      jobs.set(j.id, j);
    } catch {}
  }
}

// Start queued jobs while there are free slots, globally and for each job's worker.
function pump() {
  const cfg = loadConfig();
  for (const id of [...queue]) {
    if (activeJobs(null, ["running"]).length >= cfg.max_parallel) break;
    const job = jobs.get(id);
    if (!job || job.status !== "queued") {
      queue.splice(queue.indexOf(id), 1);
      continue;
    }
    const limit = cfg.workers[job.worker]?.max_parallel ?? 1;
    if (activeJobs(job.worker, ["running"]).length >= limit) continue; // this worker is full; a job for another worker may still start
    queue.splice(queue.indexOf(id), 1);
    job.status = "running";
    job.startedAt = isoNow();
    saveJob(job);
    executeJob(job)
      .catch((e) => {
        job.status = "failed";
        job.error = String(e?.message || e);
        saveJob(job);
      })
      .finally(pump);
  }
}

const BIG_UNTRACKED = 5 * 2 ** 20; // untracked files bigger than this stay out of a job's base

// A commit of the working tree as it is now, for a job's base: changes to tracked files plus the
// untracked files .gitignore doesn't exclude (so a file created by an earlier apply is there).
// Built in a throwaway copy of the index: the user's index, files and stashes are never touched.
// Resolves to { commit (null when nothing differs from HEAD), tooBig (untracked files left out for their size) }.
async function snapshotCommit(repo, id) {
  const head = (await git(repo, "rev-parse", "HEAD")).trim();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "capocantiere-index-"));
  const env = { GIT_INDEX_FILE: path.join(tmp, "index") };
  try {
    try {
      // a copy keeps the stat cache (add -A doesn't rehash every file) and files added with add -f
      const real = (await git(repo, "rev-parse", "--path-format=absolute", "--git-path", "index")).trim();
      fs.copyFileSync(real, env.GIT_INDEX_FILE);
    } catch {
      await gitEnv(repo, env, "read-tree", "HEAD");
    }
    const others = (await git(repo, "ls-files", "-z", "--others", "--exclude-standard")).split("\0");
    const tooBig = [];
    for (const p of others) {
      try {
        if (p && fs.lstatSync(path.join(repo, p)).size > BIG_UNTRACKED) tooBig.push(p);
      } catch {}
    }
    await gitEnv(repo, env, "add", "-A", "--", ".", ...JUNK_PATHSPECS, ...tooBig.map((p) => ":(exclude,literal)" + p));
    const tree = (await gitEnv(repo, env, "write-tree")).trim();
    if (tree === (await git(repo, "rev-parse", "HEAD^{tree}")).trim()) return { commit: null, tooBig };
    const commit = (await git(repo, "-c", "user.name=capocantiere", "-c", "user.email=capocantiere@localhost",
      "-c", "commit.gpgsign=false", // commit-tree honors it: never sign with the user's key
      "commit-tree", tree, "-p", head, "-m", `capocantiere base ${id}`)).trim();
    return { commit, tooBig };
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

async function createJob(args) {
  const repo = await repoRoot(args.repo_dir);
  ensureEnabled(repo);
  const { worker: w, notes } = await pickWorker(args.worker, args.force, args.fallback);
  const resolved = await w.resolveModel("implement", args.model, args.effort);
  const id = newId();
  const branch = `capocantiere/${id}`;
  const worktree = path.join(WORKTREE_DIR, `${path.basename(repo)}-${id}`);

  const { base, baseLabel } = await withGitLock(async () => {
    let b = (await git(repo, "rev-parse", "HEAD")).trim();
    let label = "HEAD";
    if (args.include_uncommitted !== false) {
      const { commit, tooBig } = await snapshotCommit(repo, id);
      if (commit) { b = commit; label = "HEAD + uncommitted changes"; }
      if (tooBig.length) {
        notes.push(`Untracked files over ${BIG_UNTRACKED / 2 ** 20} MB left out of the job's base: ` +
          tooBig.slice(0, 5).join(", ") + (tooBig.length > 5 ? " ..." : "") + ".");
      }
    }
    await git(repo, "worktree", "add", "-q", "-b", branch, worktree, b);
    return { base: b, baseLabel: label };
  });

  const job = {
    id,
    title: args.title || args.task.trim().split("\n")[0].slice(0, 80),
    status: "queued",
    repo,
    worktree,
    branch,
    base,
    baseLabel,
    task: args.task,
    files: args.files || [],
    context: args.context || "",
    acceptance: args.acceptance || "",
    testCommand: args.test_command || "",
    worker: w.id,
    requestedWorker: args.worker || null,
    requestedModel: args.model || null,
    requestedEffort: args.effort || null,
    fallback: args.fallback === undefined || args.fallback === null ? loadConfig().fallback : !!args.fallback,
    force: !!args.force,
    model: resolved.model,
    modelSource: resolved.source,
    effort: resolved.effort,
    timeoutMin: args.timeout_minutes || w.timeoutMin("implement"),
    notes,
    attempts: [],
    createdAt: isoNow(),
  };
  jobs.set(id, job);
  saveJob(job);
  queue.push(id);
  pump();
  return job;
}

// Throw away a failed attempt's edits before the next worker starts.
async function resetWorktree(job) {
  await git(job.worktree, "reset", "-q", "--hard", "HEAD");
  await git(job.worktree, "clean", "-qfd");
}

async function executeJob(job) {
  let w, res;
  while (true) {
    w = getWorker(job.worker);
    res = await runWorker(w, "implement", implementPrompt(job), job.worktree, job.model, job.effort, job.timeoutMin,
      true, job.id, (c) => children.set(job.id, c));
    children.delete(job.id);
    if (job.status === "discarded") return; // discarded while running
    if (res.ok || !res.quota || !job.fallback) break;
    // quota / rate limit: move on to the next worker, if any is usable
    let next;
    try {
      next = await pickWorker(null, job.force, true, [...job.attempts.map((a) => a.worker), w.id]);
    } catch {
      break;
    }
    job.attempts.push({ worker: w.id, model: job.model, status: res.status, quota: true, error: truncate(res.error, 1000), at: isoNow() });
    job.notes = [...(job.notes || []), ...next.notes, `${w.id} hit a quota/rate limit: relaunched on ${next.worker.id}.`];
    await resetWorktree(job);
    job.worker = next.worker.id;
    // a model name rarely means the same on another CLI
    const resolved = await next.worker.resolveModel("implement", null, job.requestedEffort);
    job.model = resolved.model;
    job.effort = resolved.effort;
    job.modelSource = resolved.source;
    job.timeoutMin = next.worker.timeoutMin("implement");
    saveJob(job);
  }

  Object.assign(job, {
    finishedAt: isoNow(),
    workerStatus: res.status,
    response: res.response,
    conversationId: res.conversationId,
    turns: res.turns,
    usage: res.usage,
    cost: res.cost,
    durationSeconds: res.durationSeconds,
    softDenied: res.softDenied,
    stderrTail: res.stderrTail,
  });

  // collect changes anyway, even if the worker failed midway
  await collectChanges(job);

  if (res.notFound) {
    job.status = "failed";
    job.error = notFoundMsg(w);
  } else if (!res.ok) {
    job.status = "failed";
    job.error = res.error || `${w.id} exited with status ${res.status}`;
    if (res.quota) job.error = `QUOTA EXHAUSTED / rate limit on ${w.id}${job.fallback ? "" : " (fallback off)"}.\n${job.error}`;
    job.error += w.failureNote(job.model, res.error);
  } else {
    job.status = job.filesChanged > 0 ? "done" : "no_changes";
  }
  saveJob(job);
  // nothing to review: free the worktree and branch right away
  if (!job.filesChanged) await removeWorktree(job).catch(() => {});
}

// Build artifacts that never belong in a job's diff, even if the repo has no .gitignore for them.
const JUNK_PATHSPECS = ["**/__pycache__/**", "**/*.py[co]", "**/.pytest_cache/**", "**/.mypy_cache/**", "**/.ruff_cache/**", "**/node_modules/**", "**/.DS_Store"].map((p) => ":(exclude,glob)" + p);

async function collectChanges(job) {
  if (!fs.existsSync(job.worktree)) return;
  await git(job.worktree, "add", "-A", "--", ".", ...JUNK_PATHSPECS);
  const staged = await run("git", ["diff", "--cached", "--quiet"], { cwd: job.worktree });
  if (staged.code === 1) {
    const who = job.worker || "worker";
    await git(job.worktree, "-c", `user.name=${who} (capocantiere)`, "-c", "user.email=capocantiere@localhost",
      "-c", "commit.gpgsign=false", // never sign a worker's code with the user's key
      "commit", "-q", "--no-verify", "-m", `${who}: ${job.title}`, "-m", `job ${job.id}`);
  }
  const stat = await git(job.repo, "diff", "--stat", job.base, job.branch);
  const names = (await git(job.repo, "diff", "--name-status", job.base, job.branch)).trim();
  // keep the patch as raw bytes so non-UTF-8 files survive intact
  const p = await run("git", ["diff", "--binary", job.base, job.branch], { cwd: job.repo, binary: true });
  if (p.code !== 0) throw new Error("git diff --binary failed: " + p.stderr.trim());
  const patchFile = path.join(PATCH_DIR, `${job.id}.patch`);
  fs.writeFileSync(patchFile, p.stdout);
  job.diffStat = stat.trim();
  job.changedFiles = names ? names.split("\n") : [];
  job.filesChanged = job.changedFiles.length;
  job.patchFile = patchFile;
  job.patchBytes = p.stdout.length;
}

async function removeWorktree(job) {
  await withGitLock(async () => {
    if (fs.existsSync(job.worktree)) await run("git", ["worktree", "remove", "--force", job.worktree], { cwd: job.repo });
    await run("git", ["worktree", "prune"], { cwd: job.repo });
    await run("git", ["branch", "-D", job.branch], { cwd: job.repo });
  });
}

function jobSummary(j) {
  return {
    id: j.id,
    title: j.title,
    worker: j.worker ?? null,
    status: j.status,
    files_changed: j.filesChanged ?? null,
    duration_s: j.durationSeconds ?? null,
    created: j.createdAt,
  };
}

function jobDetail(j, { includeDiff = true, maxDiffChars = 15000 } = {}) {
  const ws = j.workerStatus || j.agyStatus;
  const lines = [
    `JOB ${j.id} — ${j.title}`,
    `status: ${j.status}${ws ? ` (${j.worker}: ${ws})` : ""}`,
    `worker: ${j.worker}`,
    `branch: ${j.branch}   worktree: ${j.worktree}`,
    `base: ${j.baseLabel} (${(j.base || "").slice(0, 10)})`,
    `model: ${j.model || "CLI default"}${j.modelSource && j.modelSource !== "CLI default" ? ` (${j.modelSource})` : ""}${j.effort ? `, effort ${j.effort}` : ""}`,
  ];
  if (j.durationSeconds != null)
    lines.push(`duration: ${Math.round(j.durationSeconds)}s, turns: ${j.turns ?? "?"}${j.cost ? `, cost ${j.cost}` : ""}`);
  for (const a of j.attempts || [])
    lines.push(`earlier attempt: ${a.worker} failed${a.quota ? " (quota)" : ""} — ${(a.error || "").split("\n")[0].slice(0, 200)}`);
  if (j.notes?.length) lines.push("", "NOTES:", ...j.notes.map((n) => `- ${n}`));
  if (j.error) lines.push("", "ERROR:", truncate(j.error, 2500));
  if (j.softDenied?.length) lines.push("", "ACTIONS DENIED TO THE WORKER (soft-deny):", ...j.softDenied);
  if (j.response) lines.push("", "WORKER'S RESPONSE:", truncate(j.response, 4000));
  if (j.diffStat) lines.push("", "DIFF STAT:", j.diffStat);
  if (includeDiff && j.patchBytes && j.patchFile && fs.existsSync(j.patchFile)) {
    const patch = fs.readFileSync(j.patchFile, "utf8");
    lines.push("", `DIFF (full patch: ${j.patchFile}):`, truncate(patch, maxDiffChars));
  }
  if (j.status === "done") lines.push("", "Next step: review the diff, then apply or discard (or a new delegate with the fixes).");
  return lines.join("\n");
}

async function waitFor(ids, seconds) {
  const deadline = Date.now() + seconds * 1000;
  const allDone = () => ids.every((id) => TERMINAL.has(jobs.get(id)?.status));
  while (Date.now() < deadline) {
    if (allDone()) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return allDone();
}

// ---------------------------------------------------------------- analyze

async function analyze(a) {
  const repo = await repoRoot(a.repo_dir);
  ensureEnabled(repo);
  const prompt = analyzePrompt({ question: a.question || "", paths: a.paths, maxWords: a.max_words || 600 });
  const snapshot = async () => {
    const s = await run("git", ["status", "--porcelain=v1", "-uall"], { cwd: repo });
    const d = await run("git", ["diff"], { cwd: repo });
    return crypto.createHash("sha1").update(s.stdout + d.stdout).digest("hex");
  };
  const fallback = a.fallback === undefined || a.fallback === null ? loadConfig().fallback : !!a.fallback;
  let { worker: w, notes } = await pickWorker(a.worker, a.force, fallback);
  let requestedModel = a.model;
  const tried = [];
  let res, before, after, resolved;
  while (true) {
    resolved = await w.resolveModel("analyze", requestedModel, a.effort);
    before = await snapshot();
    res = await runWorker(w, "analyze", prompt, repo, resolved.model, resolved.effort,
      a.timeout_minutes || w.timeoutMin("analyze"), false); // in the main repo: no auto-approval
    after = await snapshot();
    tried.push(w.id);
    if (res.notFound) throw new Error(notFoundMsg(w));
    if (res.ok || !res.quota || !fallback) break;
    let next;
    try {
      next = await pickWorker(null, a.force, true, tried);
    } catch {
      break;
    }
    notes = [...notes, ...next.notes, `${w.id} hit a quota/rate limit: asked ${next.worker.id} instead.`];
    w = next.worker;
    requestedModel = null;
  }
  const out = notes.length ? [`NOTES: ${notes.join(" ")}`] : [];
  if (!res.ok)
    out.push(`ERROR from ${w.id} (${res.status})${res.quota ? " — QUOTA/RATE LIMIT" : ""}:\n${truncate(res.error, 2000)}${w.failureNote(resolved.model, res.error)}`);
  if (before !== after) out.push("⚠️ WARNING: the repo state changed during the analysis. Check `git status` / `git diff`.");
  out.push(truncate(res.response || "(no response)", 16000));
  const total = res.usage?.total_tokens;
  out.push(`\n[analyze: ${w.id}, ${resolved.model || "CLI default"} (${resolved.source}), ${Math.round(res.durationSeconds)}s, turns ${res.turns}, tokens ${total ?? "?"}]`);
  return { text: out.join("\n\n"), isError: !res.ok && !res.response };
}

// ---------------------------------------------------------------- workers report

const fmtCost = (c) => String(Number(c.toPrecision(4)));

async function workersReport() {
  const cfg = loadConfig();
  const usage = usageByWorker(24);
  const lines = [
    `Workers, in preference order (config: ${CONFIG_FILE}${fs.existsSync(CONFIG_FILE) ? "" : ", not created yet: built-in defaults"}):`,
  ];
  for (const w of allWorkers()) {
    const { installed, text: version } = await w.version();
    const b = budgetState(w, usage);
    const u = b.usage;
    const [auth, authNote] = installed ? w.authStatus() : [null, ""];
    let state = w.enabled ? "enabled" : "DISABLED";
    if (w.id === SESSION.default) state += ", session default";
    lines.push("", `${w.id} — ${w.label} [${state}]`);
    lines.push(`  installed: ${installed ? version : `NO (${w.bin} not found). ${w.installHint}`}`);
    if (installed)
      lines.push(`  authenticated: ${auth === true ? "yes" : auth === false ? "NO" : "unknown"}${authNote ? ` (${authNote})` : ""}`);
    lines.push(
      `  budget 24h: ${b.committed}/${b.budget} used, ${b.left} left` +
        (u.failures ? `, ${u.failures} failed, ${u.quota_errors} quota errors` : "") +
        (u.cost ? `, cost ${fmtCost(u.cost)}` : "")
    );
    lines.push(
      `  jobs: ${activeJobs(w.id, ["running"]).length} running, ${activeJobs(w.id, ["queued"]).length} queued (max ${w.s.max_parallel})`
    );
    lines.push(`  models: ${installed && w.enabled ? await w.modelsLine() : `${w.s.model || "CLI default"} (configured)`}`);
    lines.push(`  strengths: ${(w.s.strengths || []).join(", ") || "—"}`);
    for (const warning of w.warnings()) lines.push(`  ⚠️ ${warning}`);
  }
  lines.push("", `Fallback on quota/budget: ${cfg.fallback ? "on" : "off"}. Max parallel jobs overall: ${cfg.max_parallel}.`);
  lines.push(`Session default: ${SESSION.default || "none (preference order)"}.`);
  lines.push(modeLine(await currentRepo()));
  for (const e of cfg.errors) lines.push(`CONFIG ERROR: ${e}`);
  lines.push(`State: ${STATE_DIR}`);
  return lines.join("\n");
}

async function setSessionDefault(value) {
  const v = String(value || "").trim();
  if (["", "auto", "none"].includes(v)) {
    SESSION.default = null;
    return "Session default cleared: workers are chosen in the configured order.";
  }
  const w = getWorker(v);
  if (!w.enabled) throw new Error(`${v} is disabled in ${CONFIG_FILE}; enable it there first.`);
  const { installed } = await w.version();
  SESSION.default = v;
  return `Session default worker: ${v}${installed ? "" : ` (WARNING: ${w.bin} not found)`}.`;
}

// ---------------------------------------------------------------- tools

// Tool definitions are shared with server.py. The Node server is frozen: tools and parameters added after
// it was frozen exist only in the Python server, so they are not listed here.
const PYTHON_ONLY_TOOLS = new Set(["blame", "rate"]);
const PYTHON_ONLY_PARAMS = {
  delegate: ["kind", "tier"],
  analyze: ["tier"],
  apply: ["quality"],
  discard: ["reason"],
  workers: ["scores"],
};
const TOOLS = JSON.parse(fs.readFileSync(new URL("./tools.json", import.meta.url), "utf8"))
  .filter((t) => !PYTHON_ONLY_TOOLS.has(t.name))
  .map((t) => {
    const params = PYTHON_ONLY_PARAMS[t.name];
    if (!params || !t.inputSchema?.properties) return t;
    return {
      ...t,
      inputSchema: {
        ...t.inputSchema,
        properties: Object.fromEntries(
          Object.entries(t.inputSchema.properties).filter(([name]) => !params.includes(name)),
        ),
      },
    };
  });

function getJob(a) {
  const j = jobs.get(a.job_id);
  if (!j) throw new Error(`job ${a.job_id} not found`);
  return j;
}

async function callTool(name, a = {}) {
  switch (name) {
    case "delegate": {
      if (!a.task?.trim()) throw new Error("task is required");
      const job = await createJob(a);
      const wait = Math.min(a.wait_seconds || 0, 600);
      if (wait > 0) {
        await waitFor([job.id], wait);
        if (TERMINAL.has(job.status)) return jobDetail(job);
      }
      const w = getWorker(job.worker);
      const b = budgetState(w);
      return [
        ...(job.notes.length ? [`NOTES: ${job.notes.join(" ")}`] : []),
        `Job ${job.id} started on ${job.worker} (${job.status}). Branch ${job.branch}, base: ${job.baseLabel}.`,
        `Model: ${job.model || "CLI default"} (${job.modelSource}).`,
        `Active jobs: ${activeJobs(null, ["running"]).length} running, ${queue.length} queued.`,
        `${w.id} 24h usage (including active jobs): ${b.committed}/${b.budget}.`,
        `Use jobs with job_id="${job.id}" and wait_seconds to wait for the result.`,
      ].join("\n");
    }

    case "analyze":
      return analyze(a);

    case "jobs": {
      if (a.job_id) {
        const j = getJob(a);
        if (a.wait_seconds) await waitFor([j.id], Math.min(a.wait_seconds, 600));
        return jobDetail(j, { includeDiff: a.include_diff !== false, maxDiffChars: a.max_diff_chars || 15000 });
      }
      let repo = null;
      if (!a.all_repos) repo = await repoRoot(a.repo_dir).catch(() => null);
      const list = () => [...jobs.values()].filter((j) => !repo || j.repo === repo);
      if (a.wait_seconds) {
        const active = list().filter((j) => !TERMINAL.has(j.status)).map((j) => j.id);
        if (active.length) await waitFor(active, Math.min(a.wait_seconds, 600));
      }
      const visible = list()
        .filter((j) => !["applied", "discarded"].includes(j.status))
        .sort((x, y) => x.createdAt.localeCompare(y.createdAt));
      if (!visible.length) return "No open jobs.";
      return visible.map((j) => JSON.stringify(jobSummary(j))).join("\n");
    }

    case "apply": {
      const j = getJob(a);
      if (j.status === "running" || j.status === "queued") throw new Error("The job is still running.");
      if (j.status === "applied") return "Already applied.";
      if (j.status === "interrupted" && fs.existsSync(j.worktree)) await collectChanges(j);
      if (!j.patchFile || !fs.existsSync(j.patchFile) || !j.patchBytes) return "No changes to apply.";
      let r = await run("git", ["apply", "--whitespace=nowarn", j.patchFile], { cwd: j.repo });
      let how = "clean";
      if (r.code !== 0) {
        r = await run("git", ["apply", "--3way", "--whitespace=nowarn", j.patchFile], { cwd: j.repo });
        how = "3-way merge";
        if (r.code !== 0) {
          return {
            isError: true,
            text:
              `Apply failed (even with 3-way). The repo has changed too much since the job's base.\n${truncate(r.stderr, 3000)}\n` +
              "Options: resolve conflicts by hand (if there are markers), check the state with `git diff`, or relaunch the task.",
          };
        }
      }
      j.status = "applied";
      j.appliedAt = isoNow();
      saveJob(j);
      if (!a.keep_worktree) await removeWorktree(j);
      const warn = r.stderr.trim();
      return `Applied (${how}) to the working tree of ${j.repo}. Changes are NOT committed.\n${warn ? warn + "\n" : ""}${j.diffStat || ""}\nNow: run tests/typecheck and polish if needed.`;
    }

    case "discard": {
      const j = getJob(a);
      const wasRunning = j.status === "running";
      j.status = "discarded";
      saveJob(j);
      const c = children.get(j.id);
      if (c) {
        killTree(c, "SIGTERM");
        setTimeout(() => killTree(c, "SIGKILL"), 3000).unref();
      }
      const qi = queue.indexOf(j.id);
      if (qi >= 0) queue.splice(qi, 1);
      pump(); // a slot may have freed up: start the next queued job
      if (wasRunning) await new Promise((r) => setTimeout(r, 1500));
      await removeWorktree(j);
      return `Job ${j.id} discarded.`;
    }

    case "workers": {
      if (a.doctor) return "doctor is only available on the Python server (isolation and credentials are Python-only).";
      const head = "use" in a ? (await setSessionDefault(a.use)) + "\n\n" : "";
      return head + (await workersReport());
    }

    case "mode": {
      if (a.mode) await setMode(a);
      const repo = await repoRoot(a.repo_dir).catch(() => null);
      return modeReport(repo);
    }

    case "models": {
      const wid = a.worker || "agy";
      const w = getWorker(wid);
      if (!(w instanceof AgyWorker))
        return `${wid} does not publish a model list. Model in use: ${await w.modelsLine()}. Set "model" / "analyze_model" for it in ${CONFIG_FILE}, or pass model to delegate/analyze.`;
      if (["analyze", "implement", "exclude"].some((k) => k in a)) await w.updateModelSettings(a);
      return w.modelsReport(!!a.refresh);
    }
  }
  throw new Error(`unknown tool: ${name}`);
}

// ---------------------------------------------------------------- session summary (for the SessionStart hook)

async function sessionSummary() {
  const repo = await currentRepo();
  const tag = "[capocantiere]";
  if (delegationMode(repo).mode === "off") return `${tag} ${modeLine(repo)}`;
  const usage = usageByWorker(24);
  const ready = [];
  const other = [];
  for (const w of allWorkers()) {
    if (!w.enabled) continue;
    if (!(await w.version()).installed) {
      other.push(`${w.id} not installed`);
      continue;
    }
    if (w.blocker()) {
      other.push(`${w.id} unusable: ${w.blocker()}`);
      continue;
    }
    const b = budgetState(w, usage);
    const u = b.usage;
    ready.push(
      `${w.id} (${b.left}/${b.budget} runs left${u.quota_errors ? `, ${u.quota_errors} recent quota errors` : ""}; good at ${(w.s.strengths || []).join(", ") || "anything"})`
    );
    for (const x of w.warnings()) other.push(`${w.id}: ${x}`);
  }
  const lines = [];
  if (ready.length)
    lines.push(`${tag} Workers available for delegation (MCP tools delegate/analyze/jobs/apply/discard/workers), in preference order: ${ready.join("; ")}.`);
  else
    lines.push(`${tag} No worker is available in this session (none enabled and installed): work without delegating. See the workers tool.`);
  for (const o of other) lines.push(`${tag} ${o}`);
  lines.push(`${tag} ${modeLine(repo)}`);
  if (repo) {
    const pending = [...jobs.values()].filter(
      (j) => j.repo === repo && (j.status === "interrupted" || (["done", "failed"].includes(j.status) && (j.filesChanged || 0) > 0))
    );
    if (pending.length)
      lines.push(
        `${tag} Jobs from previous sessions still open in this repo: ${pending.map((j) => `${j.id} (${j.status} on ${j.worker}: ${j.title})`).join("; ")}. Review them with jobs, then apply or discard.`
      );
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------- MCP stdio (JSON-RPC 2.0, newline-delimited)

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

async function handle(msg) {
  if (!msg || typeof msg !== "object") return;
  const { id, method, params } = msg;
  const isRequest = id !== undefined && id !== null;
  try {
    let result;
    switch (method) {
      case "initialize":
        result = {
          protocolVersion: params?.protocolVersion || "2025-06-18",
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: "capocantiere", version: VERSION },
          instructions:
            "Tools for delegating work to external coding-agent CLIs (workers: GitHub Copilot CLI, Mistral Vibe, ...). " +
            "You remain the manager: you plan, delegate well-specified tasks, review the diffs, and apply or discard them.",
        };
        break;
      case "ping":
        result = {};
        break;
      case "tools/list":
        result = { tools: TOOLS };
        break;
      case "tools/call": {
        const out = await callTool(params?.name, params?.arguments || {});
        const o = typeof out === "string" ? { text: out } : out;
        result = { content: [{ type: "text", text: o.text }], isError: !!o.isError };
        break;
      }
      default:
        if (!isRequest) return; // notifications (e.g. notifications/initialized)
        return send({ jsonrpc: "2.0", id, error: { code: -32601, message: `Method not supported: ${method}` } });
    }
    if (isRequest) send({ jsonrpc: "2.0", id, result });
  } catch (e) {
    if (!isRequest) return;
    if (method === "tools/call") {
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: `Error: ${e?.message || e}` }], isError: true } });
    } else {
      send({ jsonrpc: "2.0", id, error: { code: -32603, message: String(e?.message || e) } });
    }
  }
}

ensureDirs();
if (process.argv.includes("--example-config")) {
  process.stdout.write(JSON.stringify(exampleConfig(), null, 2) + "\n");
} else {
  loadJobs();
  if (process.argv.includes("--session-summary")) {
    sessionSummary().then((s) => { if (s) process.stdout.write(s + "\n"); }).catch(() => {});
  } else {
    let buf = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      buf += chunk;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line) continue;
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          send({ jsonrpc: "2.0", id: null, error: { code: -32700, message: "Parse error" } });
          continue;
        }
        handle(msg);
      }
    });
    process.stdin.on("end", () => {
      for (const c of children.values()) killTree(c, "SIGTERM");
      setTimeout(() => process.exit(0), 200).unref();
    });
  }
}
