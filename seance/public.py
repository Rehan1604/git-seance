"""Public mode: the hosted demo haunts public GitHub repos only.

Nothing on anyone's disk is ever read. Visitors paste a public repo; it is cloned
(blobless, so history is cheap), excavated exactly like a local repo, and the
ghost speaks through a hosted open-weight model. Private graveyards stay local.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path

import httpx

PUBLIC = os.getenv("SEANCE_PUBLIC") == "1"
CLONE_ROOT = Path(os.getenv("SEANCE_CLONE_ROOT", "/tmp/seance-repos"))
MAX_REPO_KB = int(os.getenv("SEANCE_MAX_REPO_KB", "150000"))
MAX_CLONES = int(os.getenv("SEANCE_MAX_CLONES", "40"))
FRESH_FOR = 6 * 3600
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()

OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
META = "seance-meta.json"   # kept inside .git/, so it never shows up as an untracked file


class PublicError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- input

def parse_repo(text: str) -> tuple[str, str]:
    t = re.sub(r"^\s*(?:https?://)?(?:www\.)?github\.com/", "", text.strip(), flags=re.I)
    parts = [p for p in t.split("?")[0].split("#")[0].split("/") if p]
    if len(parts) < 2:
        raise PublicError("Paste a public GitHub repo, like github.com/owner/repo.")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if not OWNER_RE.fullmatch(owner) or not NAME_RE.fullmatch(repo) or repo in {".", ".."}:
        raise PublicError("That doesn't look like a GitHub repo. Try github.com/owner/repo.")
    return owner, repo


# ---------------------------------------------------------------- github

_owner_cache: dict[str, tuple[float, list]] = {}


def _gh(path: str) -> httpx.Response:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "git-seance"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return httpx.get(f"https://api.github.com{path}", headers=headers, timeout=10)


def lookup(owner: str, repo: str) -> dict:
    """Confirm the repo is public and small enough. Falls back to git ls-remote if the API is rate-limited."""
    try:
        r = _gh(f"/repos/{owner}/{repo}")
    except httpx.HTTPError:
        r = None
    if r is not None and r.status_code == 404:
        raise PublicError(f"There's no public repo at github.com/{owner}/{repo}. Private graveyards stay private: "
                          "run git séance on your own machine for those.", 404)
    if r is not None and r.status_code == 200:
        j = r.json()
        if j.get("private"):
            raise PublicError("That repo is private. Run git séance locally for private graveyards.", 403)
        if j.get("size", 0) > MAX_REPO_KB:
            raise PublicError(f"{j['full_name']} is too big for this séance room ({j['size'] // 1024} MB). "
                              "Try a smaller repo, or run git séance locally.", 413)
        return {"owner": j["owner"]["login"], "repo": j["name"], "full_name": j["full_name"], "checked": "api"}
    # API unavailable or rate-limited: a public repo still answers ls-remote without credentials.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        subprocess.run(["git", "ls-remote", "--heads", f"https://github.com/{owner}/{repo}.git"],
                       env=env, check=True, timeout=20, capture_output=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise PublicError(f"There's no public repo at github.com/{owner}/{repo}.", 404)
    return {"owner": owner, "repo": repo, "full_name": f"{owner}/{repo}", "checked": "ls-remote"}


def owner_repos(owner: str) -> list[dict]:
    hit = _owner_cache.get(owner.lower())
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        r = _gh(f"/users/{owner}/repos?sort=created&direction=desc&per_page=100&type=owner")
        repos = r.json() if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        repos = []
    repos = [{"name": x["name"], "created_at": x["created_at"]} for x in repos if isinstance(x, dict) and x.get("created_at")]
    _owner_cache[owner.lower()] = (time.time(), repos)
    return repos


def successor(meta: dict, died_iso: str | None) -> dict | None:
    """The owner's next public repo, created within 14 days of this one's last commit."""
    if not died_iso:
        return None
    died = datetime.fromisoformat(died_iso)
    best = None
    for x in owner_repos(meta["owner"]):
        if x["name"].lower() == meta["repo"].lower():
            continue
        born = datetime.fromisoformat(x["created_at"].replace("Z", "+00:00"))
        gap = (born - died).total_seconds() / 86400
        if -1 <= gap <= 14 and (best is None or gap < best[0]):
            best = (gap, x)
    if not best:
        return None
    return {"name": best[1]["name"], "path": None, "born": best[1]["created_at"], "days_after": max(0, round(best[0]))}


# ---------------------------------------------------------------- clones

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


def _evict() -> None:
    dirs = sorted((p for p in CLONE_ROOT.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for p in dirs[: max(0, len(dirs) - MAX_CLONES + 1)]:
        shutil.rmtree(p, ignore_errors=True)


def clone(meta: dict) -> str:
    dest = CLONE_ROOT / f"{meta['owner']}__{meta['repo']}".lower()
    with _locks[str(dest)]:
        if (dest / ".git").exists() and time.time() - (dest / ".git" / META).stat().st_mtime < FRESH_FOR:
            return str(dest)
        shutil.rmtree(dest, ignore_errors=True)
        CLONE_ROOT.mkdir(parents=True, exist_ok=True)
        _evict()
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
        cmd = ["git", "clone", "--quiet", "--filter=blob:none", "--single-branch", "--no-tags",
               f"https://github.com/{meta['full_name']}.git", str(dest)]
        # Without the API's size check (rate-limited), give big repos much less rope.
        timeout = 150 if meta.get("checked") == "api" else 45
        try:
            subprocess.run(cmd, env=env, check=True, timeout=timeout, capture_output=True)
        except subprocess.TimeoutExpired:
            shutil.rmtree(dest, ignore_errors=True)
            raise PublicError("That repo took too long to dig up. Try a smaller one.", 504)
        except subprocess.CalledProcessError:
            shutil.rmtree(dest, ignore_errors=True)
            raise PublicError("GitHub wouldn't hand over that repo. Is it public?", 502)
        (dest / ".git" / META).write_text(json.dumps(meta), encoding="utf-8")
    return str(dest)


def meta_of(path: str) -> dict:
    try:
        return json.loads((Path(path) / ".git" / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        name = Path(path).name
        owner, _, repo = name.partition("__")
        return {"owner": owner, "repo": repo, "full_name": f"{owner}/{repo}"}


def clones() -> list[str]:
    if not CLONE_ROOT.is_dir():
        return []
    return [str(p) for p in CLONE_ROOT.iterdir() if (p / ".git" / META).exists()]


def inside_clone_root(path: str) -> bool:
    try:
        Path(path).resolve().relative_to(CLONE_ROOT.resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------- abuse limits

class Limiter:
    """Small in-memory sliding windows: per visitor per hour, and one global daily cap on model calls."""

    def __init__(self) -> None:
        self.per_hour = {"summon": int(os.getenv("SEANCE_SUMMONS_PER_HOUR", "12")),
                         "call": int(os.getenv("SEANCE_CALLS_PER_HOUR", "40"))}
        self.daily_cap = int(os.getenv("SEANCE_DAILY_CAP", "800"))
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self.day: deque = deque()
        self.lock = threading.Lock()

    def hit(self, ip: str, kind: str, cost: int = 1) -> None:
        now = time.time()
        with self.lock:
            q = self.hits[(ip, kind)]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) + cost > self.per_hour[kind]:
                raise PublicError("You've held a lot of séances this hour. The spirits need rest; come back later.", 429)
            if kind == "call":
                while self.day and now - self.day[0] > 86400:
                    self.day.popleft()
                if len(self.day) + cost > self.daily_cap:
                    raise PublicError("This séance room has spoken to the dead enough for today. "
                                      "Run git séance locally, it has no limits there.", 429)
                self.day.extend([now] * cost)
            q.extend([now] * cost)


limiter = Limiter()
