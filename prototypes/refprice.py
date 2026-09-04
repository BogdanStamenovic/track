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


_RUN = re.compile(r"[a-z]+|\d+")


def tokenize(title: str) -> list[str]:
    """Words, plus capacities, plus the letter/digit runs inside mixed tokens.

    The last part is load-bearing: "PH-RTX3060-12G-V2" and "MSI GeForce RTX
    3060 12GB" are the same card, and without splitting `rtx3060` into `rtx`
    and `3060` they share nothing at all. Splitting keeps the whole token too,
    so an exact model code still counts for more than its pieces.
    """
    t = title.lower()
    caps = [f"{n}{u}" for n, u in _CAP.findall(t)]
    t = _CAP.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [w for w in t.split() if w and w not in _NOISE and len(w) > 1]
    parts = []
    for w in words:
        if w.isalpha() or w.isdigit():
            continue
        parts += [r for r in _RUN.findall(w) if len(r) > 1 and r not in _NOISE]
    return words + caps + parts


def idf(corpus: list[list[str]]) -> dict[str, float]:
    n = len(corpus)
    df: dict[str, int] = defaultdict(int)
    for toks in corpus:
        for tok in set(toks):
            df[tok] += 1
    return {tok: math.log(n / d) for tok, d in df.items()}


def similarity(a: list[str], b: list[str], weights: dict[str, float]) -> float:
    """IDF-weighted Jaccard: shared rare tokens count, shared boilerplate doesn't."""
    sa, sb = set(a), set(b)
    shared = sa & sb
    union = sa | sb
    if not union:
        return 0.0
    num = sum(weights.get(t, 0.0) for t in shared)
    den = sum(weights.get(t, 0.0) for t in union)
    return num / den if den else 0.0


def containment(a: list[str], b: list[str], weights: dict[str, float]) -> float:
    """IDF-weighted overlap coefficient: shared weight over the *shorter* title.

    Jaccard punishes length asymmetry, and scout titles are wildly asymmetric --
    "MSI GeForce RTX 3060 12GB VENTUS 2X" against "Nvidia RTX 3060 12GB (appears
    underpriced vs other 3060 12GB listings on same search, EUR 245-440)". Those
    are the same card and Jaccard scores them apart, because the second title's
    commentary inflates the union.
    """
    sa, sb = set(a), set(b)
    shared = sum(weights.get(t, 0.0) for t in sa & sb)
    smaller = min(
        sum(weights.get(t, 0.0) for t in sa), sum(weights.get(t, 0.0) for t in sb)
    )
    return shared / smaller if smaller else 0.0


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

def strategy_cheapness(
    rows: list[Row], extra: list[Row] | None = None
) -> dict[int, tuple[float, str, int]]:
    out = {}
    by_ac = defaultdict(list)
    for r in rows + list(extra or []):
        by_ac[(r.assignment_id, r.currency)].append(r.price)
    for r in rows:
        hist = by_ac[(r.assignment_id, r.currency)]
        out[r.id] = (cheapness(r.price, hist), "cheapness", len(hist))
    return out


