# What the manager sees of a job's diff, and what it has seen before an apply.
#
# A patch larger than the budget (max_diff_chars) is not cut at a character count: every file is listed with its
# flags; files where a few lines change what runs (CI, hooks, build and package manifests, scripts, symlinks, mode
# changes) are always shown whole; the others whole, while they fit; the rest is listed as not shown. The job
# remembers which files the manager was shown: apply refuses a patch with a sensitive file it never saw, unless
# told to go ahead (unreviewed_ok), and names the ordinary files it applied unseen.
# Standard library only. server.mjs has the same logic.

import os
import re

# Paths where a change decides what gets run: by CI, by git, by the build, by an editor or agent.
SENSITIVE_PREFIXES = (".github/", ".gitlab/", ".circleci/", ".buildkite/", ".githooks/", ".husky/", ".devcontainer/",
                      ".vscode/", ".idea/", ".claude/", ".cursor/", ".vibe/", ".copilot/", ".gemini/")
SENSITIVE_NAMES = {
    ".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "Jenkinsfile", "bitbucket-pipelines.yml",
    ".gitattributes", ".gitmodules", ".gitignore", ".pre-commit-config.yaml", ".mcp.json", "AGENTS.md", "CLAUDE.md",
    "Makefile", "GNUmakefile", "makefile", "CMakeLists.txt", "justfile", "Justfile", "Taskfile.yml", "Rakefile",
    "Dockerfile", "Containerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "package.json", ".npmrc", ".yarnrc", ".yarnrc.yml", "pyproject.toml", "setup.py", "setup.cfg", "tox.ini",
    "noxfile.py", "conftest.py", "pytest.ini", "Pipfile", "Cargo.toml", "build.rs", "go.mod", "build.gradle",
    "build.gradle.kts", "settings.gradle", "pom.xml", "Gemfile", ".envrc", "flake.nix", "shell.nix", "default.nix",
}
SENSITIVE_EXT = (".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd")


