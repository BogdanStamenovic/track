"""Head-to-head on reference-price strategies, measured on the real database.

Not part of the shipped tool. It exists so the scoring change is promoted on
numbers rather than on taste, and so the numbers can be re-run when the data
grows. Run: .venv/bin/python prototypes/refprice.py
"""

from __future__ import annotations

import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

DB = Path.home() / ".local" / "share" / "track" / "track.db"


@dataclass
class Row:
    id: int
    assignment_id: str
    run_id: int
    source: str
    title: str
    price: float
    currency: str | None


def load() -> list[Row]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM findings WHERE id IN ("
        " SELECT MAX(id) FROM findings GROUP BY assignment_id, dedup_key)"
    ).fetchall()
    conn.close()
    return [
        Row(r["id"], r["assignment_id"], r["run_id"], r["source"], r["title"],
            r["price"], r["currency"])
        for r in rows
        if r["price"] is not None
    ]


# -- key / neighbourhood strategies ---------------------------------------

_CAP = re.compile(r"\b(\d+)\s*(gb|tb|mb)\b")
_NOISE = {
    "laptop", "notebook", "ram", "ssd", "hdd", "nvme", "m", "gb", "tb", "inch",
    "novo", "novi", "nov", "kao", "odlican", "odlicno", "top", "akcija", "br",
    "grade", "the", "and", "with", "za", "i", "u", "na", "sa", "gaming", "oc",
    "graphics", "geforce", "nvidia", "amd", "intel", "core", "ips", "fhd",
    "touch", "touchscreen", "display", "screen", "bat", "h", "w", "v", "x",
    "premium", "plus", "pro", "max", "edition", "out", "of", "stock",
}


def tokenize(title: str) -> list[str]:
    t = title.lower()
    caps = [f"{n}{u}" for n, u in _CAP.findall(t)]
    t = _CAP.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [w for w in t.split() if w and w not in _NOISE and len(w) > 1]
    return words + caps


def idf(corpus: list[list[str]]) -> dict[str, float]:
    n = len(corpus)
    df: dict[str, int] = defaultdict(int)
    for toks in corpus:
        for tok in set(toks):
            df[tok] += 1
    return {tok: math.log(n / d) for tok, d in df.items()}


def similarity(a: list[str], b: list[str], weights: dict[str, float]) -> float:
    """IDF-weighted overlap: shared rare tokens count, shared boilerplate doesn't."""
    sa, sb = set(a), set(b)
    shared = sa & sb
    union = sa | sb
    if not union:
        return 0.0
    num = sum(weights.get(t, 0.0) for t in shared)
    den = sum(weights.get(t, 0.0) for t in union)
    return num / den if den else 0.0


def exact_key(title: str) -> str:
    """Strong tokens only: anything carrying a digit, plus capacities."""
    toks = tokenize(title)
    strong = sorted({t for t in toks if any(c.isdigit() for c in t)})
    return "|".join(strong)


# -- scoring ---------------------------------------------------------------

def cheapness(price: float, history: list[float]) -> float:
    if not history:
        return 0.5
    return sum(1 for h in history if h >= price) / len(history)


