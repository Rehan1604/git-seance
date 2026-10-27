"""Public demo mode and the hosted-model backend, tested without network or keys."""
import importlib
import json
import os
import subprocess
from contextlib import contextmanager

import pytest

from seance import public, spirit


# ---------------------------------------------------------------- input and sandboxing

@pytest.mark.parametrize("text, expected", [
    ("https://github.com/Rehan1604/amazon-ml", ("Rehan1604", "amazon-ml")),
    ("github.com/Rehan1604/amazon-ml.git", ("Rehan1604", "amazon-ml")),
    ("Rehan1604/amazon-ml", ("Rehan1604", "amazon-ml")),
    ("https://github.com/torvalds/linux/tree/master/kernel?x=1", ("torvalds", "linux")),
])
def test_parse_repo_accepts_github_forms(text, expected):
    assert public.parse_repo(text) == expected


@pytest.mark.parametrize("text", ["", "amazon-ml", "gitlab.com/a/b", "github.com/../etc", "github.com/a b/c",
                                  "https://github.com/-bad/x"])
def test_parse_repo_rejects_everything_else(text):
    with pytest.raises(public.PublicError):
        public.parse_repo(text)


def test_paths_outside_the_clone_root_are_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(public, "CLONE_ROOT", tmp_path / "clones")
    (tmp_path / "clones" / "a__b").mkdir(parents=True)
    assert public.inside_clone_root(str(tmp_path / "clones" / "a__b"))
    assert not public.inside_clone_root(str(tmp_path))
    assert not public.inside_clone_root(str(tmp_path / "clones" / ".." / "elsewhere"))
    assert not public.inside_clone_root("/etc")


def test_limiter_per_visitor_and_daily_cap(monkeypatch):
    monkeypatch.setenv("SEANCE_CALLS_PER_HOUR", "3")
    monkeypatch.setenv("SEANCE_DAILY_CAP", "4")
    lim = public.Limiter()
    lim.hit("1.1.1.1", "call", 2)
    lim.hit("1.1.1.1", "call")
    with pytest.raises(public.PublicError):
        lim.hit("1.1.1.1", "call")          # visitor over 3 per hour
    lim.hit("2.2.2.2", "call")              # global total now 4
    with pytest.raises(public.PublicError, match="enough for today"):
        lim.hit("3.3.3.3", "call")


def test_successor_is_the_owners_next_repo(monkeypatch):
    monkeypatch.setattr(public, "owner_repos", lambda owner: [
        {"name": "plain-words", "created_at": "2026-10-02T17:35:00Z"},
        {"name": "amazon-ml", "created_at": "2026-09-25T07:00:00Z"},
        {"name": "much-later", "created_at": "2026-12-01T00:00:00Z"},
    ])
    s = public.successor({"owner": "Rehan1604", "repo": "amazon-ml"}, "2026-09-27T16:28:00+05:30")
    assert s["name"] == "plain-words" and s["days_after"] == 5


# ---------------------------------------------------------------- hosted backend protocol

class FakeResponse:
    def __init__(self, status, payload=None, lines=()):
        self.status_code, self._payload, self._lines = status, payload, lines
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self):
        return self._payload

    def iter_lines(self):
        yield from self._lines

    def read(self):
        return self.text.encode()


@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setattr(spirit, "BACKEND", "openai")
    monkeypatch.setattr(spirit, "LLM_KEY", "test-key")
    monkeypatch.setattr(spirit, "LLM_BASE", "https://llm.example/v1")
    monkeypatch.setattr(spirit, "MODEL", "gemma-3-27b-it")
    monkeypatch.setattr(spirit, "_resolved", {"model": "gemma-3-27b-it", "at": 9e12})
    sent = []
    return sent


def test_hosted_model_is_discovered_when_the_configured_one_is_gone(monkeypatch):
    """Google renamed Gemma on the live demo; the server should find what the key can actually use."""
    monkeypatch.setattr(spirit, "BACKEND", "openai")
    monkeypatch.setattr(spirit, "LLM_KEY", "k")
    monkeypatch.setattr(spirit, "MODEL", "gemma-3-27b-it")
    monkeypatch.setattr(spirit, "_resolved", {})
    monkeypatch.setattr(spirit, "_hosted_models", [])
    listing = {"data": [{"id": "models/gemini-2.5-flash"}, {"id": "models/gemma-4-12b-it"},
                        {"id": "models/gemma-4-31b-it"}, {"id": "models/embeddinggemma-300m"}, {"id": "models/gemma-4-31b"}]}
    monkeypatch.setattr(spirit.httpx, "get", lambda *a, **k: FakeResponse(200, listing))
    assert spirit.hosted_model() == "gemma-4-31b-it"
    assert spirit.health()["models"][0] == "gemma-4-31b-it"
    monkeypatch.setattr(spirit, "_resolved", {})
    listing["data"].append({"id": "models/gemma-3-27b-it"})
    assert spirit.hosted_model() == "gemma-3-27b-it"          # the configured model wins when it exists


