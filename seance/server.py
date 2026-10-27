"""git séance web app.

Local mode (default): binds to 127.0.0.1 and walks the folders on your own disk.
Public mode (SEANCE_PUBLIC=1, the hosted demo): only public GitHub repos that visitors summon.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import excavate, public, spirit

STATIC = Path(__file__).resolve().parent.parent / "static"
DEFAULT_ROOT = os.getenv("SEANCE_ROOT", str(Path.home() / "Desktop"))
PUBLIC = public.PUBLIC

app = FastAPI(title="git séance", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
if PUBLIC:
    origins = [o.strip() for o in os.getenv("SEANCE_ALLOWED_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])

_graveyard: list[dict] = []            # last local scan, used to find successors
_dossiers: dict[str, tuple[str, dict]] = {}


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class SeanceReq(BaseModel):
    path: str
    message: str = Field(default="", max_length=600)
    history: list[Msg] = Field(default_factory=list, max_length=24)
    opening: bool = False


class RiteReq(BaseModel):
    path: str
    fresh: bool = False


class BuryWriteReq(BaseModel):
    path: str
    markdown: str = Field(max_length=20000)


class SummonReq(BaseModel):
    url: str = Field(max_length=300)


def _ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _limit(request: Request, kind: str, cost: int = 1) -> None:
    if PUBLIC:
        try:
            public.limiter.hit(_ip(request), kind, cost)
        except public.PublicError as e:
            raise HTTPException(e.status, str(e))


def _repo_path(path: str) -> str:
    p = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
    if PUBLIC and not public.inside_clone_root(p):
        raise HTTPException(404, "Summon a public repo first.")
    if not os.path.exists(os.path.join(p, ".git")):
        raise HTTPException(404, "There is no git repository buried at that path.")
    return p


def _successor(path: str) -> dict | None:
    if PUBLIC:
        tomb = excavate.tombstone(path)
        return public.successor(public.meta_of(path), tomb.get("died"))
    tomb = next((t for t in _graveyard if t["path"] == path), None)
    return excavate.find_successor(tomb, _graveyard) if tomb else None


def _dossier(path: str) -> tuple[dict, str]:
    head = excavate.head_state(path)
    hit = _dossiers.get(path)
    if hit and hit[0].startswith(head + "|") and PUBLIC:
        return hit[1], hit[0]              # public clones don't change under us; skip the GitHub lookup
    succ = _successor(path)
    state = f"{head}|{(succ or {}).get('path') or (succ or {}).get('name') or ''}"
    if hit and hit[0] == state:
        return hit[1], state
    d = excavate.excavate(path)
    if succ:
        excavate.attach_successor(d, succ)
    if PUBLIC:
        d["vitals"]["name"] = public.meta_of(path)["full_name"]
    _dossiers[path] = (state, d)
    return d, state


def _silent(e: Exception) -> HTTPException:
    return HTTPException(503, str(e))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {**spirit.health(), "public": PUBLIC, "default_root": "" if PUBLIC else DEFAULT_ROOT,
            "dead_after_days": excavate.DEAD_AFTER_DAYS}


def _tombs(repos: list[str]) -> list[dict]:
    now = datetime.now(timezone.utc)
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(lambda r: excavate.tombstone(r, now), repos))


@app.get("/api/graveyard")
def graveyard(root: str = Query(DEFAULT_ROOT)):
    global _graveyard
    t0 = time.perf_counter()
    if PUBLIC:
        tombs = _tombs(public.clones())
        for t in tombs:
            t["name"] = public.meta_of(t["path"])["full_name"]
            t["successor"] = None
        dead = sorted([t for t in tombs if t["status"] != "alive"], key=lambda t: t["died"] or "", reverse=True)
        return {"root": "", "dead": dead, "alive": [], "ms": round((time.perf_counter() - t0) * 1000)}
    root = os.path.abspath(os.path.expanduser(root.strip().strip('"')))
    if not os.path.isdir(root):
        raise HTTPException(404, f"No such folder: {root}")
    tombs = _tombs(excavate.find_repos(root))
    _graveyard = tombs
    for t in tombs:
        t["successor"] = excavate.find_successor(t, tombs) if t["status"] == "dead" else None
        if t["successor"] and t["cause_hint"] == "Quietly forgotten":
            t["cause_hint"] = "Replaced by a newer idea"
    dead = sorted([t for t in tombs if t["status"] != "alive"], key=lambda t: t["died"] or "", reverse=True)
    alive = sorted([t for t in tombs if t["status"] == "alive"], key=lambda t: t["days_silent"])
    return {"root": root, "dead": dead, "alive": alive, "ms": round((time.perf_counter() - t0) * 1000)}


@app.post("/api/summon")
def summon(req: SummonReq, request: Request):
    if not PUBLIC:
        raise HTTPException(404, "Summoning remote repos only exists in the public demo. Locally, walk your own folders.")
    _limit(request, "summon")
    try:
        owner, repo = public.parse_repo(req.url)
        meta = public.lookup(owner, repo)
        path = public.clone(meta)
    except public.PublicError as e:
        raise HTTPException(e.status, str(e))
    tomb = excavate.tombstone(path)
    if tomb["status"] == "alive":
        raise HTTPException(409, f"{meta['full_name']} is still breathing: its last commit was "
                                 f"{tomb['days_silent']} day{'s' if tomb['days_silent'] != 1 else ''} ago. "
                                 f"Séances only work on the dead ({excavate.DEAD_AFTER_DAYS}+ days of silence).")
    return {"path": path, "name": meta["full_name"], "status": tomb["status"]}


@app.get("/api/repo")
def repo(path: str):
    p = _repo_path(path)
    d, state = _dossier(p)
    return {
        "vitals": d["vitals"],
        "languages": d["languages"],
        "signals": d["signals"],
        "cause_hint": d["cause_hint"],
        "activity": d["activity"],
        "evidence": excavate.public_evidence(d),
        "todo_count": d["todo_count"],
        "redactions": d["redactions"],
        "successor": d["successor"],
        "dossier_chars": len(excavate.dossier_text(d)),
        "autopsy": spirit.cache_get(spirit.cache_key("autopsy", p, state)),
        "resurrection": spirit.cache_get(spirit.cache_key("resurrect", p, state)),
        "model": spirit.spoken_by(),
        "public": PUBLIC,
    }


@app.post("/api/autopsy")
def autopsy(req: RiteReq, request: Request):
    p = _repo_path(req.path)
    d, state = _dossier(p)
    key = spirit.cache_key("autopsy", p, state)
    if not req.fresh and (hit := spirit.cache_get(key)):
        return hit
    _limit(request, "call")
    try:
        spirit.ping()
        result = spirit.autopsy(d)
    except spirit.SpiritsSilent as e:
        raise _silent(e)
    spirit.cache_put(key, result)
    return result


@app.post("/api/resurrect")
def resurrect(req: RiteReq, request: Request):
    p = _repo_path(req.path)
    d, state = _dossier(p)
    key = spirit.cache_key("resurrect", p, state)
    if not req.fresh and (hit := spirit.cache_get(key)):
        return hit
    _limit(request, "call")
    try:
        spirit.ping()
        result = spirit.resurrect(d)
    except spirit.SpiritsSilent as e:
        raise _silent(e)
    spirit.cache_put(key, result)
    return result


@app.post("/api/bury")
def bury(req: RiteReq, request: Request):
    p = _repo_path(req.path)
    d, state = _dossier(p)
    key = spirit.cache_key("eulogy", p, state)
    autopsy_key = spirit.cache_key("autopsy", p, state)
    text = None if req.fresh else spirit.cache_get(key)
    autopsy_result = spirit.cache_get(autopsy_key)
    calls = (not autopsy_result) + (not text)
    if calls:
        _limit(request, "call", calls)
    try:
        if not autopsy_result:  # the obituary needs a cause of death and an epitaph
            spirit.ping()
            autopsy_result = spirit.autopsy(d)
            spirit.cache_put(autopsy_key, autopsy_result)
        if not text:
            spirit.ping()
            text = spirit.eulogy(d)
            spirit.cache_put(key, text)
    except spirit.SpiritsSilent as e:
        raise _silent(e)
    return {"markdown": spirit.obituary_markdown(d, text, autopsy_result),
            "honesty": spirit.verify_citations(text, d["evidence"]),
            "autopsy": autopsy_result}


@app.post("/api/bury/write")
def bury_write(req: BuryWriteReq):
    if PUBLIC:
        raise HTTPException(403, "The public séance room can't write into anyone's repo. Copy the obituary instead.")
    p = _repo_path(req.path)
    target = os.path.join(p, "OBITUARY.md")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(req.markdown)
    return {"written": target}


@app.post("/api/seance")
def seance(req: SeanceReq, request: Request):
    p = _repo_path(req.path)
    d, _ = _dossier(p)
    if not req.opening and not req.message.strip():
        raise HTTPException(400, "Ask the ghost something.")
    _limit(request, "call")
    try:
        spirit.ping()
    except spirit.SpiritsSilent as e:
        raise _silent(e)
    history = [m.model_dump() for m in req.history]
    return StreamingResponse(spirit.stream_seance(d, history, req.message, req.opening),
                             media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
