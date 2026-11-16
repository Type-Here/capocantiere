#!/usr/bin/env node
// gemini-delegate: MCP server that lets Claude Code (the "manager") delegate
// work to Antigravity CLI (`agy`, Gemini) using a Google AI Pro subscription.
//
// - Every code-writing task runs in an isolated git worktree, on a dedicated branch.
//   The main repo is never touched until Claude calls gemini_apply.
// - Jobs are asynchronous and can run in parallel (with a queue).
// - Usage/quota is tracked so the subscription's daily limits aren't exceeded.
//
// Zero dependencies: only Node >= 18 and git are required.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";

// ---------------------------------------------------------------- config

const env = process.env;
const num = (v, d) => (v !== undefined && v !== "" && !Number.isNaN(Number(v)) ? Number(v) : d);
const bool = (v, d) => (v === undefined || v === "" ? d : /^(1|true|yes|on)$/i.test(v));

const CONFIG = {
  agyBin: env.GEMINI_DELEGATE_AGY_BIN || "agy",
  model: env.GEMINI_DELEGATE_MODEL || "", // empty = agy's default
  analyzeModel: env.GEMINI_DELEGATE_ANALYZE_MODEL || "",
  effort: env.GEMINI_DELEGATE_EFFORT || "",
  dailyBudget: num(env.GEMINI_DELEGATE_DAILY_BUDGET, 150), // agy runs per 24h
  maxParallel: num(env.GEMINI_DELEGATE_MAX_PARALLEL, 3),
  taskTimeoutMin: num(env.GEMINI_DELEGATE_TASK_TIMEOUT_MIN, 20),
  analyzeTimeoutMin: num(env.GEMINI_DELEGATE_ANALYZE_TIMEOUT_MIN, 8),
  sandbox: bool(env.GEMINI_DELEGATE_SANDBOX, true),
  // Inside the isolated worktree agy may edit files and run commands without asking:
  // it's the only way to work headless. The main repo stays protected.
  skipPermissionsInWorktree: bool(env.GEMINI_DELEGATE_SKIP_PERMISSIONS, true),
  extraArgs: (env.GEMINI_DELEGATE_EXTRA_ARGS || "").split(" ").filter(Boolean),
  stateDir:
    env.GEMINI_DELEGATE_STATE_DIR ||
    path.join(env.XDG_STATE_HOME || path.join(os.homedir(), ".local", "state"), "gemini-delegate"),
  worktreeDir:
    env.GEMINI_DELEGATE_WORKTREE_DIR ||
    path.join(env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "gemini-delegate", "worktrees"),
};

const JOBS_DIR = path.join(CONFIG.stateDir, "jobs");
const PATCH_DIR = path.join(CONFIG.stateDir, "patches");
const USAGE_FILE = path.join(CONFIG.stateDir, "usage.jsonl");
for (const d of [JOBS_DIR, PATCH_DIR, CONFIG.worktreeDir]) fs.mkdirSync(d, { recursive: true });

const VERSION = "1.0.0";
const TERMINAL = new Set(["done", "no_changes", "failed", "applied", "discarded", "interrupted"]);

// ---------------------------------------------------------------- helpers

// Signal the child and everything it started (its own process group on POSIX).
function killTree(child, sig) {
  try {
    if (process.platform !== "win32") process.kill(-child.pid, sig);
    else child.kill(sig);
  } catch {
    try { child.kill(sig); } catch {}
  }
}

function run(cmd, args, { cwd, input, timeoutMs, onSpawn } = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(cmd, args, { cwd, env: process.env, stdio: ["pipe", "pipe", "pipe"], detached: process.platform !== "win32" });
    } catch (e) {
      return resolve({ code: -1, stdout: "", stderr: String(e), error: e });
    }
    onSpawn?.(child);
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    let timer;
    if (timeoutMs) {
      timer = setTimeout(() => {
        timedOut = true;
        killTree(child, "SIGTERM");
        setTimeout(() => killTree(child, "SIGKILL"), 5000).unref();
      }, timeoutMs);
    }
    child.stdout.on("data", (d) => (stdout += d));
    child.stderr.on("data", (d) => (stderr += d));
    child.on("error", (e) => {
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: stderr + String(e), error: e, timedOut });
    });
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      resolve({ code: code ?? -1, signal, stdout, stderr, timedOut });
    });
    if (input !== undefined) child.stdin.end(input);
    else child.stdin.end();
  });
}