def _unquote(p):
    """git quotes paths with unusual characters: "a/caf\\303\\251"."""
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        try:
            return p[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
        except Exception:
            return p[1:-1]
    return p


def _strip_prefix(p):
    p = _unquote(p.strip())
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse(patch):
    """The files of a `git diff` patch: [{path, text, added, removed, flags}], in patch order."""
    out = []
    starts = [m.start() for m in re.finditer(r"^diff --git ", patch, re.M)]
    for i, s in enumerate(starts):
        text = patch[s:starts[i + 1] if i + 1 < len(starts) else len(patch)]
        lines = text.split("\n")
        path, old_path, flags = None, None, []
        added = removed = 0
        in_hunk = False
        for ln in lines[1:]:
            if ln.startswith("@@"):
                in_hunk = True
                continue
            if in_hunk:
                if ln.startswith("+"):
                    added += 1
                elif ln.startswith("-"):
                    removed += 1
                continue
            if ln.startswith("+++ ") and ln[4:] != "/dev/null":
                path = _strip_prefix(ln[4:])
            elif ln.startswith("--- ") and ln[4:] != "/dev/null":
                old_path = _strip_prefix(ln[4:])
            elif ln.startswith("rename to "):
                path = _unquote(ln[10:])
            elif ln.startswith("rename from "):
                old_path = _unquote(ln[12:])
                flags.append("renamed")
            elif ln.startswith("new file mode "):
                flags.append("new")
                if ln.endswith("755"):
                    flags.append("executable")
                if ln.endswith("120000"):
                    flags.append("symlink")
            elif ln.startswith("deleted file mode "):
                flags.append("deleted")
            elif ln.startswith("new mode "):
                flags.append("mode %s" % ln[9:])
                if ln.endswith("120000"):
                    flags.append("symlink")
            elif ln.startswith("index ") and ln.endswith(" 120000"):
                flags.append("symlink")
            elif ln.startswith("GIT binary patch") or (ln.startswith("Binary files ") and ln.endswith(" differ")):
                flags.append("binary")
        if not path:
            path = old_path
        if not path:  # no ---/+++ lines (mode change, empty file, binary without --binary): from the first line
            m = re.match(r"diff --git (\"?a/.*?\"?) (\"?b/.*\"?)$", lines[0])
            path = _strip_prefix(m.group(2)) if m else lines[0][11:]
        out.append({"path": path, "text": text, "added": added, "removed": removed, "flags": flags,
                    "sensitive": sensitive(path, flags)})
    return out


def sensitive(path, flags=()):
    """Why a change to this file decides what gets run, or None."""
    if "symlink" in flags:
        return "symlink"
    if any(f.startswith("mode ") for f in flags) or "executable" in flags:
        return "executable bit"
    p = path.replace("\\", "/")
    base = os.path.basename(p)
    for pre in SENSITIVE_PREFIXES:
        if p.startswith(pre) or ("/" + pre) in p:
            return pre.rstrip("/")
    if base in SENSITIVE_NAMES or re.match(r"requirements.*\.txt$", base):
        return base
    if base.endswith(SENSITIVE_EXT):
        return "script"
    return None


def _binary_text(f):
    """A binary file's section without its base85 payload: nothing a reviewer can read."""
    head = []
    for ln in f["text"].split("\n"):
        if ln.startswith("GIT binary patch"):
            break
        head.append(ln)
    return "\n".join(head).rstrip("\n") + "\n(binary content not shown)\n"


def _row(f):
    tags = list(f["flags"])
    if f["sensitive"]:
        tags.append("SENSITIVE: " + f["sensitive"])
    return "  %s  +%d -%d%s" % (f["path"], f["added"], f["removed"], "  [%s]" % ", ".join(tags) if tags else "")


def render(patch, budget, only=None):
    """(text, paths shown whole). only: paths (or folders) to show, whole, ignoring the budget."""
    files = parse(patch)
    if only:
        want = [o.rstrip("/") for o in only]
        pick = [f for f in files if any(f["path"] == w or f["path"].startswith(w + "/") for w in want)]
        if not pick:
            return "No file of this job's diff matches %s. Files: %s" % (
                ", ".join(only), ", ".join(f["path"] for f in files)), []
        return "\n".join(_binary_text(f) if "binary" in f["flags"] else f["text"] for f in pick), [f["path"] for f in pick]
    if len(patch) <= budget and not any("binary" in f["flags"] for f in files):
        return patch, [f["path"] for f in files]
    shown, parts, used = [], [], 0
    # sensitive files first, always whole; binaries as their header; then the others whole, while they fit
    order = sorted(range(len(files)), key=lambda i: (not files[i]["sensitive"], i))
    for i in order:
        f = files[i]
        body = _binary_text(f) if "binary" in f["flags"] else f["text"]
        if f["sensitive"] or "binary" in f["flags"] or used + len(body) <= budget:
            parts.append(body)
            used += len(body)
            shown.append(f["path"])
    hidden = [f for f in files if f["path"] not in shown]
    head = ["FILES (%d; sensitive ones are always shown whole):" % len(files)] + [_row(f) for f in files]
    tail = []
    if hidden:
        tail = ["", "NOT SHOWN (%d, over the %d-character budget): %s" % (len(hidden), budget, ", ".join(
            "%s (+%d -%d)" % (f["path"], f["added"], f["removed"]) for f in hidden)),
                'See them with jobs job_id=... files=["path", ...], or a larger max_diff_chars.']
    return "\n".join(head + [""] + ["".join(parts).rstrip("\n")] + tail), shown


def unseen(patch, seen):
    """(sensitive files never shown, ordinary files never shown) of a patch, as [(path, why)] and [path]."""
    seen = set(seen or [])
    files = [f for f in parse(patch) if f["path"] not in seen]
    return [(f["path"], f["sensitive"]) for f in files if f["sensitive"]], [f["path"] for f in files if not f["sensitive"]]
