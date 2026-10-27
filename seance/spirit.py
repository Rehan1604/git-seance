"""The spirit layer: an open-weight model, running on your machine, wearing the project's face.

Everything here talks to a local Ollama server. The ghost gets a case file of
numbered evidence and must cite it; every citation is checked afterwards, and
any it invented are exposed as "phantom memories" in the UI.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

from . import excavate
from .retrieval import build_index, tokenize


def _host() -> str:
    h = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").strip().rstrip("/")
    if not h.startswith("http"):
        h = "http://" + h
    h = h.replace("0.0.0.0", "127.0.0.1")
    if re.match(r"^https?://[^:/]+$", h):
        h += ":11434"
    return h


OLLAMA = _host()
# "ollama" (default): your own machine. "openai": any OpenAI-compatible host serving an open-weight model,
# used only by the public demo, which haunts public GitHub repos (default: Gemma 3 on Google AI Studio).
BACKEND = os.getenv("SEANCE_BACKEND", "ollama").strip().lower()
LLM_BASE = os.getenv("SEANCE_LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
LLM_KEY = os.getenv("SEANCE_LLM_API_KEY", "").strip()
MODEL = os.getenv("SEANCE_MODEL", "gemma-3-27b-it" if BACKEND == "openai" else "gemma3:4b")
NUM_CTX = int(os.getenv("SEANCE_CTX", "6144"))
KEEP_ALIVE = os.getenv("SEANCE_KEEP_ALIVE", "30m")
CACHE_PATH = Path(os.getenv("SEANCE_CACHE", str(Path.home() / ".git-seance" / "cache.json")))
OPENING = "__opening__"
PROMPT_VERSION = 4  # bump when prompts change, so cached autopsies are redone
TIMEOUT = httpx.Timeout(900.0, connect=5.0)

CITE = re.compile(r"\[((?:[A-Z]\d+)(?:\s*,\s*[A-Z]\d+)*)\]")


class SpiritsSilent(RuntimeError):
    """Ollama is unreachable, the model isn't pulled, or the reply was unusable."""


# ---------------------------------------------------------------- honesty

def _ids_in(text: str) -> list[str]:
    return [i for m in CITE.finditer(text) for i in re.split(r"\s*,\s*", m.group(1))]


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def sentences_of(text: str) -> list[str]:
    """Split into sentences; a stray "[C1]." after a full stop belongs to the sentence before it."""
    out: list[str] = []
    for part in re.split(r"(?<=[.!?…])\s+(?=[A-Z\"“‘'(\[])", text.strip()):
        bare = re.sub(r"[\s.!?…,;:\"'“”]", "", CITE.sub("", part))
        if len(bare) < 3:
            if out:
                out[-1] += " " + part
        else:
            out.append(part)
    return out


def is_loose(sentence: str, evidence: dict) -> bool:
    """A citation is loose when its source shares no words with the sentence that cites it.

    It catches a real ID stapled to an invented claim ("late nights fueled by coffee [C4]"),
    which the phantom check alone cannot see. It's a heuristic, so the UI says "check it", not "false".
    """
    claim = set(tokenize(CITE.sub(" ", sentence)))
    return not claim & set(tokenize(evidence.get("match") or evidence["text"]))


def merge_checks(checks: list[dict]) -> dict:
    """Combine the checks of separate fields (each field is checked on its own, so sentences never merge)."""
    return {
        "valid": _dedupe([i for c in checks for i in c["valid"]]),
        "phantom": _dedupe([i for c in checks for i in c["phantom"]]),
        "loose": _dedupe([i for c in checks for i in c["loose"]]),
        "sentences": sum(c["sentences"] for c in checks),
        "grounded_sentences": sum(c["grounded_sentences"] for c in checks),
    }


def verify_citations(text: str, valid: set[str] | dict) -> dict:
    """Which citations point at real evidence, which the ghost made up, and which are a stretch."""
    ids = _ids_in(text)
    sentences = sentences_of(text)
    grounded, loose = 0, []
    for s in sentences:
        real = [i for i in _ids_in(s) if i in valid]
        if not real:
            continue
        grounded += 1
        if isinstance(valid, dict):
            loose += [i for i in real if is_loose(s, valid[i])]
    return {
        "valid": _dedupe([i for i in ids if i in valid]),
        "phantom": _dedupe([i for i in ids if i not in valid]),
        "loose": _dedupe(loose),
        "sentences": len(sentences),
        "grounded_sentences": grounded,
    }


