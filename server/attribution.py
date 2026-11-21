# Per-model attribution ("blame"): which worker and model wrote which lines of a repo.
#
# When a job is applied, the server appends one Agent Trace record (https://agent-trace.dev, v0.1.0) to
# <git-common-dir>/capocantiere/attribution.jsonl: inside .git, so it stays with the repo and never
# enters a commit or a push. Every non-trivial line the job added is kept as a content fingerprint, so
# blame matches lines by content, not by position: it survives commits, rebases and moved lines, and a
# line edited after the apply is no longer attributed to the worker.
#
# Record layout (fields not in the Agent Trace schema live under metadata["dev.capocantiere"]):
#   version, id, timestamp, tool {name, version}
#   files[]: path, conversations[{contributor {type: "ai", model_id: "<worker>/<model>"},
#            ranges[{start_line, end_line, content_hash}]}]   (1-based, inclusive, positions in the
#            working tree right after the apply; there is no vcs revision because nothing is committed)
#   metadata["dev.capocantiere"]: job, title, worker, model, kind, tier, quality, base, head, patch_sha256,
#            fingerprints {path: [fingerprint of each non-trivial added line, in file order]}
#
# Standard library only.

import hashlib
import json
import os
import re
import threading
import uuid

from common import iso_now, run

SPEC_VERSION = "0.1.0"
VENDOR = "dev.capocantiere"
LOG_NAME = "attribution.jsonl"
_LOCK = threading.Lock()

# ---------------------------------------------------------------- lines


def normalize(line):
    return line.strip()


def trivial(line):
    """Blank lines, lone brackets or punctuation: fewer than 3 letters, digits or underscores."""
    return sum(1 for c in line if c.isalnum() or c == "_") < 3


def fingerprint(line):
    return hashlib.sha1(normalize(line).encode("utf-8", "surrogateescape")).hexdigest()[:16]