def mispricing(price: float, reference: float) -> float:
    if reference <= 0:
        return 0.5
    return max(0.0, min(1.0, 0.5 + (reference - price) / reference))


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def rank(vs: list[float]) -> list[float]:
        order = sorted(range(len(vs)), key=lambda i: vs[i])
        r = [0.0] * len(vs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    dy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return num / (dx * dy) if dx and dy else None


# -- strategies ------------------------------------------------------------

def strategy_cheapness(rows: list[Row]) -> dict[int, tuple[float, str, int]]:
    out = {}
    by_ac = defaultdict(list)
    for r in rows:
        by_ac[(r.assignment_id, r.currency)].append(r.price)
    for r in rows:
        hist = by_ac[(r.assignment_id, r.currency)]
        out[r.id] = (cheapness(r.price, hist), "cheapness", len(hist))
    return out


def strategy_similarity(
    rows: list[Row], threshold: float, min_n: int, same_run_only: bool = False
) -> dict[int, tuple[float, str, int]]:
    corpus = {r.id: tokenize(r.title) for r in rows}
    weights = idf(list(corpus.values()))
    by_ac = defaultdict(list)
    for r in rows:
        by_ac[(r.assignment_id, r.currency)].append(r)
    out = {}
    for r in rows:
        peers = [
            p for p in by_ac[(r.assignment_id, r.currency)]
            if p.id != r.id
            and (not same_run_only or p.run_id == r.run_id)
            and similarity(corpus[r.id], corpus[p.id], weights) >= threshold
        ]
        if len(peers) >= min_n:
            ref = statistics.median([p.price for p in peers])
            out[r.id] = (mispricing(r.price, ref), "mispricing", len(peers))
        else:
            hist = [p.price for p in by_ac[(r.assignment_id, r.currency)]]
            out[r.id] = (cheapness(r.price, hist), "cheapness", 0)
    return out


def strategy_exact_key(rows: list[Row], min_n: int) -> dict[int, tuple[float, str, int]]:
    groups = defaultdict(list)
    for r in rows:
        groups[(r.assignment_id, r.currency, exact_key(r.title))].append(r)
    by_ac = defaultdict(list)
    for r in rows:
        by_ac[(r.assignment_id, r.currency)].append(r.price)
    out = {}
    for r in rows:
        peers = [
            p for p in groups[(r.assignment_id, r.currency, exact_key(r.title))] if p.id != r.id
        ]
        if len(peers) >= min_n:
            ref = statistics.median([p.price for p in peers])
            out[r.id] = (mispricing(r.price, ref), "mispricing", len(peers))
        else:
            out[r.id] = (cheapness(r.price, by_ac[(r.assignment_id, r.currency)]), "cheapness", 0)
    return out


# -- metrics ---------------------------------------------------------------

# Hand-picked comparable sets from his own data: listings a human agrees are
# the same thing at different prices. The ordering test is "does the score
# rank these by how good a deal they are", which for identical hardware is
# exactly reverse price order.
TRUTH_SETS = [
    ("rtx3060-12gb-eur", ["RTX 3060 12GB", "MSI GeForce RTX 3060 12GB VENTUS",
                          "RTX 3060 12GB ASUS DUAL", "ASUS PH-RTX3060-12G-V2",
                          "ASUS DUAL-RTX3060-O12G-V2", "ASUS Dual-Rtx3060-O12G-V2"]),
    ("elitebook-855-g8", ["HP EliteBook 855 G8"]),
    ("thinkpad-t490-eur", ["Lenovo ThinkPad T490 i7"]),
]


def evaluate(name: str, rows: list[Row], scores: dict[int, tuple[float, str, int]]) -> None:
    priced = [r for r in rows if r.price is not None]
    mis = [r for r in priced if scores[r.id][1] == "mispricing"]
    coverage = len(mis) / len(priced) if priced else 0.0

    # M3: does the score still just track "small number"?
    rho_all = spearman([scores[r.id][0] for r in priced], [r.price for r in priced])

    # M2: inside a set of genuine comparables, does it rank by price?
    rhos = []
    for _label, prefixes in TRUTH_SETS:
        members = [
            r for r in priced
            if any(r.title.lower().startswith(p.lower()[:18]) or p.lower()[:18] in r.title.lower()
                   for p in prefixes)
        ]
        by_cur = defaultdict(list)
        for m in members:
            by_cur[m.currency].append(m)
        for group in by_cur.values():
            if len(group) >= 3:
                rho = spearman([scores[r.id][0] for r in group], [r.price for r in group])
                if rho is not None:
                    rhos.append(rho)
    m2 = statistics.mean(rhos) if rhos else float("nan")

    spread = statistics.pstdev([scores[r.id][0] for r in priced]) if len(priced) > 1 else 0.0
    print(
        f"{name:<34} coverage {coverage:5.1%}  "
        f"rho(score,price) all {rho_all if rho_all is None else round(rho_all, 3):>6}  "
        f"rho within comparables {m2:>6.3f}  score sd {spread:.3f}"
    )


def group_purity(rows: list[Row], threshold: float) -> None:
    """How tight are the neighbourhoods? A key that lumps everything together
    has a huge within-group price spread and its 'reference' means nothing."""
    corpus = {r.id: tokenize(r.title) for r in rows}
    weights = idf(list(corpus.values()))
    by_ac = defaultdict(list)
    for r in rows:
        by_ac[(r.assignment_id, r.currency)].append(r)
    cvs = []
    sizes = []
    for r in rows:
        peers = [
            p for p in by_ac[(r.assignment_id, r.currency)]
            if p.id != r.id and similarity(corpus[r.id], corpus[p.id], weights) >= threshold
        ]
        if len(peers) >= 2:
            prices = [p.price for p in peers]
            m = statistics.mean(prices)
            cvs.append(statistics.pstdev(prices) / m if m else 0.0)
            sizes.append(len(peers))
    if cvs:
        print(
            f"  threshold {threshold:.2f}: {len(cvs)} listings with >=2 peers, "
            f"median peers {statistics.median(sizes):.0f}, "
            f"median within-group CV {statistics.median(cvs):.3f}"
        )
    else:
        print(f"  threshold {threshold:.2f}: no listing had >=2 peers")


def main() -> int:
    rows = load()
    print(f"{len(rows)} priced distinct listings across "
          f"{len({r.assignment_id for r in rows})} assignments\n")

    print("Neighbourhood tightness vs threshold (CV = price spread inside a group):")
    for t in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50):
        group_purity(rows, t)
    print()

    evaluate("P0 cheapness (today)", rows, strategy_cheapness(rows))
    for t in (0.20, 0.25, 0.30, 0.35, 0.40):
        evaluate(f"P1 similarity t={t:.2f} min2", rows, strategy_similarity(rows, t, 2))
    evaluate("P1 similarity t=0.30 min1", rows, strategy_similarity(rows, 0.30, 1))
    evaluate("P2 same-run only t=0.30 min2", rows,
             strategy_similarity(rows, 0.30, 2, same_run_only=True))
    evaluate("P3 exact strong-token key min2", rows, strategy_exact_key(rows, 2))
    evaluate("P3 exact strong-token key min1", rows, strategy_exact_key(rows, 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