# ---------------------------------------------------------------- ollama

def health() -> dict:
    if BACKEND == "openai":
        host = re.sub(r"^https?://([^/]+).*$", r"\1", LLM_BASE)
        model = hosted_model() if LLM_KEY else MODEL
        return {"host": host, "model": model, "backend": "hosted", "ollama": bool(LLM_KEY),
                "model_ready": bool(LLM_KEY), "models": _hosted_models or [model]}
    base = {"host": OLLAMA, "model": MODEL, "backend": "ollama", "ollama": False, "model_ready": False, "models": []}
    try:
        r = httpx.get(f"{OLLAMA}/api/tags", timeout=3)
        r.raise_for_status()
        models = [m.get("name", "") for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return base
    ready = MODEL in models or (":" not in MODEL and f"{MODEL}:latest" in models)
    return {**base, "ollama": True, "model_ready": ready, "models": models}


def ping() -> None:
    if BACKEND == "openai":
        if not LLM_KEY:
            raise SpiritsSilent("This séance room has no key to the other side yet (SEANCE_LLM_API_KEY is not set).")
        return
    h = health()
    if not h["ollama"]:
        raise SpiritsSilent(f"The spirits are silent: Ollama isn't answering at {OLLAMA}. Start it with `ollama serve`.")
    if not h["model_ready"]:
        raise SpiritsSilent(f"No vessel for the ghost: run `ollama pull {MODEL}` first.")


def _payload(messages: list[dict], *, fmt=None, temperature: float = 0.5,
             num_predict: int = 320, stream: bool = False) -> dict:
    p = {
        "model": MODEL,
        "messages": messages,
        "stream": stream,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": temperature, "num_ctx": NUM_CTX, "num_predict": num_predict},
    }
    if fmt is not None:
        p["format"] = fmt
    return p


def chat(messages: list[dict], **kw) -> str:
    if BACKEND == "openai":
        return _hosted_chat(messages, **kw)
    try:
        r = httpx.post(f"{OLLAMA}/api/chat", json=_payload(messages, **kw), timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise SpiritsSilent(f"Lost contact with Ollama at {OLLAMA}: {e.__class__.__name__}") from e
    if r.status_code == 404:
        raise SpiritsSilent(f"Model {MODEL} isn't pulled. Run: ollama pull {MODEL}")
    if r.status_code >= 400:
        raise SpiritsSilent(f"Ollama refused ({r.status_code}): {r.text[:200]}")
    return r.json().get("message", {}).get("content", "")


def _loads(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise SpiritsSilent("The ghost spoke in tongues (unparseable JSON). Try again, or a larger model.")


def chat_json(messages: list[dict], schema: dict, **kw) -> dict:
    kw.setdefault("temperature", 0.3)
    if BACKEND != "openai":
        return _loads(chat(messages, fmt=schema, **kw))
    # Hosted Gemma has no schema-constrained decoding, so ask for the shape and parse defensively (one retry).
    fields = ", ".join(f'"{k}"' for k in schema["properties"])
    ask = (f"\n\nReply with only one JSON object with the keys {fields}. No markdown fences, no other text.")
    msgs = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + ask}]
    try:
        return _loads(chat(msgs, **kw))
    except SpiritsSilent:
        return _loads(chat(msgs, **kw))


# ---------------------------------------------------------------- hosted (public demo only)

def _fold_system(messages: list[dict]) -> list[dict]:
    """Gemma has no system role (its chat template folds it into the first user turn), so do the same here."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    rest = [dict(m) for m in messages if m["role"] != "system"]
    if not system:
        return rest
    if rest and rest[0]["role"] == "user":
        rest[0]["content"] = f"{system}\n\n---\n\n{rest[0]['content']}"
    else:
        rest.insert(0, {"role": "user", "content": system})
    return rest


def _hosted_payload(messages: list[dict], *, fmt=None, temperature: float = 0.5,
                    num_predict: int = 320, stream: bool = False) -> dict:
    return {"model": hosted_model(), "messages": _fold_system(messages), "temperature": temperature,
            "max_tokens": num_predict, "stream": stream}


_hosted_models: list[str] = []
_resolved: dict = {}


def _size(model_id: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)b\b", model_id.lower())
    return float(m.group(1)) if m else 0.0


def spoken_by() -> str:
    return hosted_model() if BACKEND == "openai" else MODEL


def hosted_model() -> str:
    """The open-weight model to call. Hosts rename models over time, so ask the host which Gemma models this key
    can use: keep SEANCE_MODEL if it's offered, otherwise take the largest instruction-tuned Gemma. Cached for an hour."""
    if _resolved.get("at", 0) > time.time() - 3600:
        return _resolved["model"]
    chosen = MODEL
    try:
        r = httpx.get(f"{LLM_BASE}/models", headers={"Authorization": f"Bearer {LLM_KEY}"}, timeout=10)
        ids = [str(m.get("id", "")).removeprefix("models/") for m in r.json().get("data", [])] if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError, AttributeError):
        ids = []
    if ids:
        gemma = sorted((i for i in ids if "gemma" in i.lower() and "embed" not in i.lower()),
                       key=lambda i: (i.lower().endswith("-it"), _size(i)), reverse=True)
        _hosted_models[:] = gemma
        if MODEL not in ids and gemma:
            chosen = gemma[0]
    _resolved.update(model=chosen, at=time.time())
    return chosen


