"""Tests run without Ollama: they cover everything the ghost is allowed to know, and how its claims are checked."""
import os
import subprocess
from datetime import datetime, timezone

import pytest

from seance import excavate, retrieval, spirit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def git(repo, *args, date=None):
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": "Ghost Tester", "GIT_AUTHOR_EMAIL": "ghost@example.com",
        "GIT_COMMITTER_NAME": "Ghost Tester", "GIT_COMMITTER_EMAIL": "ghost@example.com",
    })
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.fixture
def graveyard(tmp_path):
    """Two dead projects: haunted-app (died mid-thought) and its successor, new-shiny."""
    repo = tmp_path / "haunted-app"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text(
        "# Haunted App\n\nA tool that summons voices from old notes.\n\n- [ ] add voice input\n- [x] parse notes\n",
        encoding="utf-8")
    (repo / "app.py").write_text(
        'API_KEY = "sk-abcdefghijklmnopqrstuvwx"\n# TODO: handle the empty notebook case\nprint("hello")\n',
        encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial commit", date="2026-09-01T10:00:00+05:30")
    for i in range(6):
        (repo / f"part{i}.py").write_text(f"value = {i}\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", f"add part {i}", date=f"2026-09-0{2 + i // 3}T1{i}:00:00+05:30")
    with open(repo / "app.py", "a", encoding="utf-8") as fh:
        fh.write("# FIXME: voice crashes on long notes\n")
    git(repo, "commit", "-qam", "wip fix later", date="2026-09-04T03:12:00+05:30")
    (repo / "part0.py").write_text("value = 'half done'\n", encoding="utf-8")       # modified, never committed
    (repo / "notes.txt").write_text("idea: make it talk back\n", encoding="utf-8")   # untracked
    (repo / "node_modules" / "junk").mkdir(parents=True)

    succ = tmp_path / "new-shiny"
    succ.mkdir()
    git(succ, "init", "-q")
    (succ / "main.py").write_text("print('shiny')\n", encoding="utf-8")
    git(succ, "add", ".")
    git(succ, "commit", "-q", "-m", "first commit", date="2026-09-06T09:00:00+05:30")
    return tmp_path, str(repo), str(succ)


def test_find_repos_skips_noise(graveyard):
    root, repo, succ = graveyard
    found = excavate.find_repos(str(root))
    assert found == sorted([repo, succ], key=str.lower)


def test_vitals_read_the_body(graveyard):
    _, repo, _ = graveyard
    t = excavate.tombstone(repo, NOW)
    assert t["status"] == "dead"
    assert t["commits"] == 8
    assert t["last_words"] == "wip fix later"
    assert t["first_words"] == "initial commit"
    assert t["born"].startswith("2026-09-01T10:00:00")
    assert t["died"].startswith("2026-09-04T03:12:00")
    assert t["days_silent"] >= 53
    assert "part0.py" in t["modified"]
    assert "notes.txt" in t["untracked"]
    assert t["cause_hint"] == "Abandoned mid-thought"


def test_excavation_finds_unfinished_business_and_hides_secrets(graveyard):
    _, repo, _ = graveyard
    d = excavate.excavate(repo, NOW)
    todos = [e["text"] for e in d["evidence"].values() if e["kind"] == "todo"]
    assert any("handle the empty notebook case" in t for t in todos)
    assert any("voice crashes on long notes" in t for t in todos)
    assert any("add voice input" in t for t in todos)              # unchecked README box
    assert not any("parse notes" in t for t in todos)              # checked box is done
    everything = " ".join(e["text"] for e in d["evidence"].values())
    assert "sk-abcdefghijklmnop" not in everything
    assert d["redactions"] >= 1
    assert d["evidence"]["C1"]["text"].endswith("wip fix later")
    assert "U1" in d["evidence"] and "notes.txt" in d["evidence"]["U1"]["text"]
    assert sum(d["activity"]["buckets"]) == 8
    keys = {s["key"] for s in d["signals"]}
    assert {"mid_sentence", "unsaved", "sprint"} <= keys


def test_dossier_quotes_last_words_and_is_compact(graveyard):
    _, repo, _ = graveyard
    d = excavate.excavate(repo, NOW)
    text = excavate.dossier_text(d)
    assert 'LAST WORDS (final commit) [C1]: "wip fix later"' in text
    assert "SECRETS HIDDEN FROM YOU: 1" in text
    app_chunk = next(e for e in d["evidence"].values() if e["kind"] == "file" and "app.py" in e["label"])
    assert 'API_KEY = "[REDACTED]"' in app_chunk["text"]
    assert len(text) < 9000


def test_successor_is_the_project_born_after_death(graveyard):
    root, repo, succ = graveyard
    tombs = [excavate.tombstone(r, NOW) for r in excavate.find_repos(str(root))]
    me = next(t for t in tombs if t["path"] == repo)
    s = excavate.find_successor(me, tombs)
    assert s and s["name"] == "new-shiny" and s["days_after"] == 2
    d = excavate.excavate(repo, NOW)
    excavate.attach_successor(d, s)
    assert "X1" in d["evidence"] and "X1" in excavate.dossier_ids(d)


def test_stillborn(tmp_path):
    repo = tmp_path / "never"
    repo.mkdir()
    git(repo, "init", "-q")
    t = excavate.tombstone(str(repo), NOW)
    assert t["status"] == "stillborn" and t["cause_hint"] == "Stillborn"
    d = excavate.excavate(str(repo), NOW)
    assert "stillborn" in excavate.dossier_text(d)


def test_team_sprint_with_a_final_commit_is_a_hackathon_not_abandonment(tmp_path):
    """The amazon-ml case: 3 people, 2 days, ends on 'Model v3 ...', notebooks still dirty."""
    repo = tmp_path / "amazon-ml"
    repo.mkdir()
    git(repo, "init", "-q")
    for i, who in enumerate(["Rehan1604", "Bhavya Gothi", "Devansh270"] * 3):
        (repo / f"nb{i % 2}.ipynb").write_text(f'{{"cells": [{i}]}}\n', encoding="utf-8")
        git(repo, "add", ".")
        subprocess.run(["git", "-c", f"user.name={who}", "-c", "user.email=x@y.z", "commit", "-q", "-m",
                        "Model v3, judge/validation notebooks" if i == 8 else f"train step {i}"],
                       cwd=repo, check=True, capture_output=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": who, "GIT_COMMITTER_NAME": who,
                            "GIT_AUTHOR_DATE": f"2026-09-2{5 + i // 4}T1{i}:00:00+05:30",
                            "GIT_COMMITTER_DATE": f"2026-09-2{5 + i // 4}T1{i}:00:00+05:30"})
    (repo / "nb0.ipynb").write_text('{"cells": ["rerun"]}\n', encoding="utf-8")
    d = excavate.excavate(str(repo), NOW)
    keys = {s["key"] for s in d["signals"]}
    assert {"unsaved", "final_bow", "sprint", "team"} <= keys
    assert d["cause_hint"] == "The hackathon ended"
    assert "Bhavya Gothi" in d["evidence"]["V1"]["text"]
    assert "nb0.ipynb" in d["evidence"]["L1"]["text"]
    text = excavate.dossier_text(d)
    assert "[V1]" in text and "[L1]" in text and "LEFT BEHIND" in text


