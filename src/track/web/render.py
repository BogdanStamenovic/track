"""HTML for the viewer.

Server-rendered, no client framework, no build step: every control is a plain
GET link so the whole thing works with JavaScript off and reads fine on a phone.
The one rule the templates enforce everywhere is that an absent value is
rendered as absent -- "no reason recorded", "no price listed" -- and never
filled in with a guess.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from .data import SORTS, Assignment, Listing, parse_ts

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f6f6f4; --card: #fff; --ink: #16181c; --dim: #6b7280; --line: #e3e3df;
  --accent: #1f6feb; --good: #157347; --dead: #b42318; --warn: #9a6700;
  --chip: #ececea;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#111315; --card:#191c1f; --ink:#e8e8e6; --dim:#98a0a8; --line:#2a2f34;
          --accent:#5aa9ff; --good:#4ec98a; --dead:#ff8079; --warn:#d9a400;
          --chip:#23272b; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 -apple-system,
       BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
a { color: var(--accent); }
.wrap { max-width: 900px; margin: 0 auto; padding: 14px 12px 60px; }
header h1 { font-size: 19px; margin: 4px 0 2px; }
header p { margin: 0 0 10px; color: var(--dim); font-size: 13px; }
.bar { display:flex; flex-wrap:wrap; gap:6px; margin: 10px 0; }
.chip { display:inline-block; padding:5px 10px; border-radius:999px; background:var(--chip);
        color:var(--ink); text-decoration:none; font-size:13px; border:1px solid transparent; }
.chip.on { background:var(--accent); color:#fff; }
.chip.mini { padding:2px 8px; font-size:12px; color:var(--dim); }
form.search { display:flex; gap:6px; margin:10px 0; }
form.search input { flex:1; min-width:0; padding:9px 11px; border-radius:9px;
        border:1px solid var(--line); background:var(--card); color:var(--ink); font-size:16px; }
form.search button { padding:9px 14px; border-radius:9px; border:1px solid var(--line);
        background:var(--chip); color:var(--ink); font-size:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:12px 13px; margin-bottom:10px; }
.card.dead { opacity:.62; border-style:dashed; }
.card.dead .title { text-decoration: line-through; }
.title { font-weight:600; font-size:15px; margin:0 0 6px; line-height:1.35; word-break:break-word; }
.title a { text-decoration:none; }
.price { font-size:17px; font-weight:700; }
.none { color:var(--dim); font-weight:400; font-style:italic; }
.meta { display:flex; flex-wrap:wrap; gap:6px 12px; color:var(--dim); font-size:12.5px;
        margin-top:7px; }
.reason { margin-top:8px; padding:8px 10px; background:var(--chip); border-radius:8px;
          font-size:13.5px; }
.reason.absent { color:var(--dim); font-style:italic; }
.scorebar { height:4px; border-radius:2px; background:var(--line); margin-top:8px; overflow:hidden; }
.scorebar i { display:block; height:100%; background:var(--good); }
.badge { font-size:11px; font-weight:700; letter-spacing:.04em; text-transform:uppercase;
         padding:2px 7px; border-radius:5px; background:var(--chip); color:var(--dim); }
.badge.dead { background:var(--dead); color:#fff; }
.badge.warn { background:var(--warn); color:#fff; }
.reason.gone { border-left:3px solid var(--dead); }
.reason.warn { border-left:3px solid var(--warn); }
.badge.new { background:var(--good); color:#fff; }
.link { display:inline-block; margin-top:8px; font-size:13px; word-break:break-all; }
details.card > summary, details.gap > summary { cursor:pointer; font-size:13px; }
details.gap > summary { color:var(--dim); }
details.gap p { margin:8px 0 0; }
.gap { color:var(--dim); font-size:12.5px; margin:8px 0 14px; padding:8px 10px;
       border:1px dashed var(--line); border-radius:9px; }
footer { color:var(--dim); font-size:12px; margin-top:26px; border-top:1px solid var(--line);
         padding-top:10px; }
ul.plain { list-style:none; padding:0; margin:0; }
"""