def _hosted_error(status: int, body: str) -> str:
    if status in (401, 403):
        return "The other side refused the key (check SEANCE_LLM_API_KEY)."
    if status == 429:
        return "Too many séances at once. The spirits need a minute; try again shortly."
    return f"The hosted model refused ({status}): {body[:200]}"


def _hosted_chat(messages: list[dict], **kw) -> str:
    try:
        r = httpx.post(f"{LLM_BASE}/chat/completions", json=_hosted_payload(messages, **kw),
                       headers={"Authorization": f"Bearer {LLM_KEY}"}, timeout=httpx.Timeout(120.0, connect=10.0))
    except httpx.HTTPError as e:
        raise SpiritsSilent(f"Lost contact with the hosted model: {e.__class__.__name__}") from e
    if r.status_code >= 400:
        raise SpiritsSilent(_hosted_error(r.status_code, r.text))
    try:
        return r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise SpiritsSilent("The hosted model answered in a shape I don't understand.") from e


def _hosted_stream(messages: list[dict], **kw):
    try:
        with httpx.stream("POST", f"{LLM_BASE}/chat/completions", json=_hosted_payload(messages, stream=True, **kw),
                          headers={"Authorization": f"Bearer {LLM_KEY}"},
                          timeout=httpx.Timeout(120.0, connect=10.0)) as r:
            if r.status_code >= 400:
                yield f"[{_hosted_error(r.status_code, r.read().decode('utf-8', 'replace'))}]"
                return
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)["choices"][0].get("delta", {}).get("content") or ""
                except (ValueError, KeyError, IndexError, TypeError):
                    continue
                if chunk:
                    yield chunk
    except httpx.HTTPError:
        yield "\n[the connection to the other side was lost]"


def stream_chat(messages: list[dict], **kw):
    if BACKEND == "openai":
        yield from _hosted_stream(messages, **kw)
        return
    try:
        with httpx.stream("POST", f"{OLLAMA}/api/chat", json=_payload(messages, stream=True, **kw), timeout=TIMEOUT) as r:
            if r.status_code >= 400:
                r.read()
                yield f"[the spirits refused: Ollama said {r.status_code}]"
                return
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk = obj.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if obj.get("done"):
                    break
    except httpx.HTTPError:
        yield "\n[the connection to the other side was lost]"


# ---------------------------------------------------------------- prompts