def test_redaction_leaves_ordinary_code_alone():
    code = 'token = request.headers.get("Authorization")\npassword = "hunter2hunter2"\nkey = "ghp_' + "a" * 30 + '"'
    out, n = excavate.redact(code)
    assert 'request.headers.get("Authorization")' in out
    assert "hunter2hunter2" not in out and "ghp_" not in out
    assert n == 2


def test_bm25_surfaces_the_right_memory():
    idx = retrieval.BM25({
        "C1": "fix login redirect loop",
        "T1": "TODO: add voiceInput support for long notes",
        "R1": "A dashboard for receipts",
    })
    assert idx.search("why no voice input?")[0] == "T1"
    assert idx.search("voice", exclude={"T1"}) == []


def test_citations_are_checked():
    h = spirit.verify_citations("I died at night [C1]. You promised voice [T9]. I miss you [C2, T1].", {"C1", "C2", "T1"})
    assert h["valid"] == ["C1", "C2", "T1"]
    assert h["phantom"] == ["T9"]
    assert h["sentences"] == 3 and h["grounded_sentences"] == 2


def test_sentences_that_open_with_curly_quotes_or_brackets_are_split():
    """From the real amazon-ml recording: a field starting with ‘ was merged into the sentence before it."""
    text = "I could truly demonstrate my value [L1]. ‘Model v3’ – September 27, 2026 at 16:28 [C1]. (A sigh) Gone."
    assert len(spirit.sentences_of(text)) == 3