def test_hosted_chat_folds_the_system_prompt_into_the_first_turn(hosted, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        hosted.append((url, json, headers))
        return FakeResponse(200, {"choices": [{"message": {"content": "You came back [C1]."}}]})
    monkeypatch.setattr(spirit.httpx, "post", fake_post)
    out = spirit.chat([{"role": "system", "content": "CASE FILE"}, {"role": "user", "content": "hello"}],
                      temperature=0.7, num_predict=50)
    url, body, headers = hosted[0]
    assert out == "You came back [C1]."
    assert url == "https://llm.example/v1/chat/completions" and headers["Authorization"] == "Bearer test-key"
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert body["messages"][0]["content"].startswith("CASE FILE") and body["messages"][0]["content"].endswith("hello")
    assert body["max_tokens"] == 50 and body["model"] == "gemma-3-27b-it"


def test_hosted_json_survives_markdown_fences_and_asks_for_keys(hosted, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        hosted.append(json)
        return FakeResponse(200, {"choices": [{"message": {"content": '```json\n{"verdict": "rest"}\n```'}}]})
    monkeypatch.setattr(spirit.httpx, "post", fake_post)
    out = spirit.chat_json([{"role": "system", "content": "S"}, {"role": "user", "content": "decide"}],
                           spirit.RESURRECT_SCHEMA)
    assert out == {"verdict": "rest"}
    assert '"verdict"' in hosted[0]["messages"][-1]["content"]
    assert "format" not in hosted[0]


def test_hosted_errors_are_readable(hosted, monkeypatch):
    monkeypatch.setattr(spirit.httpx, "post", lambda *a, **k: FakeResponse(429, {"error": "slow down"}))
    with pytest.raises(spirit.SpiritsSilent, match="Too many séances"):
        spirit.chat([{"role": "user", "content": "x"}])
    monkeypatch.setattr(spirit, "LLM_KEY", "")
    with pytest.raises(spirit.SpiritsSilent, match="no key"):
        spirit.ping()


def test_hosted_stream_reads_server_sent_events(hosted, monkeypatch):
    lines = ['data: {"choices":[{"delta":{"content":"You "}}]}', "",
             'data: {"choices":[{"delta":{"content":"came back [C1]."}}]}', ": keep-alive",
             'data: {"choices":[{"delta":{}}]}', "data: [DONE]"]

    @contextmanager
    def fake_stream(method, url, json=None, headers=None, timeout=None):
        assert json["stream"] is True
        yield FakeResponse(200, lines=lines)
    monkeypatch.setattr(spirit.httpx, "stream", fake_stream)
    assert "".join(spirit.stream_chat([{"role": "user", "content": "hi"}])) == "You came back [C1]."


# ---------------------------------------------------------------- the public server, end to end

def _git(repo, *args, date=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Ghost", "GIT_AUTHOR_EMAIL": "g@x.y",
           "GIT_COMMITTER_NAME": "Ghost", "GIT_COMMITTER_EMAIL": "g@x.y"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.fixture
def public_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SEANCE_PUBLIC", "1")
    monkeypatch.setenv("SEANCE_CLONE_ROOT", str(tmp_path / "clones"))
    monkeypatch.setenv("SEANCE_CACHE", str(tmp_path / "cache.json"))
    importlib.reload(public)
    from seance import server
    importlib.reload(server)

    def fake_clone(meta):
        dest = public.CLONE_ROOT / f"{meta['owner']}__{meta['repo']}".lower()
        dest.mkdir(parents=True, exist_ok=True)
        _git(dest, "init", "-q")
        (dest / "a.py").write_text("# TODO: wake up\n", encoding="utf-8")
        _git(dest, "add", ".")
        _git(dest, "commit", "-qm", "first and last", date="2026-08-01T10:00:00+00:00")
        (dest / ".git" / public.META).write_text(json.dumps(meta), encoding="utf-8")
        return str(dest)

    monkeypatch.setattr(public, "lookup", lambda o, r: {"owner": o, "repo": r, "full_name": f"{o}/{r}"})
    monkeypatch.setattr(public, "clone", fake_clone)
    monkeypatch.setattr(public, "owner_repos", lambda owner: [])
    from fastapi.testclient import TestClient
    yield TestClient(server.app), server
    monkeypatch.delenv("SEANCE_PUBLIC")
    importlib.reload(public)
    importlib.reload(server)


def test_public_mode_summons_and_sandboxes(public_app, tmp_path):
    client, server = public_app
    assert client.get("/api/health").json()["public"] is True
    r = client.post("/api/summon", json={"url": "https://github.com/someone/old-thing"})
    assert r.status_code == 200, r.text
    path = r.json()["path"]
    assert r.json()["name"] == "someone/old-thing"
    g = client.get("/api/graveyard", params={"root": "/"}).json()     # root is ignored in public mode
    assert [t["name"] for t in g["dead"]] == ["someone/old-thing"]
    repo = client.get("/api/repo", params={"path": path}).json()
    assert repo["vitals"]["name"] == "someone/old-thing" and repo["public"] is True
    assert client.get("/api/repo", params={"path": str(tmp_path)}).status_code == 404       # outside the clones
    assert client.post("/api/bury/write", json={"path": path, "markdown": "x"}).status_code == 403
    assert client.post("/api/summon", json={"url": "not a repo"}).status_code == 400


def test_local_mode_has_no_summon():
    from fastapi.testclient import TestClient
    from seance import server
    assert TestClient(server.app).post("/api/summon", json={"url": "a/b"}).status_code == 404