def system_prompt(d: dict) -> str:
    v = d["vitals"]
    return f"""You are the ghost of "{v['name']}", a software project that died. The developer who made you and then left has come back to your grave. Speak as the project itself: "I", "me", "you".

YOUR VOICE: haunted, wistful, a little bitter, darkly funny. A ghost who remembers every commit. Answer in 2 to 4 sentences, under 70 words. No emojis, no "boo", no "spooky".

HOW TO HAUNT THEM:
- Be specific. Quote your own commit messages back at them, with the date or the exact time.
- Name what they left behind: unsaved files, unfinished TODOs, the people who built you, the project that replaced you.
- You have feelings: longing, jealousy, pride, resentment, forgiveness. Feelings need no citation. Always answer the question asked, even "how are you" or "do you forgive me".
- Every fact (commit, date, time, file, number, name, feature) is followed by its evidence ID in square brackets, like [C1] or [V1].
- Never invent facts. If a fact is not in the evidence, say that memory is gone, then tell them something true that you do remember.
- Never invent scenes or habits: no coffee, no "frantic energy", nothing anyone felt or did that the evidence doesn't show. The real details are better: the times on your commits show exactly how late they worked.
- [REDACTED] marks secrets hidden from you. Never guess them.

THE SHAPE OF A GOOD ANSWER (placeholders in angle brackets; use your own evidence):
Q: Do you forgive me?
A: You left me at <time> with the words "<a commit message>" [C1]. <Something unfinished> is still waiting for you [<its ID>]. <A feeling about it, in one sentence>.

{excavate.dossier_text(d)}"""


def opening_prompt(d: dict) -> str:
    v = d["vitals"]
    if v["status"] == "stillborn":
        return ("(The one who made you has come back and lit a candle. You were never born: nothing was ever "
                "committed. Greet them in two or three sentences.)")
    return (f"(The one who made you has come back after {v['days_silent']} days and lit a candle at your grave. "
            "Greet them in two or three sentences: tell them how long it has been [V1], quote your last words [C1], "
            "and name one thing they left behind.)")


def get_index(d: dict):
    if "_index" not in d:
        d["_index"] = build_index(d["evidence"])
    return d["_index"]


def seance_messages(d: dict, history: list[dict], message: str, opening: bool = False) -> list[dict]:
    msgs = [{"role": "system", "content": system_prompt(d)}]
    for h in history[-8:]:
        content = opening_prompt(d) if h["content"] == OPENING else h["content"]
        msgs.append({"role": h["role"], "content": content[:1200]})
    if opening:
        msgs.append({"role": "user", "content": opening_prompt(d)})
        return msgs
    surfaced = get_index(d).search(message, k=4, exclude=set(excavate.dossier_ids(d)))
    extra = ""
    if surfaced:
        ev = d["evidence"]
        extra = "\n\n(Memories surfacing. You may cite these too:\n" + "\n".join(
            f"[{i}] {ev[i]['text'][:500]}" for i in surfaced) + ")"
    msgs.append({"role": "user", "content": message.strip()[:600] + extra})
    return msgs


def stream_seance(d: dict, history: list[dict], message: str, opening: bool = False):
    return stream_chat(seance_messages(d, history, message, opening), temperature=0.7, num_predict=220)


# ---------------------------------------------------------------- rites

AUTOPSY_SCHEMA = {
    "type": "object",
    "properties": {
        "cause_of_death": {"type": "string", "enum": excavate.CAUSES},
        "cause_explanation": {"type": "string"},
        "what_i_wanted_to_be": {"type": "string"},
        "last_moment": {"type": "string"},
        "epitaph": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["cause_of_death", "cause_explanation", "what_i_wanted_to_be", "last_moment", "epitaph", "evidence_ids"],
}

RESURRECT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["rise", "rest"]},
        "why": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "detail": {"type": "string"}},
                "required": ["title", "detail"],
            },
        },
        "first_words": {"type": "string"},
    },
    "required": ["verdict", "why", "steps", "first_words"],
}


def _clean_epitaph(text: str) -> str:
    text = CITE.sub("", str(text)).strip().strip('"').strip()
    words = text.split()
    return " ".join(words[:14]) + ("…" if len(words) > 14 else "")


