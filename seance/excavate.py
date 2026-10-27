"""Excavation: everything the ghost knows comes from here.

Pure evidence gathering from git and the working tree. No model calls,
no network. If a fact is not dug up here, the ghost is not allowed to say it.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone

DEAD_AFTER_DAYS = int(os.getenv("SEANCE_DEAD_AFTER_DAYS", "14"))

SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
    ".next", ".idea", ".vscode", "target", ".mypy_cache", ".pytest_cache", "site-packages",
    ".gradle", "coverage", ".turbo", ".cache", "AppData", "Library", "Program Files",
    "Program Files (x86)", "Windows", "$Recycle.Bin",
}
TEXT_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".html", ".css", ".scss", ".md", ".txt",
    ".json", ".yml", ".yaml", ".toml", ".sh", ".ps1", ".bat", ".sql", ".kt", ".swift",
    ".vue", ".svelte", ".dart", ".r", ".ini", ".cfg",
}
LANG_NAMES = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "React (JSX)", ".ts": "TypeScript",
    ".tsx": "React (TSX)", ".java": "Java", ".c": "C", ".cpp": "C++", ".cc": "C++",
    ".h": "C/C++ headers", ".cs": "C#", ".go": "Go", ".rs": "Rust", ".rb": "Ruby",
    ".php": "PHP", ".html": "HTML", ".css": "CSS", ".scss": "SCSS", ".kt": "Kotlin",
    ".swift": "Swift", ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart", ".sql": "SQL",
    ".ipynb": "Jupyter notebooks",
}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
    "Cargo.lock", "composer.lock", "bun.lockb", "uv.lock",
}
# Files the ghost is never allowed to open, even locally.
SECRET_FILE = re.compile(r"(^|/)\.env|\.pem$|\.key$|(^|/)id_rsa|secret|credential", re.I)
KEY_FILE = re.compile(
    r"(^|/)((main|app|index|server|api|routes?|cli|manage|App)\.(py|js|jsx|ts|tsx|go|rs|java)"
    r"|package\.json|requirements\.txt|pyproject\.toml|Dockerfile|docker-compose\.ya?ml)$"
)
SECRET_PATTERNS = [
    # NAME = "literal value"  (quoted literals only, so ordinary code survives)
    re.compile(
        r"(?i)\b([a-z0-9_]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|access[_-]?key|private[_-]?key)[a-z0-9_]*)"
        r"(\s*[:=]\s*)([\"'])([^\"'\s]{8,})\3"
    ),
    # well-known key shapes, anywhere
    re.compile(
        r"\b(sk-[A-Za-z0-9_\-]{16,}|gsk_[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z\-_]{30,}|xox[baprs]-[A-Za-z0-9\-]{10,}|hf_[A-Za-z0-9]{20,})\b"
    ),
]
TODO_RE = re.compile(r"(?:#|//|/\*|<!--|--|\*|;)\s*(TODO|FIXME|HACK|XXX|BUG)\b[\s:(\-]*(.*)")
CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+(.+)$")
LAZY_WORDS = re.compile(
    r"^(wip|tmp|temp|test|testing|try|trying|attempt|asdf|stuff|changes|update|updates|minor|"
    r"fix|fixes|small fix|quick fix|todo|\.+|idk|save|saving|commit|done\?)$|\b(wip|fix later|broken|"
    r"not working|doesn'?t work|half[- ]done|temp fix)\b",
    re.I,
)
FINAL_WORDS = re.compile(r"\b(final|finished|finish|complete[d]?|release|deploy(ed)?|submi(t|ssion)|ship(ped)?|v\d+(\.\d+)*|done)\b", re.I)
BUG_WORDS = re.compile(r"\b(fix|bug|error|broken|crash|issue|debug|hotfix|patch)\b", re.I)
SETUP_WORDS = re.compile(r"\b(init|initial|first commit|setup|set up|scaffold|boilerplate|create react app|vite)\b", re.I)

CAUSES = [
    "Stillborn",
    "Abandoned mid-thought",
    "Mission accomplished, then forgotten",
    "Blocked by a bug",
    "The hackathon ended",
    "Scope swallowed it",
    "Never got past setup",
    "Replaced by a newer idea",
    "Slow fade",
    "Quietly forgotten",
]

LOG_FMT = "%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e"
MAX_COMMITS = 1500


# ---------------------------------------------------------------- helpers

def git(repo: str, *args: str, timeout: int = 20) -> str:
    """Run git read-only inside `repo`. Returns stdout, or '' on any failure."""
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=off", "-c", "safe.directory=*", "-C", repo, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def redact(text: str) -> tuple[str, int]:
    """Hide anything that looks like a secret before the model ever sees it."""
    count = 0

    def _named(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]{m.group(3)}"

    def _shape(_m: re.Match) -> str:
        nonlocal count
        count += 1
        return "[REDACTED]"

    text = SECRET_PATTERNS[0].sub(_named, text)
    text = SECRET_PATTERNS[1].sub(_shape, text)
    return text, count


def _parse_log(raw: str) -> list[dict]:
    commits = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\r\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) != 5:
            continue
        full, short, author, date, subject = parts
        try:
            when = datetime.fromisoformat(date.strip())
        except ValueError:
            continue
        commits.append({"hash": full, "short": short, "author": author.strip(),
                        "when": when, "subject": subject.strip()})
    commits.sort(key=lambda c: c["when"], reverse=True)
    return commits


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _trunc(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------------------------------------------------------------- discovery

def find_repos(root: str, max_depth: int = 4, limit: int = 80) -> list[str]:
    """Walk a folder and return every git repository under it (not nested ones)."""
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        return []
    found: list[str] = []
    stack = [(root, 0)]
    while stack and len(found) < limit:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        if any(e.name == ".git" for e in entries):
            found.append(current)
            continue
        if depth >= max_depth:
            continue
        for e in entries:
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir and e.name not in SKIP_DIRS and not e.name.startswith("."):
                stack.append((e.path, depth + 1))
    return sorted(found, key=str.lower)


def head_state(repo: str) -> str:
    """A cheap fingerprint of 'has anything changed since I last dug here'."""
    head = git(repo, "rev-parse", "HEAD").strip()
    status = git(repo, "status", "--porcelain")
    return head + ":" + hashlib.sha1(status.encode("utf-8", "replace")).hexdigest()[:10]


# ---------------------------------------------------------------- vitals

def _read_vitals(repo: str, now: datetime) -> tuple[dict, list[dict]]:
    name = os.path.basename(os.path.normpath(repo))
    log_args = ("--branches", "--tags", f"-n{MAX_COMMITS}", f"--pretty=format:{LOG_FMT}")
    commits = _parse_log(git(repo, "log", "HEAD", *log_args) or git(repo, "log", *log_args))
    head = git(repo, "rev-parse", "--short", "HEAD").strip()

    modified, untracked = [], []
    for line in git(repo, "status", "--porcelain").splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip().strip('"')
        (untracked if code == "??" else modified).append(path)
    dirty_stat = git(repo, "diff", "HEAD", "--shortstat").strip() if commits else ""
    stashes = [s.strip() for s in git(repo, "stash", "list").splitlines() if s.strip()]
    unmerged = [b.strip().lstrip("*+ ").strip() for b in git(repo, "branch", "--no-merged").splitlines() if b.strip()]

    try:
        root_files = os.listdir(repo)
    except OSError:
        root_files = []
    readme = next((f for f in root_files if f.lower().startswith("readme")), None)

    if not commits:
        vitals = {
            "path": repo, "name": name, "status": "stillborn", "born": None, "died": None,
            "days_silent": None, "lifespan_days": 0, "commits": 0, "last_words": "",
            "last_hash": "", "first_words": "", "authors": [], "modified": modified[:12],
            "untracked": untracked[:12], "dirty_stat": "", "stashes": stashes[:6],
            "unmerged": unmerged[:6], "readme": readme, "head": head,
        }
        return vitals, []

    last = commits[0]
    if len(commits) < MAX_COMMITS:
        born_commit = commits[-1]
        total = len(commits)
    else:
        roots = git(repo, "rev-list", "--max-parents=0", "--branches").split()
        root_commits = _parse_log(git(repo, "log", "--no-walk", f"--pretty=format:{LOG_FMT}", *roots[:20])) if roots else []
        born_commit = min(root_commits, key=lambda c: c["when"]) if root_commits else commits[-1]
        try:
            total = int(git(repo, "rev-list", "--count", "--branches", "--tags").strip() or len(commits))
        except ValueError:
            total = len(commits)

    born, died = born_commit["when"], last["when"]
    days_silent = max(0, (now - died).days)
    vitals = {
        "path": repo,
        "name": name,
        "status": "dead" if days_silent >= DEAD_AFTER_DAYS else "alive",
        "born": _iso(born),
        "died": _iso(died),
        "days_silent": days_silent,
        "lifespan_days": max(0, (died - born).days),
        "commits": total,
        "last_words": last["subject"],
        "last_hash": last["short"],
        "first_words": born_commit["subject"],
        "authors": [a for a, _ in Counter(c["author"] for c in commits).most_common(6)],
        "modified": modified[:12],
        "untracked": untracked[:12],
        "dirty_stat": dirty_stat,
        "stashes": stashes[:6],
        "unmerged": unmerged[:6],
        "readme": readme,
        "head": head,
    }
    return vitals, commits


def signals(v: dict, commits: list[dict], todo_count: int | None = None,
            unchecked: int | None = None, readme_chars: int | None = None) -> list[dict]:
    """Deterministic omens. The model may overrule them, but it starts from these."""
    out: list[dict] = []

    def add(key: str, label: str, detail: str) -> None:
        out.append({"key": key, "label": label, "detail": detail})

    if v["status"] == "stillborn":
        add("stillborn", "Stillborn", "git init was run, but nothing was ever committed.")
        return out

    lw = v["last_words"].strip()
    if LAZY_WORDS.search(lw) or len(lw) < 6:
        add("mid_sentence", "Died mid-sentence", f'Its last words were barely words: "{lw}".')
    if v["modified"]:
        stat = f" ({v['dirty_stat']})" if v["dirty_stat"] else ""
        add("unsaved", "Died holding unsaved work", f"{len(v['modified'])} tracked file(s) changed but never committed{stat}.")
    if FINAL_WORDS.search(lw):
        add("final_bow", "Took a final bow", f'The last commit sounds like an ending: "{lw}".')
    recent = commits[:5]
    if len(recent) >= 3 and sum(bool(BUG_WORDS.search(c["subject"])) for c in recent) >= 3:
        add("fighting_bug", "Was fighting a bug", "At least 3 of its last 5 commits mention fixes or errors.")
    if v["lifespan_days"] <= 10 and v["commits"] >= 5:
        add("sprint", "Lived fast", f"{v['commits']} commits in {v['lifespan_days'] or 1} day(s), then silence.")
    if (todo_count or 0) >= 15 or (unchecked or 0) >= 5:
        add("scope", "Bigger dreams than days", f"{todo_count or 0} TODOs and {unchecked or 0} unchecked README boxes left behind.")
    if v["commits"] <= 3 and (SETUP_WORDS.search(v["first_words"]) or v["commits"] <= 2):
        add("setup", "Barely began", f"Only {v['commits']} commit(s), starting with \"{v['first_words']}\".")
    if len(commits) >= 12:
        def gaps(cs: list[dict]) -> list[float]:
            return [(cs[i]["when"] - cs[i + 1]["when"]).total_seconds() for i in range(len(cs) - 1)]
        late, early = sorted(gaps(commits[:6])), sorted(gaps(commits[-6:]))
        if early and late and late[len(late) // 2] > 3 * max(early[len(early) // 2], 3600):
            add("slow_fade", "Faded slowly", "The gaps between its final commits kept growing.")
    if v["stashes"]:
        add("stash", "Buried secrets", f"{len(v['stashes'])} stash(es) never popped.")
    if v["unmerged"]:
        add("branches", "Paths not taken", f"{len(v['unmerged'])} branch(es) never merged: {', '.join(v['unmerged'][:3])}.")
    if readme_chars is not None and readme_chars < 200:
        add("unexplained", "Never explained itself", "Its README is missing or nearly empty.")
    if len(v["authors"]) > 1:
        add("team", "Raised by a team", f"{len(v['authors'])} people committed to it: {', '.join(v['authors'][:4])}.")
    return out


CAUSE_BY_SIGNAL = [
    ("final_bow", "Mission accomplished, then forgotten"),
    ("fighting_bug", "Blocked by a bug"),
    ("sprint", "The hackathon ended"),
    ("unsaved", "Abandoned mid-thought"),
    ("scope", "Scope swallowed it"),
    ("setup", "Never got past setup"),
    ("successor", "Replaced by a newer idea"),
    ("slow_fade", "Slow fade"),
]


def cause_hint(sigs: list[dict]) -> str:
    keys = {s["key"] for s in sigs}
    if "stillborn" in keys:
        return "Stillborn"
    if "mid_sentence" in keys:                       # last words were "wip", "fix later", ...
        return "Abandoned mid-thought"
    if "sprint" in keys and keys & {"final_bow", "team"}:
        return "The hackathon ended"                 # a team sprint that ended on a finished-sounding commit
    for key, cause in CAUSE_BY_SIGNAL:
        if key in keys:
            return cause
    return "Quietly forgotten"


def activity(v: dict, commits: list[dict], n_max: int = 60) -> dict:
    """Commit heartbeat between birth and death, for the EKG."""
    if not commits:
        return {"buckets": [], "lifespan_days": 0, "silence_days": 0}
    born = datetime.fromisoformat(v["born"])
    died = datetime.fromisoformat(v["died"])
    span = (died - born).total_seconds()
    n = int(min(n_max, max(12, v["lifespan_days"] + 1)))
    buckets = [0] * n
    for c in commits:
        idx = 0 if span <= 0 else int((c["when"] - born).total_seconds() / span * (n - 1))
        buckets[min(n - 1, max(0, idx))] += 1
    return {"buckets": buckets, "lifespan_days": v["lifespan_days"], "silence_days": v["days_silent"]}


def tombstone(repo: str, now: datetime | None = None) -> dict:
    """Light vitals for the graveyard view (no file scanning)."""
    now = now or datetime.now(timezone.utc)
    v, commits = _read_vitals(repo, now)
    sigs = signals(v, commits)
    v["cause_hint"] = cause_hint(sigs)
    v["omens"] = [s["label"] for s in sigs]
    return v


def find_successor(tomb: dict, graveyard: list[dict]) -> dict | None:
    """The project born right after this one went quiet. Probably why it died."""
    if not tomb.get("died"):
        return None
    died = datetime.fromisoformat(tomb["died"])
    born_self = datetime.fromisoformat(tomb["born"])
    best = None
    for other in graveyard:
        if other["path"] == tomb["path"] or not other.get("born"):
            continue
        ob = datetime.fromisoformat(other["born"])
        gap = (ob - died).total_seconds() / 86400
        if -1 <= gap <= 14 and ob > born_self and (best is None or gap < best[0]):
            best = (gap, other)
    if not best:
        return None
    return {"name": best[1]["name"], "path": best[1]["path"], "born": best[1]["born"],
            "days_after": max(0, round(best[0]))}


# ---------------------------------------------------------------- full excavation

def _tracked_text_files(repo: str, extra: list[str]) -> list[str]:
    raw = git(repo, "ls-files", "-z")
    files = [f for f in raw.split("\0") if f] + extra
    keep, seen = [], set()
    for f in files:
        f = f.replace("\\", "/")
        base = f.rsplit("/", 1)[-1]
        ext = os.path.splitext(base)[1].lower()
        if f in seen or base in LOCKFILES or SECRET_FILE.search(f) or base.endswith(".min.js"):
            continue
        if ext not in TEXT_EXTS and base not in {"Dockerfile", "Makefile"}:
            continue
        if any(part in SKIP_DIRS for part in f.split("/")[:-1]):
            continue
        seen.add(f)
        keep.append(f)
    # key files and unsaved files first, then shallow before deep
    extra_set = set(extra)
    keep.sort(key=lambda f: (f not in extra_set, not KEY_FILE.search(f), f.count("/"), f.lower()))
    return keep[:500]


def _languages(repo: str) -> list[str]:
    exts = Counter(os.path.splitext(f)[1].lower() for f in git(repo, "ls-files", "-z").split("\0") if f)
    langs = [(LANG_NAMES[e], n) for e, n in exts.most_common() if e in LANG_NAMES]
    return [f"{name} ({n})" for name, n in langs[:4]]


def excavate(repo: str, now: datetime | None = None) -> dict:
    """Dig up everything: vitals, omens, heartbeat, and numbered evidence."""
    now = now or datetime.now(timezone.utc)
    v, commits = _read_vitals(repo, now)
    evidence: dict[str, dict] = {}
    redactions = 0

    def put(eid: str, kind: str, label: str, text: str, match: str | None = None) -> None:
        nonlocal redactions
        text, n = redact(text)
        label, m = redact(label)
        redactions += n + m
        evidence[eid] = {"id": eid, "kind": kind, "label": label, "text": text}
        if match is not None:  # what a claim must overlap with to count as supported (no author names)
            evidence[eid]["match"] = redact(match)[0]

    # the vital record: citable as [V1]
    if commits:
        born, died = datetime.fromisoformat(v["born"]), datetime.fromisoformat(v["died"])
        put("V1", "vitals", "vital record",
            f"Born {born:%B %d, %Y} with the first commit \"{v['first_words']}\". Last commit {died:%B %d, %Y at %H:%M}. "
            f"Lived {plural(max(v['lifespan_days'], 1), 'day')}, {plural(v['commits'], 'commit')}. "
            f"Silent for {plural(v['days_silent'], 'day')} since. Made by {', '.join(v['authors']) or 'unknown'}.")

    # commits, newest first: C1 is the last thing it ever said
    for i, c in enumerate(commits[:300], 1):
        put(f"C{i}", "commit", f"{c['short']} · {c['when']:%Y-%m-%d %H:%M}",
            f"{c['when']:%Y-%m-%d %H:%M} {c['author']}: {c['subject']}",
            match=f"{c['when']:%Y-%m-%d %H:%M %B %A} {c['subject']}")
    first_id = f"C{min(len(commits), 300)}" if commits else None
    if commits and len(commits) > 300:  # make sure the birth is always citable
        b = min(commits, key=lambda c: c["when"])
        put("C0", "commit", f"{b['short']} · {b['when']:%Y-%m-%d %H:%M}",
            f"{b['when']:%Y-%m-%d %H:%M} {b['author']}: {b['subject']} (first commit)")
        first_id = "C0"

    # README
    readme_text = ""
    if v["readme"]:
        try:
            with open(os.path.join(repo, v["readme"]), encoding="utf-8", errors="replace") as fh:
                readme_text = fh.read(20000)
        except OSError:
            readme_text = ""
    unchecked = [m.group(1).strip() for m in map(CHECKBOX_RE.match, readme_text.splitlines()) if m]
    paras = [p.strip() for p in re.split(r"\n\s*\n", readme_text) if p.strip()]
    paras = [p for p in paras if "shields.io" not in p and not p.startswith("![") and not p.startswith("<img")]
    for i, p in enumerate(paras[:12], 1):
        put(f"R{i}", "readme", f"{v['readme']} ¶{i}", _trunc(p, 420))

    # files: TODOs + chunks for retrieval
    todos: list[tuple[str, str]] = []
    for item in unchecked:
        todos.append((f"{v['readme']} (unchecked box)", f"[ ] {item}"))
    chunks: list[tuple[str, str, str]] = []
    unsaved = [p for p in v["modified"] + v["untracked"] if not p.endswith("/")]
    for rel in _tracked_text_files(repo, unsaved):
        full = os.path.join(repo, rel)
        try:
            if os.path.getsize(full) > 150_000:
                continue
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        for ln, line in enumerate(lines, 1):
            m = TODO_RE.search(line)
            if m and len(todos) < 80:
                todos.append((f"{rel}:{ln}", f"{m.group(1)}: {m.group(2).strip().rstrip('*/->').strip()}"))
        per_file = 6 if KEY_FILE.search(rel) or rel in unsaved else 3
        for start in range(0, min(len(lines), 40 * per_file), 40):
            if len(chunks) >= 150:
                break
            body = "\n".join(lines[start:start + 40]).strip()
            if body:
                chunks.append((rel, f"{rel} (lines {start + 1}-{min(len(lines), start + 40)})", body[:1500]))
    tracked = [f for f in git(repo, "ls-files", "-z").split("\0")
               if f and not any(p in SKIP_DIRS for p in f.split("/")[:-1])]
    if tracked:
        tracked.sort(key=lambda f: (f.count("/"), f.lower()))
        more = f" (and {len(tracked) - 30} more)" if len(tracked) > 30 else ""
        put("L1", "layout", "files in the repo", "My files: " + ", ".join(tracked[:30]) + more)
    for i, (where, what) in enumerate(todos[:60], 1):
        put(f"T{i}", "todo", where, f"{where} — {_trunc(what, 160)}")
    for i, (rel, label, body) in enumerate(chunks, 1):
        put(f"F{i}", "file", label, f"{label}:\n{body}")

    # the moment of death
    if v["modified"] or v["untracked"]:
        bits = []
        if v["modified"]:
            bits.append("changed but never committed: " + ", ".join(v["modified"][:8]))
        if v["untracked"]:
            bits.append("created but never added: " + ", ".join(v["untracked"][:8]))
        if v["dirty_stat"]:
            bits.append(f"({v['dirty_stat']})")
        put("U1", "unsaved", "uncommitted at time of death", "; ".join(bits))
    for i, s in enumerate(v["stashes"], 1):
        put(f"S{i}", "stash", f"stash {i}", f"Stash never popped: {s}")
    if v["unmerged"]:
        put("B1", "branch", "unmerged branches", "Branches never merged: " + ", ".join(v["unmerged"]))

    sigs = signals(v, commits, todo_count=len(todos), unchecked=len(unchecked), readme_chars=len(readme_text))
    return {
        "vitals": v,
        "languages": _languages(repo) if commits else [],
        "signals": sigs,
        "cause_hint": cause_hint(sigs),
        "activity": activity(v, commits),
        "evidence": evidence,
        "first_id": first_id,
        "todo_count": len(todos),
        "redactions": redactions,
        "successor": None,
    }


def attach_successor(d: dict, succ: dict) -> None:
    d["successor"] = succ
    d["evidence"]["X1"] = {
        "id": "X1", "kind": "successor", "label": f"successor: {succ['name']}",
        "text": f"A new project, {succ['name']}, had its first commit {succ['days_after']} day(s) after this one's last commit.",
    }
    d["signals"].append({"key": "successor", "label": f"Survived by {succ['name']}",
                         "detail": f"{succ['name']} was born {succ['days_after']} day(s) after the last commit."})
    d["cause_hint"] = cause_hint(d["signals"])


def dossier_text(d: dict) -> str:
    """The compact case file the model reads. Stays identical across turns (prompt cache)."""
    v = d["vitals"]
    ev = d["evidence"]
    if v["status"] == "stillborn":
        head = [f"PROJECT: {v['name']}", "STATUS: stillborn. git init was run but nothing was ever committed."]
    else:
        born = datetime.fromisoformat(v["born"])
        died = datetime.fromisoformat(v["died"])
        head = [
            f"PROJECT: {v['name']}",
            f"BORN: {born:%B %d, %Y} — first words [{d['first_id']}]: \"{v['first_words']}\"",
            f"DIED: {died:%B %d, %Y at %H:%M} — silent for {v['days_silent']} days since",
            f"LIFESPAN: {plural(v['lifespan_days'], 'day')}, {plural(v['commits'], 'commit')}, made by: {', '.join(v['authors']) or 'unknown'}",
            f"LAST WORDS (final commit) [C1]: \"{v['last_words']}\"",
        ]
        left = []
        if "U1" in ev:
            left.append(f"{plural(len(v['modified']) + len(v['untracked']), 'uncommitted file')} [U1]")
        if d["todo_count"]:
            left.append(f"{plural(d['todo_count'], 'unfinished TODO')} [T1]")
        if "X1" in ev:
            left.append(f"replaced by {d['successor']['name']} [X1]")
        if left:
            head.append("LEFT BEHIND: " + "; ".join(left))
    if d["languages"]:
        head.append("WRITTEN IN: " + ", ".join(d["languages"]))
    if d["signals"]:
        head.append("OMENS: " + " | ".join(f"{s['label']}: {s['detail']}" for s in d["signals"]))
    head.append(f"SECRETS HIDDEN FROM YOU: {d['redactions']}")
    head.append("")
    head.append("EVIDENCE (cite these IDs):")
    for eid in dossier_ids(d):
        head.append(f"[{eid}] {_trunc(ev[eid]['text'], 260)}")
    return "\n".join(head)


def dossier_ids(d: dict) -> list[str]:
    """Evidence that always sits in the case file; retrieval surfaces the rest."""
    picks = ["V1"] + [f"C{i}" for i in range(1, 21)]
    if d["first_id"] and d["first_id"] not in picks:
        picks.append(d["first_id"])
    picks += ["U1", "X1", "L1"]
    picks += [f"T{i}" for i in range(1, 13)] + [f"R{i}" for i in range(1, 5)]
    picks += ["S1", "S2", "B1"]
    return [eid for eid in picks if eid in d["evidence"]]


def public_evidence(d: dict) -> dict:
    """What the browser gets: ids, labels and short previews for hover cards."""
    return {eid: {"kind": e["kind"], "label": e["label"], "preview": e["text"][:600],
                  "match": (e.get("match") or e["text"])[:3000]}
            for eid, e in d["evidence"].items()}