async function git(cwd, ...args) {
  const r = await run("git", args, { cwd });
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

const truncate = (s, n) =>
  !s ? "" : s.length <= n ? s : s.slice(0, n) + `\n… [truncated: ${s.length - n} characters omitted]`;
const tail = (s, n) => (!s ? "" : s.length <= n ? s : "…" + s.slice(-n));

function newId() {
  const d = new Date();
  const p = (x) => String(x).padStart(2, "0");
  return `g${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}-${crypto
    .randomBytes(2)
    .toString("hex")}`;
}

async function repoRoot(dir) {
  const start = dir || env.CLAUDE_PROJECT_DIR || process.cwd();
  const r = await run("git", ["rev-parse", "--show-toplevel"], { cwd: start });
  if (r.code !== 0) throw new Error(`"${start}" is not a git repository. gemini-delegate requires git.`);
  return r.stdout.trim();
}

// ---------------------------------------------------------------- usage / quota

function recordUsage(entry) {
  fs.appendFileSync(USAGE_FILE, JSON.stringify({ ts: Date.now(), ...entry }) + "\n");
}

function usageWindow(hours = 24) {
  const since = Date.now() - hours * 3600_000;
  const out = { runs: 0, turns: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, failures: 0, quota_errors: 0 };
  if (!fs.existsSync(USAGE_FILE)) return out;
  for (const line of fs.readFileSync(USAGE_FILE, "utf8").split("\n")) {
    if (!line) continue;
    let e;
    try { e = JSON.parse(line); } catch { continue; }
    if (e.ts < since) continue;
    out.runs++;
    out.turns += e.turns || 0;
    out.input_tokens += e.usage?.input_tokens || 0;
    out.output_tokens += e.usage?.output_tokens || 0;
    out.total_tokens += e.usage?.total_tokens || 0;
    if (!e.ok) out.failures++;
    if (e.quota) out.quota_errors++;
  }
  return out;
}

function activeJobCount() {
  let n = 0;
  for (const j of jobs.values()) if (j.status === "running" || j.status === "queued") n++;
  return n;
}

function budgetCheck(force) {
  const u = usageWindow(24);
  u.committed = u.runs + activeJobCount(); // includes jobs still running
  if (!force && u.committed >= CONFIG.dailyBudget) {
    throw new Error(
      `Daily budget reached (${u.committed}/${CONFIG.dailyBudget} agy runs in the last 24h, including running jobs). ` +
        `Do the work directly in Claude, or retry with force=true.`
    );
  }
  return u;
}

const QUOTA_RE = /(429|RESOURCE_EXHAUSTED|rate ?limit|quota)/i;

// ---------------------------------------------------------------- agy

function agyArgs({ prompt, model, effort, timeoutMin, skipPermissions }) {
  const a = ["-p", prompt, "--output-format", "json"];
  if (model) a.push("--model", model);
  if (effort) a.push("--effort", effort);
  if (timeoutMin) a.push("--print-timeout", `${timeoutMin}m`);
  if (skipPermissions) a.push("--dangerously-skip-permissions");
  if (CONFIG.sandbox) a.push("--sandbox");
  a.push(...CONFIG.extraArgs);
  return a;
}

function parseAgyOutput(stdout) {
  const lines = stdout.trim().split("\n").reverse();
  for (const l of lines) {
    const t = l.trim();
    if (!t.startsWith("{")) continue;
    try { return JSON.parse(t); } catch {}
  }
  try { return JSON.parse(stdout); } catch {}
  return null;
}

async function runAgy({ cwd, prompt, model, effort, timeoutMin, skipPermissions, kind, jobId, onSpawn }) {
  const args = agyArgs({ prompt, model, effort, timeoutMin, skipPermissions });
  const started = Date.now();
  const r = await run(CONFIG.agyBin, args, {
    cwd,
    timeoutMs: (timeoutMin + 1) * 60_000,
    onSpawn,
  });
  const envl = parseAgyOutput(r.stdout);
  const notFound = r.error && r.error.code === "ENOENT";
  const status = envl?.status || (r.code === 0 ? "SUCCESS" : "ERROR");
  const ok = !notFound && r.code === 0 && status === "SUCCESS";
  const errText = [envl?.error, r.timedOut ? `timed out after ${timeoutMin} min` : "", ok ? "" : tail(r.stderr, 1500)]
    .filter(Boolean)
    .join("\n");
  const quota = QUOTA_RE.test(errText) || QUOTA_RE.test(r.stderr || "");
  recordUsage({
    kind,
    job: jobId,
    ok,
    quota,
    turns: envl?.num_turns || 0,
    usage: envl?.usage || null,
    seconds: Math.round((Date.now() - started) / 1000),
  });
  // "soft-deny" notices that agy writes to stderr even when it exits with 0
  const softDenied = (r.stderr || "")
    .split("\n")
    .filter((l) => /denied|not approved|requires approval|soft-den/i.test(l))
    .slice(0, 10);
  return {
    ok,
    notFound,
    quota,
    status,
    response: envl?.response ?? (envl ? "" : r.stdout.trim()),
    error: errText,
    conversationId: envl?.conversation_id || null,
    turns: envl?.num_turns || 0,
    usage: envl?.usage || null,
    durationSeconds: envl?.duration_seconds ?? Math.round((Date.now() - started) / 1000),
    softDenied,
    stderrTail: tail(r.stderr, 3000),
  };
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
  job.updatedAt = new Date().toISOString();
  fs.writeFileSync(jobFile(job.id), JSON.stringify(job, null, 2));
}
function loadJobs() {
  for (const f of fs.readdirSync(JOBS_DIR)) {
    if (!f.endsWith(".json")) continue;
    try {
      const j = JSON.parse(fs.readFileSync(path.join(JOBS_DIR, f), "utf8"));
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
loadJobs();

function runningCount() {
  return [...jobs.values()].filter((j) => j.status === "running").length;
}

function pump() {
  while (queue.length && runningCount() < CONFIG.maxParallel) {
    const id = queue.shift();
    const job = jobs.get(id);
    if (!job || job.status !== "queued") continue;
    executeJob(job).catch((e) => {
      job.status = "failed";
      job.error = String(e?.message || e);
      saveJob(job);
    }).finally(pump);
  }
}

async function createJob(args) {
  const repo = await repoRoot(args.repo_dir);
  const usage = budgetCheck(args.force);
  const id = newId();
  const branch = `gemini/${id}`;
  const worktree = path.join(CONFIG.worktreeDir, `${path.basename(repo)}-${id}`);

  const { base, baseLabel } = await withGitLock(async () => {
    const head = (await git(repo, "rev-parse", "HEAD")).trim();
    let b = head;
    let label = "HEAD";
    if (args.include_uncommitted !== false) {
      // `git stash create` makes a commit of the uncommitted changes WITHOUT touching the working tree
      const s = (await git(repo, "stash", "create", `gemini-delegate base ${id}`)).trim();
      if (s) { b = s; label = "HEAD + uncommitted changes"; }
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
    model: args.model || CONFIG.model,
    effort: args.effort || CONFIG.effort,
    timeoutMin: args.timeout_minutes || CONFIG.taskTimeoutMin,
    createdAt: new Date().toISOString(),
  };
  jobs.set(id, job);
  saveJob(job);
  queue.push(id);
  pump();
  return { job, usage };
}

async function executeJob(job) {
  job.status = "running";
  job.startedAt = new Date().toISOString();
  saveJob(job);

  const prompt = implementPrompt(job);
  const res = await runAgy({
    cwd: job.worktree,
    prompt,
    model: job.model,
    effort: job.effort,
    timeoutMin: job.timeoutMin,
    skipPermissions: CONFIG.skipPermissionsInWorktree,
    kind: "implement",
    jobId: job.id,
    onSpawn: (c) => children.set(job.id, c),
  });
  children.delete(job.id);
  if (job.status === "discarded") return; // discarded while running

  Object.assign(job, {
    finishedAt: new Date().toISOString(),
    agyStatus: res.status,
    response: res.response,
    conversationId: res.conversationId,
    turns: res.turns,
    usage: res.usage,
    durationSeconds: res.durationSeconds,
    softDenied: res.softDenied,
    stderrTail: res.stderrTail,
  });

  // collect changes anyway, even if agy failed midway
  await collectChanges(job);

  if (res.notFound) {
    job.status = "failed";
    job.error = `Comando "${CONFIG.agyBin}" not found. Install Antigravity CLI and run \`agy\` once to log in.`;
  } else if (!res.ok) {
    job.status = "failed";
    job.error = res.error || `agy exited with status ${res.status}`;
    if (res.quota) job.error = "QUOTA EXHAUSTED / Gemini rate limit.\n" + job.error;
  } else {
    job.status = job.filesChanged > 0 ? "done" : "no_changes";
  }
  saveJob(job);
  // nothing to review: free the worktree and branch right away
  if (!job.filesChanged) await removeWorktree(job).catch(() => {});
}

async function collectChanges(job) {
  if (!fs.existsSync(job.worktree)) return;
  await git(job.worktree, "add", "-A");
  const staged = await run("git", ["diff", "--cached", "--quiet"], { cwd: job.worktree });
  if (staged.code === 1) {
    await git(
      job.worktree,
      "-c", "user.name=Gemini (agy)",
      "-c", "user.email=gemini-delegate@localhost",
      "commit", "-q", "--no-verify", "-m", `gemini: ${job.title}`, "-m", `job ${job.id}`
    );
  }
  const stat = await git(job.repo, "diff", "--stat", job.base, job.branch);
  const names = (await git(job.repo, "diff", "--name-status", job.base, job.branch)).trim();
  const patch = await git(job.repo, "diff", "--binary", job.base, job.branch);
  const patchFile = path.join(PATCH_DIR, `${job.id}.patch`);
  fs.writeFileSync(patchFile, patch);
  job.diffStat = stat.trim();
  job.changedFiles = names ? names.split("\n") : [];
  job.filesChanged = job.changedFiles.length;
  job.patchFile = patchFile;
  job.patchBytes = Buffer.byteLength(patch);
}

async function removeWorktree(job) {
  await withGitLock(async () => {
    if (fs.existsSync(job.worktree)) {
      await run("git", ["worktree", "remove", "--force", job.worktree], { cwd: job.repo });
    }
    await run("git", ["worktree", "prune"], { cwd: job.repo });
    await run("git", ["branch", "-D", job.branch], { cwd: job.repo });
  });
}

function jobSummary(j) {
  return {
    id: j.id,
    title: j.title,
    status: j.status,
    files_changed: j.filesChanged ?? null,
    duration_s: j.durationSeconds ?? null,
    created: j.createdAt,
  };
}

function jobDetail(j, { includeDiff = true, maxDiffChars = 15000 } = {}) {
  const lines = [
    `JOB ${j.id} — ${j.title}`,
    `status: ${j.status}${j.agyStatus ? ` (agy: ${j.agyStatus})` : ""}`,
    `branch: ${j.branch}   worktree: ${j.worktree}`,
    `base: ${j.baseLabel} (${j.base?.slice(0, 10)})`,
  ];
  if (j.durationSeconds != null) lines.push(`duration: ${j.durationSeconds}s, turns: ${j.turns ?? "?"}`);
  if (j.error) lines.push("", "ERROR:", truncate(j.error, 2500));
  if (j.softDenied?.length) lines.push("", "ACTIONS DENIED TO agy (soft-deny):", ...j.softDenied);
  if (j.response) lines.push("", "GEMINI'S RESPONSE:", truncate(j.response, 4000));
  if (j.diffStat) lines.push("", "DIFF STAT:", j.diffStat);
  if (includeDiff && j.patchBytes && j.patchFile && fs.existsSync(j.patchFile)) {
    const patch = fs.readFileSync(j.patchFile, "utf8");
    lines.push("", `DIFF (full patch: ${j.patchFile}):`, truncate(patch, maxDiffChars));
  }
  if (j.status === "done")
    lines.push("", "Next step: review the diff, then gemini_apply or gemini_discard (or a new gemini_delegate with the fixes).");
  return lines.join("\n");
}

async function waitFor(ids, seconds) {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    if (ids.every((id) => TERMINAL.has(jobs.get(id)?.status))) return true;
    await new Promise((r) => setTimeout(r, 1000));
  }
  return ids.every((id) => TERMINAL.has(jobs.get(id)?.status));
}

// ---------------------------------------------------------------- tools

// Tool definitions are shared with server.py.
const TOOLS = JSON.parse(fs.readFileSync(new URL("./tools.json", import.meta.url), "utf8"));

async function callTool(name, a = {}) {
  switch (name) {
    case "gemini_delegate": {
      if (!a.task?.trim()) throw new Error("task is required");
      const { job, usage } = await createJob(a);
      const wait = Math.min(a.wait_seconds || 0, 600);
      if (wait > 0) {
        await waitFor([job.id], wait);
        if (TERMINAL.has(job.status)) return jobDetail(job);
      }
      return [
        `Job ${job.id} started (${job.status}). Branch ${job.branch}, base: ${job.baseLabel}.`,
        `Active jobs: ${runningCount()}/${CONFIG.maxParallel} running, ${queue.length} queued.`,
        `24h usage (including running jobs): ${usage.committed + 1}/${CONFIG.dailyBudget}.`,
        `Use gemini_jobs with job_id="${job.id}" and wait_seconds to wait for the result.`,
      ].join("\n");
    }

    case "gemini_analyze": {
      const repo = await repoRoot(a.repo_dir);
      budgetCheck(a.force);
      const snapshot = async () => {
        const s = await run("git", ["status", "--porcelain=v1", "-uall"], { cwd: repo });
        const d = await run("git", ["diff"], { cwd: repo });
        return crypto.createHash("sha1").update(s.stdout + d.stdout).digest("hex");
      };
      const before = await snapshot();
      const timeoutMin = a.timeout_minutes || CONFIG.analyzeTimeoutMin;
      const res = await runAgy({
        cwd: repo,
        prompt: analyzePrompt({ question: a.question, paths: a.paths, maxWords: a.max_words || 600 }),
        model: a.model || CONFIG.analyzeModel || CONFIG.model,
        effort: a.effort || CONFIG.effort,
        timeoutMin,
        skipPermissions: false, // in the main repo: no auto-approval
        kind: "analyze",
      });
      const after = await snapshot();
      const out = [];
      if (res.notFound) throw new Error(`Comando "${CONFIG.agyBin}" not found. Install Antigravity CLI and run \`agy\` once to log in.`);
      if (!res.ok) out.push(`ERROR (${res.status})${res.quota ? " — QUOTA/RATE LIMIT" : ""}:\n${truncate(res.error, 2000)}`);
      if (before !== after)
        out.push("⚠️ WARNING: the repo state changed during the analysis. Check `git status` / `git diff`.");
      out.push(truncate(res.response || "(no response)", 16000));
      out.push(`\n[gemini_analyze: ${res.durationSeconds}s, turns ${res.turns}, tokens ${res.usage?.total_tokens ?? "?"}]`);
      return { text: out.join("\n\n"), isError: !res.ok && !res.response };
    }

    case "gemini_jobs": {
      if (a.job_id) {
        const j = jobs.get(a.job_id);
        if (!j) throw new Error(`job ${a.job_id} not found`);
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

    case "gemini_apply": {
      const j = jobs.get(a.job_id);
      if (!j) throw new Error(`job ${a.job_id} not found`);
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
              `Options: resolve conflicts by hand (if there are markers), check the state with \`git diff\`, or relaunch the task.`,
          };
        }
      }
      j.status = "applied";
      j.appliedAt = new Date().toISOString();
      saveJob(j);
      if (!a.keep_worktree) await removeWorktree(j);
      return `Applied (${how}) to the working tree of ${j.repo}. Changes are NOT committed.\n${r.stderr.trim() ? r.stderr.trim() + "\n" : ""}${j.diffStat || ""}\nNow: run tests/typecheck and polish if needed.`;
    }

    case "gemini_discard": {
      const j = jobs.get(a.job_id);
      if (!j) throw new Error(`job ${a.job_id} not found`);
      const wasRunning = j.status === "running";
      j.status = "discarded";
      saveJob(j);
      const c = children.get(j.id);
      if (c) { killTree(c, "SIGTERM"); setTimeout(() => killTree(c, "SIGKILL"), 3000).unref(); }
      const qi = queue.indexOf(j.id);
      if (qi >= 0) queue.splice(qi, 1);
      pump(); // a slot may have freed up: start the next queued job
      if (wasRunning) await new Promise((r) => setTimeout(r, 1500));
      await removeWorktree(j);
      return `Job ${j.id} discarded.`;
    }

    case "gemini_usage": {
      const u = usageWindow(24);
      const v = await run(CONFIG.agyBin, ["--version"]);
      const agy = v.error?.code === "ENOENT" ? "NOT FOUND" : (v.stdout || v.stderr).trim().split("\n")[0] || "ok";
      const active = [...jobs.values()].filter((j) => !TERMINAL.has(j.status));
      const review = [...jobs.values()].filter((j) => j.status === "done");
      return [
        `agy: ${agy} (${CONFIG.agyBin})`,
        `Last 24h: ${u.runs}/${CONFIG.dailyBudget} runs, ${u.turns} turns, ${u.total_tokens} tokens, ${u.failures} failed, ${u.quota_errors} quota errors`,
        `Active jobs: ${active.length} (max parallel ${CONFIG.maxParallel}); to review: ${review.length}`,
        `Config: model=${CONFIG.model || "(default agy)"} analyze_model=${CONFIG.analyzeModel || "(=model)"} effort=${CONFIG.effort || "(default)"} sandbox=${CONFIG.sandbox} skip_permissions_in_worktree=${CONFIG.skipPermissionsInWorktree} timeout_task=${CONFIG.taskTimeoutMin}m`,
        `State: ${CONFIG.stateDir}`,
      ].join("\n");
    }
  }
  throw new Error(`unknown tool: ${name}`);
}

// ---------------------------------------------------------------- session summary (for the SessionStart hook)

async function sessionSummary() {
  const v = await run(CONFIG.agyBin, ["--version"]);
  const lines = [];
  if (v.error?.code === "ENOENT") {
    lines.push(`[gemini-delegate] agy not found: delegation to Gemini is unavailable in this session (install Antigravity CLI and run \`agy\` to log in).`);
    return lines.join("\n");
  }
  const u = usageWindow(24);
  const left = Math.max(CONFIG.dailyBudget - u.runs, 0);
  lines.push(
    `[gemini-delegate] Delegation to Gemini available (MCP tools gemini_*). 24h budget: ${left}/${CONFIG.dailyBudget} runs left` +
      (u.quota_errors ? `, WARNING: ${u.quota_errors} recent quota errors` : "") + "."
  );
  let repo = null;
  try { repo = await repoRoot(); } catch {}
  if (repo) {
    const pending = [...jobs.values()].filter((j) => j.repo === repo && (j.status === "interrupted" || (["done", "failed"].includes(j.status) && j.filesChanged > 0)));
    if (pending.length)
      lines.push(
        `[gemini-delegate] Jobs from previous sessions still open in this repo: ${pending.map((j) => `${j.id} (${j.status}: ${j.title})`).join("; ")}. Review them with gemini_jobs, then gemini_apply or gemini_discard.`
      );
  }
  return lines.join("\n");
}

// ---------------------------------------------------------------- MCP stdio (JSON-RPC 2.0, newline-delimited)

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}

async function handle(msg) {
  const { id, method, params } = msg;
  const isRequest = id !== undefined && id !== null;
  try {
    let result;
    switch (method) {
      case "initialize":
        result = {
          protocolVersion: params?.protocolVersion || "2025-06-18",
          capabilities: { tools: { listChanged: false } },
          serverInfo: { name: "gemini-delegate", version: VERSION },
          instructions:
            "Tools for delegating work to Gemini (Antigravity CLI) using the user's Google subscription. You remain the manager: you plan, delegate well-specified tasks, review the diffs, and apply or discard them.",
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
      try { msg = JSON.parse(line); } catch {
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