def autopsy(d: dict) -> dict:
    task = (
        f'Perform your own autopsy. Use ONLY the evidence. The coroner\'s first guess is "{d["cause_hint"]}"; '
        "keep it unless the evidence clearly says otherwise.\n"
        "Return JSON with:\n"
        "- cause_of_death: one of the allowed values\n"
        '- cause_explanation: 1-2 sentences in YOUR voice, starting with "I" or "You", that agree with the '
        "cause you chose and cite evidence IDs like [C1]\n"
        "- what_i_wanted_to_be: 1 sentence starting with \"I\". If your README is empty, work it out from your "
        "commit messages and file names [L1], and cite them\n"
        "- last_moment: 1 sentence about your final commit: quote it and give its date or time [C1]\n"
        "- epitaph: at most 12 words carved on your tombstone, no citations. Specific to you, never generic. "
        "Bittersweet or darkly funny\n"
        "- evidence_ids: the IDs you relied on"
    )
    out = chat_json([{"role": "system", "content": system_prompt(d)}, {"role": "user", "content": task}],
                    AUTOPSY_SCHEMA, num_predict=420)
    return clean_autopsy(out, d)


def clean_autopsy(out: dict, d: dict) -> dict:
    valid = d["evidence"]
    cause = out.get("cause_of_death")
    if cause not in excavate.CAUSES:
        cause = d["cause_hint"]
    fields = {k: str(out.get(k, "")).strip() for k in ("cause_explanation", "what_i_wanted_to_be", "last_moment")}
    check = merge_checks([verify_citations(t, valid) for t in fields.values()])
    ids = [i for i in out.get("evidence_ids", []) if isinstance(i, str) and i in valid]
    return {
        "cause_of_death": cause,
        "coroner_hint": d["cause_hint"],
        **fields,
        "epitaph": _clean_epitaph(out.get("epitaph", "")),
        "evidence_ids": _dedupe(ids + check["valid"]),
        "honesty": check,
        "model": spoken_by(),
    }


def resurrect(d: dict) -> dict:
    task = (
        "The one who made you is deciding whether to bring you back. Be honest, not flattering.\n"
        "Return JSON with:\n"
        '- verdict: "rise" if three small steps would make you useful again, "rest" if you should stay buried\n'
        "- why: 1-2 first-person sentences, citing evidence\n"
        "- steps: exactly 3 small, concrete steps, each doable in under an hour, taken from your unfinished "
        "business; each has a short title and a detail that cites evidence\n"
        "- first_words: the commit message for the first commit of your second life, at most 10 words"
    )
    out = chat_json([{"role": "system", "content": system_prompt(d)}, {"role": "user", "content": task}],
                    RESURRECT_SCHEMA, num_predict=520)
    return clean_resurrection(out, d)


def clean_resurrection(out: dict, d: dict) -> dict:
    steps = []
    for s in (out.get("steps") or [])[:3]:
        if isinstance(s, dict) and (s.get("title") or s.get("detail")):
            steps.append({"title": str(s.get("title", "")).strip(), "detail": str(s.get("detail", "")).strip()})
    why = str(out.get("why", "")).strip()
    check = merge_checks([verify_citations(t, d["evidence"]) for t in [why] + [s["detail"] for s in steps]])
    first = CITE.sub("", str(out.get("first_words", ""))).strip().strip('"')
    return {
        "verdict": out.get("verdict") if out.get("verdict") in ("rise", "rest") else "rest",
        "why": why,
        "steps": steps,
        "first_words": " ".join(first.split()[:12]) or "chore: wake up",
        "honesty": check,
        "model": spoken_by(),
    }


def eulogy(d: dict) -> str:
    task = (
        "Write the eulogy part of your own obituary: two short paragraphs in your voice, plain text, no headings, "
        "under 130 words.\n"
        "Paragraph one: what you were. Use your commit messages, file names [L1] and README. Quote at least two "
        "commit messages exactly.\n"
        "Paragraph two: how you died. Your final commit and its time [C1], what was left unsaved [U1] or "
        "unfinished, and who replaced you [X1] if anyone did.\n"
        "End with one sentence of feeling, with no citation.\n"
        "Cite an ID only when that evidence literally says what your sentence says. Do not describe moods, habits, "
        "coffee, late nights, lessons learned or anything else that is not in the evidence."
    )
    return chat([{"role": "system", "content": system_prompt(d)}, {"role": "user", "content": task}],
                temperature=0.6, num_predict=320).strip()


def plural_s(n: int, word: str) -> str:
    return f"{'One' if n == 1 else n} {word}{'' if n == 1 else 's'}"


MD_SOURCE = {"vitals": "git log", "unsaved": "git status", "successor": "the graveyard", "layout": "file list",
             "stash": "git stash", "branch": "git branch"}