def read_lines(path):
    """The file's lines without line endings, or None if it can't be read as text."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "surrogateescape").splitlines()


# ---------------------------------------------------------------- where the log lives


def log_path(repo):
    """<git-common-dir>/capocantiere/attribution.jsonl: shared by every worktree of the repo."""
    r = run("git", ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=repo)
    d = r["stdout"].strip() if r["code"] == 0 else ""
    if not d:
        r = run("git", ["rev-parse", "--git-common-dir"], cwd=repo)
        d = r["stdout"].strip() if r["code"] == 0 else ".git"
        d = os.path.join(repo, d)
    return os.path.join(d, "capocantiere", LOG_NAME)


def read_records(repo):
    """Every record of the repo, oldest first. Unreadable lines are skipped."""
    path = log_path(repo)
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("metadata"), dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def _meta(rec):
    m = (rec.get("metadata") or {}).get(VENDOR)
    return m if isinstance(m, dict) else {}


# ---------------------------------------------------------------- reading a patch


def _unquote(p):
    """git quotes paths with unusual characters as C strings: "a/caf\\303\\251.txt"."""
    if not (len(p) >= 2 and p[0] == '"' and p[-1] == '"'):
        return p
    raw, i, s = bytearray(), 1, p[:-1]
    esc = {"n": 10, "t": 9, '"': 34, "\\": 92, "a": 7, "b": 8, "f": 12, "r": 13, "v": 11}
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n in "01234567":
                raw.append(int(s[i + 1:i + 4], 8))
                i += 4
                continue
            raw.append(esc.get(n, ord(n)))
            i += 2
            continue
        raw += c.encode("utf-8", "surrogateescape")
        i += 1
    return raw.decode("utf-8", "surrogateescape")


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_patch(text):
    """{path: [hunk]} for the new side of a unified diff; hunk = (new_start, [(tag, line)]) with tag
    " " for context and "+" for added lines. Deleted and binary files are left out."""
    files, path, hunk = {}, None, None
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            path, hunk = None, None
            continue
        if line.startswith("+++ "):
            p = _unquote(line[4:].rstrip("\t"))
            path = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            hunk = None
            continue
        m = _HUNK.match(line)
        if m and path:
            hunk = (int(m.group(1)), [])
            files.setdefault(path, []).append(hunk)
            continue
        if hunk is None or not line or line[0] not in " +-\\":
            continue
        if line[0] in " +":
            hunk[1].append((line[0], line[1:]))
    return files


def _locate(lines, start, new_side):
    """0-based index where new_side (the hunk's new-side lines) sits in lines, nearest to start; None if
    nowhere (e.g. the apply merged it with other edits)."""
    n = len(new_side)
    if n == 0:
        return None
    want = [normalize(x) for x in new_side]
    guess = max(0, start - 1)
    for d in range(len(lines) + 1):
        for i in ((guess - d, guess + d) if d else (guess,)):
            if 0 <= i <= len(lines) - n and [normalize(x) for x in lines[i:i + n]] == want:
                return i
    return None


def _ranges(lines, positions):
    """Contiguous runs of the given 1-based positions, trimmed of trivial lines at both ends; a trivial
    line inside a run (a blank line in a function) does not break it."""
    out, run_ = [], []
    for p in sorted(set(positions)):
        if run_ and p != run_[-1] + 1:
            out.append(run_)
            run_ = []
        run_.append(p)
    if run_:
        out.append(run_)
    ranges = []
    for r in out:
        keep = [p for p in r if not trivial(lines[p - 1])]
        if not keep:
            continue
        body = [normalize(lines[p - 1]) for p in range(keep[0], keep[-1] + 1) if not trivial(lines[p - 1])]
        h = hashlib.sha1("\n".join(body).encode("utf-8", "surrogateescape")).hexdigest()
        ranges.append({"start_line": keep[0], "end_line": keep[-1], "content_hash": "sha1:" + h})
    return ranges


# ---------------------------------------------------------------- recording


def build_record(repo, job, patch_bytes, quality=None, tool_version=None, head=None):
    """The Agent Trace record of an applied job, or None if it added no non-trivial line."""
    text = patch_bytes.decode("utf-8", "surrogateescape")
    worker = job.get("worker") or "worker"
    model = job.get("effectiveModel") or job.get("model") or "default"
    files, prints = [], {}
    for path, hunks in parse_patch(text).items():
        added = [l for _, h in hunks for t, l in h if t == "+" and not trivial(l)]
        if not added:
            continue
        prints[path] = [fingerprint(l) for l in added]
        lines = read_lines(os.path.join(repo, path))
        positions = []
        if lines is not None:
            for start, h in hunks:
                i = _locate(lines, start, [l for _, l in h])
                if i is not None:
                    positions += [i + k + 1 for k, (t, _) in enumerate(h) if t == "+"]
        files.append({"path": path, "conversations": [{
            "contributor": {"type": "ai", "model_id": "%s/%s" % (worker, model)},
            "ranges": _ranges(lines, positions) if lines is not None else [],
        }]})
    if not prints:
        return None
    meta = {
        "job": job.get("id"), "title": job.get("title"), "worker": worker, "model": model,
        "kind": job.get("kind"), "tier": job.get("tier"), "quality": quality,
        "base": job.get("base"), "head": head, "patch_sha256": hashlib.sha256(patch_bytes).hexdigest(),
        "fingerprints": prints,
    }
    return {
        "version": SPEC_VERSION,
        "id": str(uuid.uuid4()),
        "timestamp": iso_now(),
        "tool": {"name": "capocantiere", "version": tool_version or ""},
        "files": files,
        "metadata": {VENDOR: {k: v for k, v in meta.items() if v is not None}},
    }


def record_apply(repo, job, quality=None, tool_version=None):
    """Append the record of a job just applied to repo's working tree. Returns the record (or None)."""
    with open(job["patchFile"], "rb") as f:
        patch = f.read()
    r = run("git", ["rev-parse", "HEAD"], cwd=repo)
    rec = build_record(repo, job, patch, quality, tool_version, r["stdout"].strip() if r["code"] == 0 else None)
    if rec is None:
        return None
    path = log_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=True, separators=(",", ":")) + "\n")
    return rec


# ---------------------------------------------------------------- blame