def short_label(text: str, limit: int = 80) -> str:
    """A headline for an assignment whose `text` is a paragraph-long brief.

    track stores the full scouting brief in `assignments.text`, so the raw value
    is unusable as a title. Cut at the first sentence or clause boundary.
    """
    clean = " ".join((text or "").split())
    if not clean:
        return "(untitled assignment)"
    for sep in (": ", ". ", " -- ", ", "):
        head = clean.split(sep, 1)[0]
        if 8 <= len(head) <= limit:
            return head
    return clean[:limit].rstrip(" ,.;:") + ("\u2026" if len(clean) > limit else "")


def brief(text: str) -> str:
    """The full assignment text, folded away -- it is long and rarely re-read."""
    clean = " ".join((text or "").split())
    if not clean or clean == short_label(clean):
        return ""
    return (f'<details class="card"><summary>the full brief</summary>'
            f'<p style="margin:8px 0 0">{esc(clean)}</p></details>')


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def page(title: str, body: str, subtitle: str = "") -> str:
    sub = f"<p>{subtitle}</p>" if subtitle else ""
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body><div class=\"wrap\">"
        f"<header><h1>{esc(title)}</h1>{sub}</header>{body}"
        "</div></body></html>"
    )


def _href(base: str, **params: Any) -> str:
    clean = {k: v for k, v in params.items() if v not in (None, "", False)}
    return base + ("?" + urlencode(clean) if clean else "")


def fmt_price(item: Listing) -> str:
    if item.price is None:
        return '<span class="none">no price listed</span>'
    amount = f"{item.price:,.0f}" if abs(item.price) >= 100 else f"{item.price:,.2f}"
    text = f"{amount} {esc(item.currency)}" if item.currency else amount
    if item.price_stale:
        text += ' <span class="badge">last known</span>'
    return text


def fmt_ago(value: Any) -> str:
    moment = parse_ts(value)
    if moment is None:
        return "unknown"
    delta = (datetime.now(timezone.utc) - moment).total_seconds()
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def _age_bits(item: Listing) -> list[str]:
    """How old is it -- stated exactly, with each number's meaning attached."""
    bits = []
    if item.first_seen_at:
        bits.append(f"first seen by track {fmt_ago(item.first_seen_at)}"
                    f" ({esc(str(item.first_seen_at)[:10])})")
    else:
        bits.append("first seen: unknown")
    if item.listed_at:
        bits.append(f"listed {fmt_ago(item.listed_at)} ({esc(str(item.listed_at)[:10])})")
    elif item.extras.get("listing_age_days") is not None:
        bits.append(f"listed {_days(item.extras['listing_age_days'])} ago")
    if item.model_year:
        bits.append(f"model year {esc(item.model_year)}")
    return bits


def _status_note(item: Listing) -> str:
    """What track established about this listing, in track's own words."""
    if item.dead:
        note = item.retired_note or "no note recorded"
        when = f" ({fmt_ago(item.status.get('retired_at'))})" if item.status.get("retired_at") else ""
        return f'<div class="reason gone">Retired{when}: {esc(note)}</div>'
    if item.unverified:
        return (f'<div class="reason warn">Still listed as far as track knows, but the last '
                f"check did not confirm it: {esc(item.unverified)}. "
                "A failed check is not proof a listing is gone.</div>")
    return ""


def _score_basis(item: Listing) -> str:
    """The numeric half of "why": what the score was measured against."""
    reference = item.extras.get("reference_price")
    count = item.extras.get("reference_n")
    bits = []
    if isinstance(reference, (int, float)) and not isinstance(reference, bool):
        amount = f"{reference:,.0f}"
        currency = f" {esc(item.currency)}" if item.currency else ""
        if isinstance(count, (int, float)) and not isinstance(count, bool) and count:
            n = int(count)
            # The peer count is the honest measure of how much this score is
            # worth, so it is stated rather than folded away into "a reference".
            peers = f"{n} comparable" + ("" if n == 1 else "s")
            bits.append(f"vs {peers} at {amount}{currency}")
        else:
            bits.append(f"vs a reference at {amount}{currency}")
    if item.score_basis:
        bits.append(esc(item.score_basis))
    return " &middot; ".join(bits)