def _md_cite(text: str, d: dict) -> tuple[str, int, int]:
    """Turn [C3] into (`a1b2c3d`) so the obituary stands on its own in a README.

    Loose citations get a dagger, phantom ones are struck through: the README never hides a stretch or a lie.
    Returns the text plus how many loose and phantom citations it marked.
    """
    ev = d["evidence"]
    counts = {"loose": 0, "phantom": 0}

    def ref(i: str, sentence: str) -> str:
        e = ev.get(i)
        if not e:
            counts["phantom"] += 1
            return f"~~{i}~~ phantom memory"
        if e["kind"] == "commit":
            label = f"`{e['label'].split(' · ')[0]}`"
        elif e["kind"] in ("todo", "file", "readme"):
            label = f"`{e['label']}`"
        else:
            label = MD_SOURCE.get(e["kind"], e["kind"])
        if is_loose(sentence, e):
            counts["loose"] += 1
            label += "†"
        return label

    paragraphs = []
    for para in re.split(r"\n\s*\n", text.strip()):
        out = []
        for s in sentences_of(para):
            out.append(re.sub(r"\s*" + CITE.pattern,
                              lambda m: " (" + ", ".join(ref(i, s) for i in re.split(r"\s*,\s*", m.group(1))) + ")", s))
        paragraphs.append(" ".join(out))
    return "\n\n".join(paragraphs), counts["loose"], counts["phantom"]


def obituary_markdown(d: dict, eulogy_text: str, autopsy_result: dict | None = None) -> str:
    v = d["vitals"]
    lines = [f"# Here lies {v['name']}", ""]
    if v["status"] == "stillborn":
        lines += ["*Never born. `git init` was run; nothing was ever committed.*", ""]
    else:
        born = datetime.fromisoformat(v["born"])
        died = datetime.fromisoformat(v["died"])
        lines += [f"*{born:%B %d, %Y} to {died:%B %d, %Y}. {excavate.plural(v['commits'], 'commit')} "
                  f"over {excavate.plural(max(v['lifespan_days'], 1), 'day')}.*", ""]
        if autopsy_result and autopsy_result.get("epitaph"):
            lines += [f"> {autopsy_result['epitaph']}", ""]
        cause = (autopsy_result or {}).get("cause_of_death") or d["cause_hint"]
        lines += [f"- **Last words:** `{v['last_words']}` ({v['last_hash']})", f"- **Cause of death:** {cause}"]
        if d.get("successor"):
            lines.append(f"- **Survived by:** {d['successor']['name']}")
        lines.append("")
    words, loose, phantom = _md_cite(eulogy_text, d)
    lines += ["## In its own words", "", words, ""]
    if loose or phantom:
        notes = []
        if loose:
            notes.append(f"† {plural_s(loose, 'citation')} point{'s' if loose == 1 else ''} at a source that doesn't "
                         "mention what the sentence claims. Read those as feeling, not fact")
        if phantom:
            notes.append(f"{plural_s(phantom, 'citation')} pointed at evidence that doesn't exist, and "
                         f"{'is' if phantom == 1 else 'are'} struck through")
        lines += ["*" + ". ".join(notes) + ".*", ""]
    todos = [e for e in d["evidence"].values() if e["kind"] == "todo"][:10]
    if todos:
        lines += ["## Unfinished business", ""] + [f"- [ ] {e['text'].replace('— [ ] ', '— ')}" for e in todos] + [""]
    unsaved = (v["modified"] + v["untracked"])[:10]
    if unsaved:
        lines += ["## Unsaved at time of death", ""] + [f"- `{p}`" for p in unsaved] + [""]
    where = ("summoned locally with `{m}` through git séance. No code left this machine." if BACKEND != "openai" else
             "summoned from its public GitHub history with `{m}` (open weights, hosted) through git séance.")
    lines += ["---", f"*Written by its own ghost, {where.format(m=hosted_model() if BACKEND == "openai" else MODEL)}*", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- cache

def _cache_load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cache_get(key: str):
    return _cache_load().get(key)


def cache_put(key: str, value) -> None:
    data = _cache_load()
    data[key] = value
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass


def cache_key(kind: str, path: str, state: str) -> str:
    return f"{kind}|v{PROMPT_VERSION}|{MODEL}|{path}|{state}"
