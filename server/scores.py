# Trust scores: how well each arm (worker:model) does each kind of task, learned from job outcomes.
#
# Raw events are appended to <state>/scores.jsonl and the scores are computed when read, so the formula
# can change without losing the history. Event fields:
#   ts (ms), id (job or analysis id), repo, worker, requested (model the server asked for, "" = CLI default),
#   model (the arm's model: the one the CLI reported, else the requested one, else "default"), kind, tier,
#   event, reward (0..1, or null = no vote).
# Only the LATEST event of each id counts: an apply after a failed job replaces its 0, and a discard with
# reason "obsolete" (or rate ... obsolete) withdraws a vote.
#
# Score: a Beta per arm and kind, with exponential time decay (weight 0.5 ** (age_days / half_life_days)).
# The prior of a kind is the arm's global score (all kinds): alpha0 = 1 + M * p_glob, beta0 = 1 + M * (1 - p_glob),
# so a kind with little data stays close to the model's overall behavior. The global prior of an arm is
# slightly optimistic (mean about 0.6, a bit higher for workers early in the configured order), so new arms get tried.
# Selection is Thompson sampling: draw theta ~ Beta(alpha, beta) per candidate and sort by theta.
#
# Standard library only.

import json
import os
import random
import threading
import time

from common import STATE_DIR, env, now_ms

SCORES_FILE = os.path.join(STATE_DIR, "scores.jsonl")
KINDS = ("implement", "tests", "refactor", "docs", "analysis")
TIERS = ("fast", "standard", "strong", "frontier")
DEFAULT_TIER = {"implement": "standard", "analyze": "fast"}  # per role, when none is requested
QUALITY = {"good": 1.0, "minor_fixes": 0.6, "major_fixes": 0.3, "bad": 0.0}
M = 4  # strength of the global score as the prior of a kind
PRIOR_MEAN = 0.6  # global prior of an arm never seen
PRIOR_SPREAD = 0.1  # the first worker in `order` gets PRIOR_MEAN + SPREAD/2, the last PRIOR_MEAN - SPREAD/2
PRIOR_STRENGTH = 4  # pseudo-events behind the global prior
DAY_MS = 86400_000

_LOCK = threading.Lock()
_seed = env.get("CAPOCANTIERE_SEED")
RNG = random.Random(int(_seed) if _seed and _seed.lstrip("-").isdigit() else None)


def arm_model(requested, effective):
    return effective or requested or "default"