def _rel(repo, path):
    p = path if os.path.isabs(path) else os.path.join(repo, path)
    rel = os.path.relpath(os.path.realpath(p), os.path.realpath(repo))
    if rel.startswith(".."):
        raise RuntimeError("%s is outside the repo %s" % (path, repo))
    return rel.replace(os.sep, "/")


def attribute(repo, rel, lines):
    """For each line: the newest record of that file holding its fingerprint, "" for a line no worker
    wrote, None for a trivial line."""
    recs = [r for r in read_records(repo) if rel in (_meta(r).get("fingerprints") or {})]
    sets = [(r, set(_meta(r)["fingerprints"][rel])) for r in reversed(recs)]
    out = []
    for line in lines:
        if trivial(line):
            out.append(None)
            continue
        fp = fingerprint(line)
        out.append(next((r for r, s in sets if fp in s), ""))
    return out


def _fill(attrs):
    """Trivial lines take the attribution of their neighbors: both sides if they agree, else the one
    before (a closing bracket belongs with what it closes)."""
    out = list(attrs)
    for i, a in enumerate(out):
        if a is not None:
            continue
        prev = next((out[j] for j in range(i - 1, -1, -1) if out[j] is not None), None)
        nxt = next((attrs[j] for j in range(i + 1, len(attrs)) if attrs[j] is not None), None)
        out[i] = prev if prev is not None else (nxt if nxt is not None else "")
    return out


def _who(rec):
    if not rec:
        return "not from a worker"
    m = _meta(rec)
    title = (m.get("title") or "").replace("\n", " ")
    return "%s %s/%s %s%s%s" % (m.get("job"), m.get("worker"), m.get("model"), (rec.get("timestamp") or "")[:10],
                                " [%s]" % m["kind"] if m.get("kind") else "",
                                ' "%s"' % (title[:60] + ("..." if len(title) > 60 else "")) if title else "")


def blame(repo, path, start=None, end=None):
    """Compact attribution of path's lines start..end (1-based, inclusive): one line per run."""
    rel = _rel(repo, path)
    lines = read_lines(os.path.join(repo, rel))
    if lines is None:
        raise RuntimeError("%s: not found or not a text file" % rel)
    if not lines:
        return "%s: empty file" % rel
    s = max(1, int(start or 1))
    e = min(len(lines), int(end or len(lines)))
    if s > e:
        raise RuntimeError("%s has %d lines: nothing in %s-%s" % (rel, len(lines), start, end))
    attrs = _fill(attribute(repo, rel, lines))[s - 1:e]
    out, i = [], 0
    while i < len(attrs):
        j = i
        while j + 1 < len(attrs) and (attrs[j + 1] is attrs[i] or not (attrs[j + 1] or attrs[i])):
            j += 1
        out.append("  %d-%d  %s" % (s + i, s + j, _who(attrs[i])))
        i = j + 1
    worker_lines = sum(1 for a in attrs if a)
    head = "%s:%d-%d  %d/%d lines from workers" % (rel, s, e, worker_lines, len(attrs))
    if not any(_meta(r).get("fingerprints", {}).get(rel) for r in read_records(repo)):
        head += " (no applied job ever touched this file)"
    return "\n".join([head] + out)


# ---------------------------------------------------------------- survival (for a future trust signal)


def survival(repo, job_id):
    """How many of an applied job's non-trivial lines still exist in the working tree, by content.
    Returns (alive, total), or None if the job has no record. Not used in any score yet."""
    rec = next((r for r in reversed(read_records(repo)) if _meta(r).get("job") == job_id), None)
    if rec is None:
        return None
    alive = total = 0
    for rel, prints in (_meta(rec).get("fingerprints") or {}).items():
        lines = read_lines(os.path.join(repo, rel)) or []
        have = {}
        for line in lines:
            if not trivial(line):
                fp = fingerprint(line)
                have[fp] = have.get(fp, 0) + 1
        for fp in prints:
            total += 1
            if have.get(fp, 0) > 0:
                have[fp] -= 1
                alive += 1
    return alive, total