def _days(value: Any) -> str:
    """track stores the age as a float; "27.0 days" reads like a measurement."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return f"{esc(value)} days"
    days = float(value)
    if days < 1:
        return f"{days * 24:.0f} hours"
    text = f"{days:.0f}" if abs(days - round(days)) < 0.05 else f"{days:.1f}"
    return f"{text} day" + ("" if text == "1" else "s")


def _host(url: str | None) -> str:
    if not url:
        return ""
    return urlsplit(url).netloc or url


def render_listing(item: Listing, *, has_reason: bool = True) -> str:
    dead = item.dead is True
    badges = []
    if dead:
        badges.append(f'<span class="badge dead">{esc(item.retired_reason or "retired")}</span>')
    if item.is_new and not dead:
        badges.append('<span class="badge new">new</span>')
    if item.times_seen > 1:
        badges.append(f'<span class="badge">seen {item.times_seen}x</span>')

    title = esc(item.title)
    if item.url:
        title = f'<a href="{esc(item.url)}" target="_blank" rel="noreferrer">{title}</a>'

    if item.score is None:
        score_line = '<span class="none">not scored</span>'
        bar = ""
    else:
        stale = ' <span class="badge">earlier sighting</span>' if item.score_stale else ""
        score_line = f"score {item.score:.2f}{stale}"
        pct = max(0.0, min(1.0, float(item.score))) * 100
        bar = f'<div class="scorebar"><i style="width:{pct:.1f}%"></i></div>'
        basis = _score_basis(item)
        if basis:
            score_line += f" &middot; {basis}"

    # When the column does not exist yet, the banner says so once; repeating
    # "no reason recorded" on 130 identical cards is noise, not information.
    reason = item.reason
    if reason:
        reason_html = f'<div class="reason">{esc(reason)}</div>'
    elif has_reason:
        reason_html = '<div class="reason absent">no reason recorded</div>'
    else:
        reason_html = ""

    if item.url:
        link = (f'<a class="link" href="{esc(item.url)}" target="_blank" rel="noreferrer">'
                f"{esc(_host(item.url))} &rarr;</a>")
    else:
        link = '<div class="link none">no link recorded</div>'

    if item.unverified:
        # Not a strikethrough: track could not reach the page, which is a fact
        # about our reach, not about the listing. Said plainly, styled quietly.
        badges.append('<span class="badge warn">unverified</span>')

    meta = [f"<span>{esc(item.source)}</span>"]
    if item.condition:
        meta.append(f"<span>{esc(item.condition)}</span>")
    meta.append(f"<span>{score_line}</span>")
    meta += [f"<span>{bit}</span>" for bit in _age_bits(item)]
    if item.superseded_by:
        meta.append(f'<span>beaten by {esc(item.superseded_by[:12])}</span>')
    if item.status.get("last_checked_at"):
        meta.append(f'<span>checked {fmt_ago(item.status["last_checked_at"])}</span>')

    return (
        f'<article class="card{" dead" if dead else ""}">'
        f'<p class="title">{title}</p>'
        f'<div class="price">{fmt_price(item)} {" ".join(badges)}</div>'
        f"{bar}{reason_html}{_status_note(item)}"
        f'<div class="meta">{"".join(meta)}</div>'
        f"{link}</article>"
    )


def render_index(assignments: Sequence[Assignment], gaps: Sequence[str] = ()) -> str:
    if not assignments:
        body = '<p class="none">no assignments in the database yet</p>'
        return page("track", body)
    rows = []
    for a in assignments:
        dead = f" &middot; {a.dead_count} gone" if a.dead_count else ""
        rows.append(
            f'<article class="card"><p class="title">'
            f'<a href="/a/{esc(a.id)}">{esc(short_label(a.text))}</a></p>'
            f'<div class="meta"><span>{a.listing_count} listings{dead}</span>'
            f"<span>{esc(a.status)}</span>"
            f"<span>{esc(a.market) if a.market else 'market unset'}</span>"
            f"<span>{a.runs_count} runs</span>"
            f"<span>last run {fmt_ago(a.last_run_at) if a.last_run_at else 'never'}</span>"
            f'<span class="chip mini">{esc(a.id)}</span></div></article>'
        )
    return page("track", "".join(rows), "what is being hunted")


def _blank_note(gaps: Sequence[str], unfilled: Sequence[str]) -> str:
    """One folded line explaining every field that is blank on every card.

    Folded because on a phone two open banners push the first listing below the
    fold, and this is a footnote about the data rather than the data. It stays
    on the page rather than being dropped: a blank that is never explained reads
    as a fact about the listing.
    """
    total = len(gaps) + len(unfilled)
    if not total:
        return ""
    body = []
    if gaps:
        body.append(
            f"<p><b>Not recorded by track at all:</b> {', '.join(esc(g) for g in gaps)}. "
            "Blank because there is nowhere to put it, not because this listing lacks it.</p>"
        )
    if unfilled:
        body.append(
            f"<p><b>Recorded, but not captured yet:</b> {', '.join(esc(u) for u in unfilled)}. "
            "The columns exist and nothing has filled them on this assignment. "
            "They appear as soon as a run captures them.</p>"
        )
    field = "field is" if total == 1 else "fields are"
    return (f'<details class="gap"><summary>{total} {field} blank on every card '
            f"&mdash; why</summary>{''.join(body)}</details>")


def render_assignment(
    assignment: Assignment,
    listings: Sequence[Listing],
    *,
    total: int,
    sort: str,
    query: str,
    show_dead: bool,
    dead_known: bool,
    has_reason: bool,
    gaps: Sequence[str],
    unfilled: Sequence[str] = (),
) -> str:
    base = f"/a/{assignment.id}"
    chips = [
        f'<a class="chip{" on" if sort == key else ""}" '
        f'href="{_href(base, sort=key, q=query, dead=1 if show_dead else None)}">{esc(label)}</a>'
        for key, label in SORTS.items()
    ]
    if dead_known:
        chips.append(
            f'<a class="chip{" on" if show_dead else ""}" '
            f'href="{_href(base, sort=sort, q=query, dead=None if show_dead else 1)}">'
            f'{"showing gone" if show_dead else "hiding gone"}</a>'
        )

    search_form = (
        f'<form class="search" method="get" action="{esc(base)}">'
        f'<input type="search" name="q" placeholder="filter by title, source or reason" '
        f'value="{esc(query)}">'
        f'<input type="hidden" name="sort" value="{esc(sort)}">'
        + ('<input type="hidden" name="dead" value="1">' if show_dead else "")
        + "<button>filter</button></form>"
    )

    if listings:
        cards = "".join(render_listing(item, has_reason=has_reason) for item in listings)
    else:
        cards = '<p class="none">nothing matches that filter</p>'

    shown = f"{len(listings)} of {total} listings"
    sub = (f"{esc(assignment.status)} &middot; {shown}"
           f" &middot; last run {fmt_ago(assignment.last_run_at) if assignment.last_run_at else 'never'}")

    footer = (
        '<footer><a href="/">&larr; all assignments</a> &middot; '
        "read-only view of track.db; nothing here is written back. "
        f"assignment <code>{esc(assignment.id)}</code></footer>"
    )
    body = (brief(assignment.text) + '<div class="bar">' + "".join(chips) + "</div>"
            + search_form + _blank_note(gaps, unfilled) + cards + footer)
    return page(short_label(assignment.text) if assignment.text else assignment.id, body, sub)


def render_error(message: str, code: int) -> str:
    return page(
        f"{code}",
        f'<div class="card"><p class="title">{esc(message)}</p>'
        '<a class="link" href="/">&larr; back to assignments</a></div>',
    )