def record(entry):
    """Append one event. entry: id, repo, worker, requested, model, kind, tier, event, reward."""
    e = dict({"ts": now_ms()}, **entry)
    with _LOCK, open(SCORES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def read_events():
    """Every event, oldest first. Unreadable lines are skipped."""
    out = []
    try:
        with open(SCORES_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("id") and e.get("worker"):
                    out.append(e)
    except OSError:
        pass
    return out


def last_event(job_id, events=None):
    for e in reversed(events if events is not None else read_events()):
        if e.get("id") == job_id:
            return e
    return None


def votes(events):
    """The latest event of each id, when it carries a vote."""
    latest = {}
    for e in events:
        latest[e["id"]] = e
    return [e for e in latest.values() if isinstance(e.get("reward"), (int, float))]


def learned_model(worker, requested, events):
    """The arm a run with this requested model lands on: the model the CLI reported last time it was asked
    for it (e.g. what "CLI default" currently means), else the requested model itself."""
    for e in reversed(events):
        if e.get("worker") == worker and (e.get("requested") or "") == (requested or "") and e.get("model"):
            return e["model"]
    return arm_model(requested, None)


class Table:
    """Decayed sums per arm ("worker:model") and per (arm, kind), read once."""

    def __init__(self, events=None, half_life_days=30, now=None, order=()):
        now = now if now is not None else now_ms()
        self.events = events if events is not None else read_events()
        self.order = list(order)
        self.glob = {}  # arm -> [a, b, n]
        self.kind = {}  # (arm, kind) -> [a, b, n]
        hl = max(float(half_life_days or 30), 0.001)
        for e in votes(self.events):
            w = 0.5 ** (max(now - (e.get("ts") or now), 0) / DAY_MS / hl)
            r = min(max(float(e["reward"]), 0.0), 1.0)
            arm = "%s:%s" % (e["worker"], e.get("model") or "default")
            for key, d in ((arm, self.glob), ((arm, e.get("kind") or "implement"), self.kind)):
                s = d.setdefault(key, [0.0, 0.0, 0.0])
                s[0] += w * r
                s[1] += w * (1 - r)
                s[2] += w

    def prior_mean(self, worker):
        if worker in self.order and len(self.order) > 1:
            i = self.order.index(worker)
            return PRIOR_MEAN + PRIOR_SPREAD * (0.5 - i / (len(self.order) - 1))
        return PRIOR_MEAN

    def glob_params(self, arm):
        pm = self.prior_mean(arm.split(":", 1)[0])
        a, b, _ = self.glob.get(arm, (0.0, 0.0, 0.0))
        return PRIOR_STRENGTH * pm + a, PRIOR_STRENGTH * (1 - pm) + b

    def params(self, arm, kind):
        """(alpha, beta) of the arm for this kind."""
        ga, gb = self.glob_params(arm)
        p = ga / (ga + gb)
        a, b, _ = self.kind.get((arm, kind), (0.0, 0.0, 0.0))
        return 1 + M * p + a, 1 + M * (1 - p) + b

    def mean(self, arm, kind):
        a, b = self.params(arm, kind)
        return a / (a + b)

    def n(self, arm, kind=None):
        s = self.glob.get(arm) if kind is None else self.kind.get((arm, kind))
        return s[2] if s else 0.0

    def sample(self, arm, kind):
        a, b = self.params(arm, kind)
        return RNG.betavariate(a, b)

    def arms(self):
        return sorted(self.glob)

    def top_kind(self, arm):
        ks = [(s[2], k) for (a, k), s in self.kind.items() if a == arm]
        return max(ks)[1] if ks else None


def rank(candidates, kind, table):
    """Thompson sampling. candidates: dicts with at least "arm". Returns them sorted, each with "theta"."""
    out = [dict(c, theta=round(table.sample(c["arm"], kind), 3)) for c in candidates]
    out.sort(key=lambda c: -c["theta"])
    return out


def explain(ranking, limit=4):
    """One line: why the first arm was chosen."""
    if not ranking:
        return ""
    parts = ["%s %.2f" % (c["arm"], c["theta"]) for c in ranking[:limit]]
    return "chosen by trust score (Thompson draws): " + " > ".join(parts) + (" ..." if len(ranking) > limit else "")


def summary_lines(table):
    """One line per arm with data: mean and weighted events for its most used kind."""
    lines = []
    for arm in table.arms():
        k = table.top_kind(arm) or "implement"
        lines.append("  %s  %.2f (n~%d, %s)" % (arm, table.mean(arm, k), round(table.n(arm, k)), k))
    return lines


def report(table, half_life_days):
    """The detail for /capocantiere:scores."""
    if not table.glob:
        return ("No trust scores yet: they come from applied, discarded and failed jobs (and rated analyses). "
                "Until then workers are tried in the configured order, with some exploration.")
    lines = ["Trust scores (mean of Beta, n = weighted events, half-life %s days):" % half_life_days, ""]
    width = max(len(a) for a in table.arms())
    lines.append("  %s  %s" % ("arm".ljust(width), "  ".join("%-12s" % k for k in ("overall",) + KINDS)))
    for arm in table.arms():
        ga, gb = table.glob_params(arm)
        cells = ["%.2f (%d)" % (ga / (ga + gb), round(table.glob[arm][2]))]
        for k in KINDS:
            n = table.kind.get((arm, k))
            cells.append("%.2f (%d)" % (table.mean(arm, k), round(n[2])) if n else "-")
        lines.append("  %s  %s" % (arm.ljust(width), "  ".join("%-12s" % c for c in cells)))
    recent = sorted(votes(table.events), key=lambda e: e.get("ts") or 0)[-5:]
    if recent:
        lines += ["", "Last votes:"]
        for e in reversed(recent):
            lines.append("  %s %s %s:%s %s %s=%.1f" % (
                time.strftime("%m-%d %H:%M", time.localtime((e.get("ts") or 0) / 1000)), e["id"], e["worker"],
                e.get("model") or "default", e.get("kind") or "implement", e.get("event"), e["reward"]))
    return "\n".join(lines)