def strategy_similarity(
    rows: list[Row], threshold: float, min_n: int, same_run_only: bool = False,
    metric=similarity, extra: list[Row] | None = None,
) -> dict[int, tuple[float, str, int]]:
    pool = rows + list(extra or [])
    corpus = {r.id: tokenize(r.title) for r in pool}
    weights = idf(list(corpus.values()))
    by_ac = defaultdict(list)
    for r in pool:
        by_ac[(r.assignment_id, r.currency)].append(r)
    out = {}
    for r in rows:
        peers = [
            p for p in by_ac[(r.assignment_id, r.currency)]
            if p.id != r.id
            and (not same_run_only or p.run_id == r.run_id)
            and metric(corpus[r.id], corpus[p.id], weights) >= threshold
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


def group_purity(rows: list[Row], threshold: float, metric=None) -> None:
    """How tight are the neighbourhoods a threshold builds?

    A reference drawn from a group whose own prices vary by 50% is not a
    reference. This is the objective half of the comparison -- it needs no
    labels, and it is the trade-off against coverage.
    """
    corpus = {r.id: tokenize(r.title) for r in rows}
    weights = idf(list(corpus.values()))
    by_ac = defaultdict(list)
    for r in rows:
        by_ac[(r.assignment_id, r.currency)].append(r)
    cvs, sizes = [], []
    for r in rows:
        peers = [
            p for p in by_ac[(r.assignment_id, r.currency)]
            if p.id != r.id
            and (metric or similarity)(corpus[r.id], corpus[p.id], weights) >= threshold
        ]
        if len(peers) >= 2:
            prices = [p.price for p in peers]
            m = statistics.mean(prices)
            cvs.append(statistics.pstdev(prices) / m if m else 0.0)
            sizes.append(len(peers))
    if cvs:
        print(
            f"  t={threshold:.2f}: {len(cvs):>3} listings with >=2 peers, "
            f"median peers {statistics.median(sizes):.0f}, "
            f"median within-group CV {statistics.median(cvs):.3f}"
        )
    else:
        print(f"  t={threshold:.2f}: no listing had >=2 peers")


# -- metrics ---------------------------------------------------------------

# Hand-verified comparable sets: listings a human confirms are the same thing.
# The machine's job is to find these groups on its own; supplying them by hand
# is the only way to have an answer to check against. Identifying the group is
# the part machines get wrong -- once the group is fixed, the "right" score is
# arithmetic: how far below the group's leave-one-out median this one is.
#
# It is a thin truth set and mostly graphics cards, because that is where his
# data actually has price dispersion within one model. Treat it as a screen.
TRUTH_GROUPS = {
    "rtx3060-12gb-eur": [176, 177, 178, 179, 181],
    "rtx3090-24gb-eur": [175, 183, 184, 212, 213, 214, 215],
    "rtx4060ti-16gb-rsd": [188, 189, 190],
    "elitebook-855-g8-rsd": [92, 153, 152, 151, 54, 53, 52, 22, 21, 146],
    "latitude-7490-i7-eur": [137, 144],
    "thinkpad-t490-i7-eur": [138, 106],
}


def truth_discounts(rows: dict[int, Row]) -> dict[int, float]:
    """Ground-truth "how good a deal is this": discount vs the group's peers."""
    out: dict[int, float] = {}
    for ids in TRUTH_GROUPS.values():
        present = [i for i in ids if i in rows]
        for i in present:
            peers = [rows[j].price for j in present if j != i]
            if not peers:
                continue
            ref = statistics.median(peers)
            if ref > 0:
                out[i] = (ref - rows[i].price) / ref
    return out


@dataclass
class Report:
    name: str
    coverage: float
    rho_truth: float | None
    n_truth: int
    rho_price: float | None
    median_cv: float | None
    sd: float


def evaluate(name: str, rows: list[Row], scores: dict[int, tuple[float, str, int]]) -> Report:
    by_id = {r.id: r for r in rows}
    priced = rows
    covered = [r for r in priced if scores[r.id][1] == "mispricing"]

    truth = truth_discounts(by_id)
    graded = [i for i in truth if i in scores]
    rho_truth = spearman([scores[i][0] for i in graded], [truth[i] for i in graded])

    # Does the score still just mean "small number"? Cheapness is ~-1 by
    # construction; a mispricing score should be much weaker, because an
    # expensive thing can be the better deal.
    rho_price = spearman([scores[r.id][0] for r in priced], [r.price for r in priced])

    return Report(
        name=name,
        coverage=len(covered) / len(priced) if priced else 0.0,
        rho_truth=rho_truth,
        n_truth=len(graded),
        rho_price=rho_price,
        median_cv=None,
        sd=statistics.pstdev([scores[r.id][0] for r in priced]) if len(priced) > 1 else 0.0,
    )


def show(r: Report) -> None:
    def f(v: float | None) -> str:
        return "  n/a " if v is None else f"{v:+.3f}"

    print(
        f"  {r.name:<32} cover {r.coverage:5.1%}   "
        f"vs truth {f(r.rho_truth)}   vs price {f(r.rho_price)}   sd {r.sd:.3f}"
    )


def headline_cases(rows: list[Row], scores: dict[int, tuple[float, str, int]]) -> None:
    """The two listings this change exists for."""
    by_id = {r.id: r for r in rows}
    for label, fid in [
        ("230 EUR RTX 3060 (underpriced, cheap)", 176),
        ("1100 EUR RTX 3090 (fairly priced, dear)", 184),
        ("2000 EUR RTX 3090 (overpriced, dear)", 215),
        ("145730 RSD HP OmniBook (dear, unknown)", 174),
    ]:
        if fid in scores and fid in by_id:
            score, basis, n = scores[fid]
            print(f"    {label:<42} {score:.2f}  [{basis}, n={n}]")


def perturbation(rows: list[Row]) -> None:
    """Does an unrelated model class moving into the history change your score?

    The objective test, needing no labels. Ten more RTX 3090 listings say
    nothing whatsoever about what an RTX 3060 is worth, so a valuation must
    not move. A cheapness percentile must, because its denominator is every
    price the assignment has ever seen regardless of what the thing is.
    """
    group = [176, 177, 178, 179, 181]  # the RTX 3060 12GB comparables, EUR
    dear = [r for r in rows if r.id in (183, 184, 212, 213, 214, 215)]
    ghosts = [
        Row(9000 + i, r.assignment_id, 99, r.source, r.title, r.price, r.currency)
        for i, r in enumerate(dear * 2)
    ]
    print(f"  injecting {len(ghosts)} more RTX 3090 listings (EUR 1100-2000) into the history:")
    for label, before, after in [
        (
            "P0 cheapness",
            strategy_cheapness(rows),
            strategy_cheapness(rows, extra=ghosts),
        ),
        (
            "P1 containment t=0.35 min2",
            strategy_similarity(rows, 0.35, 2, metric=containment),
            strategy_similarity(rows, 0.35, 2, metric=containment, extra=ghosts),
        ),
    ]:
        drift = [abs(after[i][0] - before[i][0]) for i in group if i in before and i in after]
        moved = [
            f"{rows_by_id(rows)[i].price:.0f}: {before[i][0]:.2f}->{after[i][0]:.2f}"
            for i in group
            if i in before
        ]
        print(f"    {label:<28} max drift {max(drift):.2f}   {', '.join(moved)}")


def rows_by_id(rows: list[Row]) -> dict[int, Row]:
    return {r.id: r for r in rows}


def main() -> int:
    rows = load()
    print(f"{len(rows)} priced distinct listings across "
          f"{len({r.assignment_id for r in rows})} assignments")
    print(f"{sum(len(v) for v in TRUTH_GROUPS.values())} listings in "
          f"{len(TRUTH_GROUPS)} hand-verified comparable groups\n")

    print("Neighbourhood tightness vs threshold (CV = price spread inside a group):")
    for t in (0.15, 0.20, 0.25, 0.30, 0.35, 0.40):
        group_purity(rows, t)
    print()

    strategies: list[tuple[str, dict[int, tuple[float, str, int]]]] = [
        ("P0 cheapness (today)", strategy_cheapness(rows)),
    ]
    for t in (0.15, 0.20, 0.25, 0.30, 0.35):
        strategies.append(
            (f"P1 similarity t={t:.2f} min2", strategy_similarity(rows, t, 2))
        )
    strategies += [
        ("P1 similarity t=0.20 min1", strategy_similarity(rows, 0.20, 1)),
        ("P2 same-run only t=0.20 min2", strategy_similarity(rows, 0.20, 2, same_run_only=True)),
        ("P3 exact strong-token min2", strategy_exact_key(rows, 2)),
        ("P3 exact strong-token min1", strategy_exact_key(rows, 1)),
    ]
    for t in (0.30, 0.35, 0.40, 0.50):
        strategies.append(
            (
                f"P4 containment t={t:.2f} min2",
                strategy_similarity(rows, t, 2, metric=containment),
            )
        )
    strategies.append(
        ("P4 containment t=0.35 min1", strategy_similarity(rows, 0.35, 1, metric=containment))
    )

    print("Neighbourhood tightness, containment instead of Jaccard:")
    for t in (0.30, 0.35, 0.40, 0.50, 0.60):
        group_purity(rows, t, metric=containment)
    print()

    print("Strategies (rho vs truth: higher is better; rho vs price: nearer 0 is better):")
    for name, scores in strategies:
        show(evaluate(name, rows, scores))

    print("\nHeadline cases under P0 cheapness:")
    headline_cases(rows, strategy_cheapness(rows))
    print("\nHeadline cases under P4 containment t=0.35 min2:")
    headline_cases(rows, strategy_similarity(rows, 0.35, 2, metric=containment))

    print("\nStability: does an unrelated model class change the score?")
    perturbation(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
