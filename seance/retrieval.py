"""Memories surfacing: a tiny BM25 index over the excavated evidence.

When you ask the ghost something the case file doesn't cover, this finds the
commits, TODOs and file fragments that might hold the answer. Pure Python,
no embedding model, nothing to download.
"""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = set(
    "the a an and or of to in on for with is it its this that be as at by from was were are "
    "i you my your we our me what why how when did do does have has had not no so if then "
    "there their they them he she his her who whom which would could should will can".split()
)
_WORD = re.compile(r"[A-Za-z0-9_]+")
_PARTS = re.compile(r"[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+")


def tokenize(text: str) -> list[str]:
    toks = []
    for word in _WORD.findall(text):
        parts = [p for chunk in word.split("_") for p in _PARTS.findall(chunk)] or [word]
        if len(parts) > 1:
            parts.append(word)  # keep the whole identifier too
        for p in parts:
            p = p.lower()
            if len(p) > 1 and p not in STOP:
                toks.append(p)
    return toks


class BM25:
    def __init__(self, docs: dict[str, str], k1: float = 1.4, b: float = 0.75):
        self.ids = list(docs)
        self.toks = [tokenize(docs[i]) for i in self.ids]
        self.tf = [Counter(t) for t in self.toks]
        self.avg = (sum(map(len, self.toks)) / len(self.toks)) if self.toks else 1.0
        self.avg = self.avg or 1.0
        df: Counter = Counter()
        for tf in self.tf:
            df.update(tf.keys())
        n = len(self.ids)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self.k1, self.b = k1, b

    def search(self, query: str, k: int = 5, exclude: set[str] | frozenset = frozenset()) -> list[str]:
        q = set(tokenize(query))
        scored = []
        for i, tf in enumerate(self.tf):
            if self.ids[i] in exclude:
                continue
            dl = len(self.toks[i]) or 1
            s = 0.0
            for w in q:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
            if s > 0:
                scored.append((s, self.ids[i]))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [i for _, i in scored[:k]]


def build_index(evidence: dict[str, dict]) -> BM25:
    return BM25({eid: f"{e['label']} {e['text']}" for eid, e in evidence.items()})