def test_autopsy_fields_are_checked_separately(graveyard):
    _, repo, _ = graveyard
    d = excavate.excavate(repo, NOW)
    a = spirit.clean_autopsy({"cause_of_death": "Abandoned mid-thought",
                              "cause_explanation": "You left me with wip fix later [C1].",
                              "what_i_wanted_to_be": "I wanted to be a useful tool [C1].",
                              "last_moment": "‘wip fix later’ at 03:12 [C1].", "epitaph": "x", "evidence_ids": []}, d)
    assert a["honesty"]["sentences"] == 3 and a["honesty"]["loose"] == ["C1"]


def test_a_citation_after_the_full_stop_belongs_to_the_sentence_before():
    h = spirit.verify_citations("Silent. [C1].", {"C1"})
    assert h["sentences"] == 1 and h["grounded_sentences"] == 1
    h = spirit.verify_citations("I am… still processing. [C1]. You left.", {"C1"})
    assert h["sentences"] == 2 and h["grounded_sentences"] == 1


def test_autopsy_cleanup_rejects_invented_causes(graveyard):
    _, repo, _ = graveyard
    d = excavate.excavate(repo, NOW)
    raw = {
        "cause_of_death": "Eaten by wolves",
        "cause_explanation": "You stopped mid-sentence [C1]. You swore you'd fix me [C99].",
        "what_i_wanted_to_be": "A voice for old notes [R2].",
        "last_moment": "3:12 AM, wip [C1].",
        "epitaph": "Here rests a voice that never spoke [C1] and never will, not even once more",
        "evidence_ids": ["C1", "ZZ9"],
    }
    a = spirit.clean_autopsy(raw, d)
    assert a["cause_of_death"] == d["cause_hint"]
    assert a["honesty"]["phantom"] == ["C99"]
    assert "[C1]" not in a["epitaph"] and len(a["epitaph"].split()) <= 15
    assert "ZZ9" not in a["evidence_ids"]


def test_obituary_is_self_contained_and_hides_nothing(graveyard):
    _, repo, _ = graveyard
    d = excavate.excavate(repo, NOW)
    md = spirit.obituary_markdown(d, "You left me with the words wip fix later [C1].\n\n"
                                     "You saw the late nights fueled by coffee [C2]. I learned patience [Q7].")
    assert md.startswith("# Here lies haunted-app")
    assert "`wip fix later`" in md
    assert "[C1]" not in md and "[Q7]" not in md
    short_c1 = d["evidence"]["C1"]["label"].split(" · ")[0]
    short_c2 = d["evidence"]["C2"]["label"].split(" · ")[0]
    assert f"(`{short_c1}`)" in md                       # supported: plain hash
    assert f"(`{short_c2}`†)" in md                      # coffee is not in that commit: dagger
    assert "~~Q7~~ phantom memory" in md                 # invented evidence: struck through, not dropped
    assert "One citation points at a source" in md and "struck through" in md
    assert "## Unfinished business" in md and "## Unsaved at time of death" in md


def test_loose_citations_are_caught():
    ev = {
        "C1": {"kind": "commit", "label": "92bd43b · 2026-09-27 16:28",
               "text": "2026-09-27 16:28 Rehan1604: Model v3, judge/validation notebooks",
               "match": "2026-09-27 16:28 September Sunday Model v3, judge/validation notebooks"},
        "U1": {"kind": "unsaved", "label": "uncommitted", "text": "created but never added: Documentation_template.md"},
    }
    h = spirit.verify_citations(
        "You left me at 16:28 with Model v3 [C1]. The documentation sits untouched [U1]. "
        "Rehan, you saw the late nights fueled by coffee [C1].", ev)
    assert h["grounded_sentences"] == 3 and h["phantom"] == []
    assert h["loose"] == ["C1"]                          # only the coffee sentence; author names don't count
    assert not spirit.is_loose("The documentation sits untouched", ev["U1"])


def test_seance_messages_keep_the_case_file_first(graveyard):
    _, repo, _ = graveyard
    d = excavate.excavate(repo, NOW)
    history = [{"role": "user", "content": spirit.OPENING}, {"role": "assistant", "content": "You came back [C1]."}]
    msgs = spirit.seance_messages(d, history, "what was in notes.txt?")
    assert msgs[0]["role"] == "system" and "EVIDENCE" in msgs[0]["content"]
    assert msgs[1]["content"].startswith("(The one who made you")
    assert msgs[-1]["content"].startswith("what was in notes.txt?")
    # identical system prompt across turns, so Ollama can reuse its prompt cache
    assert spirit.seance_messages(d, [], "hello")[0] == msgs[0]
