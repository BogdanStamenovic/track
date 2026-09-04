# track

Give it an assignment — "a powerful but cheap laptop", "a used Eurorack filter
under $80" — and it works out who realistically sells that kind of thing cheap
and where, keeps checking on a schedule, and posts what it found to Discord.

Each run spins up Sonnet scouts (short-lived `claude -p` sessions) against the
sources it knows about, scores what they find against everything ever seen for
that assignment, and reports only what is new and genuinely underpriced. It
also accumulates statistics on the sources themselves, so over time you learn
which places actually turn up bargains for that category and which never do.

`track` never buys anything and never contacts a seller. It is a research
tool, not a checkout bot.

## Install

```
ownbox install track
```

Or manually:

```
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

```
track add "<what to track>" [--interval 6h] [--max-price N] [--notify AGENT]
                            [--wake-backend shell|rtcwake|wol] [--wake-target MAC]
                            [--wake-on HOST] [--no-schedule]
track list [--json]
track show <assignment-id> [--limit N] [--json]
track sources <assignment-id> [--json]
track run <assignment-id> | --all-active [--no-post] [--force]
track unschedule <assignment-id>
track pause <assignment-id>
track resume <assignment-id>
track remove <assignment-id>
```

| Option | Meaning |
| --- | --- |
| `--interval` | how often to re-check: `6h`, `30m`, `2d`, or bare seconds |
| `--max-price` | ceiling; dearer finds are still recorded, just kept out of the summary |
| `--market` | where you are buying from, e.g. `Serbia` — a hard constraint on which sources count, not a preference (default `$TRACK_MARKET`) |
| `--notify` | hotline agent whose Discord channel gets the summary — **needed for scheduled runs** (see below) |
| `--wake-backend` | `shell` for a machine that stays up; `rtcwake`/`wol` to resume a sleeping one first |
| `--wake-target` | MAC address, for `--wake-backend wol` |
| `--wake-on` | wake origin name of the machine that should run the check |
| `unschedule` | drop track's own recurring wakeup but keep the assignment runnable, for when an external scheduler owns the timing |
| `--no-post` | run without posting to Discord |
| `--all-active` | run every active assignment in turn, for a scheduler that owns one wakeup for the whole database |
| `--force` | run an assignment that is paused |
| `-v, --verbose` | detailed progress on stderr |
| `-q, --quiet` | suppress non-error output |
| `--db PATH` | override the sqlite path (default `~/.local/share/track/track.db`) |
| `--version` | print the version and exit |

Exit codes: `0` success, `1` an operation failed, `2` usage error. `track run`
refines that, because it is the command a scheduler fires with nobody watching
and its exit status is the only signal that reaches anyone:

| Code | Meaning |
| --- | --- |
| `0` | a summary was posted and it contained at least one usable finding |
| `1` | a summary was posted, honestly, but there was nothing usable in it |
| `2` | usage error |
| `3` | the summary could not be posted at all |

"Ran but found nothing" is deliberately not `0`: silence and success must not
look alike to whatever fired the run. A report that never reached its channel
gets its own code rather than being folded into a generic failure, because it
is the one outcome nobody will otherwise hear about.

`--all-active` collapses several runs into one code on the same principle: `3`
if **any** summary failed to post, else `0` if any assignment turned something
up, else `1`. One silent assignment out of three is still a silent
assignment. Assignments run one after another, not in parallel — a single
cycle already fans out five scouts — and one failing assignment never stops
the ones after it.

stdout carries real output only — assignment ids, listings, summaries; everything
else goes to stderr.

### Example

A real run, unedited:

```
$ track add "a 16GB DDR4-3200 desktop RAM kit, used or open-box" \
      --interval 6h --max-price 60 --notify track-dev
b15f0a7b

$ track run b15f0a7b
3 listing(s) came back without a price -- the site would not serve one
**track** — "a 16GB DDR4-3200 desktop RAM kit, used or open-box"
7 sources checked · 4 listings seen · 3 new · 1 over the 60.00 ceiling

• **16 gb ddr4 ram** — price unknown @ Facebook Marketplace
  <https://www.facebook.com/marketplace/item/1706457870761672/>
• **Open Box Crucial Pro 32GB (2 x 16GB) DDR4 3200** — price unknown @ Newegg

Cheapest sources so far:
  Micro Center: from 87.96 USD (median 87.96 USD, 1 listings)
  no price readable from: Newegg, Facebook Marketplace

_scouts: ~$0.29 of model usage at list price (no charge on a Claude subscription)_

$ track sources b15f0a7b
b15f0a7b: a 16GB DDR4-3200 desktop RAM kit, used or open-box
  Micro Center: 1 listings, 1 priced (100%), from 87.96 USD, median 87.96 USD
  Newegg: 2 listings, 0 priced (0%), from ?, median ?
  Facebook Marketplace: 1 listings, 0 priced (0%), from ?, median ?
```

## How it works

**Scouts.** Every run fans out one `claude -p --model sonnet` session per
source. They get `WebSearch` and `WebFetch` and nothing else — see *Scouts are
deliberately caged* below — and hand back strict JSON.

**Market.** Without `--market`, scouts return whatever the open web surfaces,
which in practice means US sources — eBay, Newegg, and a chain of walk-in
stores on another continent. Set it and it becomes a hard constraint in the
scout prompt: local marketplaces and classifieds, sellers who actually ship
there, local-language sites over international ones. Export `TRACK_MARKET`
once rather than passing it every time. `track add` warns when neither is set.

**Tiers.** Once a run has enough priced findings in one currency, the summary
leads with budget / mid / stretch rather than a flat ranked list, picking the
best-*scoring* listing in each band rather than the cheapest — the cheapest
thing in the budget band is usually the one with the worst specifications, and
the question being answered is "what is the best buy at roughly this price".
Below that threshold it falls back to a ranked list, because thirds drawn from
four listings describe the sample rather than the market. Tiers are cut inside
a single currency and the summary says which, since track does not convert and
the other currency's listings are genuinely unranked against them.

**Scoring is per currency.** A finding is only ever compared against others
quoted in the same currency. Pooling them as bare numbers would rank every
dinar-priced listing above every euro one; in a live Serbian run, a 120 EUR
listing scores 0.83 against the EUR history rather than 1.00 for being
numerically smaller than every RSD price on record. Source statistics are
keyed by source *and* currency for the same reason — a median taken across two
currencies describes nothing.

**Scoring is about mispricing, not cheapness.** A finding's score answers "is
this priced below what this thing actually goes for", which needs a reference
price for the *comparable* rather than a ranking of every number the
assignment has ever seen. `0.50` means priced at the going rate, above means
under it, below means over it; the score is linear in the discount, so `0.80`
is thirty percent under and `1.00` is half price or better.

The reference is the median asking price of the listing's comparables — other
listings in the same currency whose titles share enough distinctive weight to
be the same thing. Similarity is IDF-weighted containment: shared weight over
the *shorter* title, so a terse `ASUS PH-RTX3060-12G-V2` and a chatty `Nvidia
RTX 3060 12GB (appears underpriced vs other 3060 12GB listings, EUR 245-440)`
still recognise each other. The deviation is weighted `n/(n+1)`, so a
reference nobody corroborates states half of what it saw.

The market is snapshotted once at the start of a run and not extended while
that run scores, so two identical runs cannot disagree just because their
scouts returned in a different order. It spans the assignment's history *and*
the whole of the current run's haul, because comparables usually arrive
together: five RTX 3060s priced 230 to 440 EUR turned up in one run of a
brand-new assignment, and against history alone there was nothing to compare
them with.

**No comparable means a degraded score, not a missing one.** A listing with
nothing to compare against falls back to the old measure — the share of known
prices it undercuts — and is marked `score_basis = 'cheapness'` so the weaker
claim is visible rather than blended in. That happens for about 7% of priced
listings on the data this was tuned against.

**What the change is worth, measured on 152 priced listings:**

| | cheapness (old) | mispricing (new) |
| --- | --- | --- |
| listings with a real comparable | 0% | 92.8% |
| correlation of score with raw price | −0.546 | **+0.011** |
| correlation with 29 hand-verified comparable discounts | +0.777 | **+0.871** |

The middle row is the one that matters and it needs no labels: a cheapness
percentile is very nearly a restatement of "small number", and a valuation
should be near zero there, because an expensive thing can be the better deal.
The third row is a check rather than a verdict — 29 listings in six groups
that a human confirmed are the same thing, scored against their own
leave-one-out median.

The other label-free test is stability. Injecting twelve more RTX 3090
listings says nothing whatsoever about what an RTX 3060 is worth, so nothing
about a 3060's valuation should move:

```
                        230     245     300     310     440 EUR
cheapness         0.92->0.96  ->0.92  ->0.88  ->0.83  0.58->0.79
mispricing        0.62->0.62  ->0.59  ->0.44  ->0.61  0.20->0.20
```

Cheapness moves every one of them, and the *worst* deal in the class climbs
from 0.58 to 0.79 because unrelated dearer cards arrived. Across all 152
listings the largest drift is 0.43 for cheapness and 0.19 for mispricing.

On real data the biggest single mover was an HP ZBook Power 15 G10
(i7-13700H, 1TB, RTX 3000) at 327,860 RSD, **0.10 → 0.66** — an expensive
machine that is cheap for what it is, which the old score buried for being a
big number. In the other direction a 220 EUR Acer Aspire went 0.91 → 0.26:
cheap in absolute terms, dear for an Aspire.

**Findings are append-only.** A run never rewrites or rescores an earlier one.
A listing seen five times has five rows, which is how a price drop is visible
at all; anything reasoning about the current market collapses each listing to
its latest row first, so a stale listing cannot outvote a rare cheap one
purely by surviving more runs.

**A listing's identity is its URL, unless that URL is an index page.**
`dedup_key` is built from host + path with the query string stripped, because
that is where trackers and session ids live. That breaks when a scout answers
with the *search-results* URL for every hit: ten graphics cards priced 1 to
1100 EUR came back on one `kupujemprodajem.com/.../pretraga` path, hashed to
one key, and nine of them stopped existing for every query downstream —
scoring, source statistics, the summary. Keying on the title instead breaks
the opposite case: one genuine product page came back under four title
spellings across four runs, and title-keyed that is four listings with no
price history each.

So index pages are identified by observation, not by guessing at URL shape: a
URL base that comes back attached to **more than one distinct title within a
single run** is serving a list, and its listings are keyed by title instead.
Measured across the 9 runs on record, 147 bases had exactly one title per run
and 7 had more; all 7 were index pages (two `?pretraga=` searches, two
category pages, one bare host), and no product page was misclassified. The
classification is recorded per assignment and never reverted — a base that
flipped back because one run returned a single result from it would key that
run's listing differently from every other run's.

**Source statistics** are derived from the findings by query rather than kept
as counters, because a counter can drift out of step with the rows it claims
to summarise and a query cannot.

**The reaper.** After every research cycle, one more scout re-checks
listings the run did *not* see — a listing a scout just found is alive by
definition — and retires what has gone. It works through the back catalogue
least-recently-checked first, six at a time, so a run's cost does not scale
with the size of its history.

Six is measured, not chosen. A batch of six real listing URLs took **20s and
$0.216** and resolved all six off the pages themselves ("Odmah dostupno",
"posted 9 days ago, no sold marking"). That is $0.036 a URL, so twelve would
cost about $0.43 against a $0.50 per-scout ceiling — and twelve is exactly
what timed out at 120s on the first live run, correctly retiring nothing and
wasting the scout. Six leaves 72% headroom on the clock at half the budget.
What grows with the back catalogue is how long a full sweep takes: at a 6h
interval a 137-listing assignment is swept in under four days, and faster in
practice, since listings the run itself saw need no check.

**Retirement is a marking. Nothing is ever deleted.** A retired listing keeps
every sighting row it ever had, so "what did that cost in July" stays
answerable after the advert is gone, and a listing that turns up alive in a
later run is silently un-retired with its failure count reset.

**A failed fetch is not proof a listing is dead.** Timeouts, rate limits and
anti-bot walls are the normal weather of reading third-party marketplaces,
and treating one as a death certificate would quietly empty the database of
exactly the sources that block hardest. So the check answers one of four
states and they are handled differently:

| state | meaning | what happens |
| --- | --- | --- |
| `live` | the page loaded and it is still on offer | failure count reset |
| `gone` | the page says sold/expired/removed, or 404 | retired immediately, `reason='gone'` |
| `blocked` | the **site** refused: 403, captcha, login gate | recorded, **never counted** — a 403 is evidence about the site and none about the listing |
| `unknown` | timeout, network error, unreadable page | counts; retired only after `FAILURES_BEFORE_RETIRING` separate checks |

A URL nothing came back for is `unknown`, never `gone` — silence is the
commonest way a batch check goes wrong, and eight unanswered URLs must not
read as eight sales. That gap-filling lives in the reaper rather than the
scout, so the guarantee holds whoever does the checking. The row records
which signal fired, and the summary reports the blocked and unreachable
counts next to the retirements rather than only the retirements.

**Superseding is held to a much higher bar than price comparison**, and the
two use different similarity measures on purpose. Finding a reference price
wants recall, so it uses the overlap coefficient — shared weight over the
*shorter* title. That measure ignores whatever the longer title says beyond
the overlap, which is harmless for a median and fatal for a claim that one
listing replaces another. Measured on 162 live listings, retiring anything
with a cheaper comparable:

| rule | retired | an actual replacement it chose |
| --- | --- | --- |
| overlap ≥ 0.45, 15% cheaper | 48% | ProBook 650 G2 for a ProBook 450 G7 |
| overlap ≥ 0.90, 20% cheaper | 18% | a listing titled "Thinkpad" for a P52 workstation |
| symmetric ≥ 0.75, 10% cheaper | ~0% | — |

The loose rules retire half the market on replacements a person would reject
at a glance. The strict rule almost never fires, and **that is the honest
finding rather than a failure to tune**: what high title similarity actually
turns up in this data is duplicates at the *same* price, which is
bookkeeping and not a better deal. So supersession ships strict, and a
"comparable" priced under a quarter of the listing it would replace is
ignored outright — an RTX 3090 was posted at 1 EUR, and without that floor it
would retire every real 3090 on the board.

**Scheduling.** `track add` arms the next check with the sibling
[`wake`][wake] CLI. Wake tasks are one-shot, so each run re-arms its own
successor before it returns, always under the same task id (`track-<id>`) —
an interrupted run cannot leave two timers racing on the next one. If `wake`
isn't installed, `track` falls back to a `systemd --user` timer, which recurs
by itself.

**Waking a sleeping machine takes two tasks, not one.** `rtcwake` and
Wake-on-LAN only bring a box back to life; neither runs anything once it is
up. So those backends schedule a pair: the resume at T, and the shell task
that actually runs `track` at T + 120s, by which time the machine is up to
receive it. With `--wake-on`, that second task is pinned to the machine that
was woken rather than to the wake server.

[wake]: https://github.com/BogdanStamenovic/wake

## Scouts are deliberately caged

An early version gave scouts the default tool set. Handed eBay's 403 anti-bot
wall, one responded by spoofing headers and routing requests through
third-party proxies to get around it, and burned its entire timeout doing so.
That is the behaviour this section exists to prevent.

Each scout is launched with:

- **`--tools WebSearch,WebFetch`** — replaces the *available* tool set, so
  there is no Bash to reach for. This is the load-bearing control: it is the
  difference between telling a scout not to defeat a block and it having
  nothing to defeat one with. `--allowedTools` alone is not this; it only
  governs which tools run without approval.
- **`--allowedTools WebSearch,WebFetch`** — the same two, pre-approved.
  Required *as well*: `--setting-sources ""` throws away the operator's
  permission rules, and an un-allowlisted tool in a `--print` session has
  nobody left to approve it. Passing only `--tools` produced a scout that
  reported, correctly and uselessly, that both of its tools were denied.
- **`--strict-mcp-config`** — no MCP servers. An inherited browser-automation
  server would hand back exactly the capability `--tools` just removed.
- **`--setting-sources ""`** — no inherited hooks or permissions.
- **`--max-budget-usd`** and a wall-clock timeout — this is an unattended job
  on a timer, not a session anyone is watching.

One thing this cannot do from inside the CLI: a scout still inherits the
operator's global `CLAUDE.md` and memory. On the machine this was built for,
that memory's first line is *"improvise, never declare impossible — route
around every blocker"*, which is precisely the instruction that produced the
proxy incident. The only flags that suppress memory loading (`--bare`,
`CLAUDE_CODE_SIMPLE=1`) also disable OAuth and demand an API key — both were
tried, both return `Not logged in`. So the countermand lives in the scout
prompt instead. The tool restriction is what makes a workaround impossible;
the prompt is what stops the scout wasting its budget attempting one.

## Testing

`.venv/bin/python -m pytest` runs the suite. `scripts/mutate.sh` is a small
mutation check that flips each tuning constant and reports whether the suite
notices. It exists because four of them did not: assertions like
`assert gap == RESUME_GRACE_SECONDS` pass just as happily when that constant
is zero, so they pinned nothing. Those now assert the property that matters
(a resumed machine needs *some* seconds to come up; a scout's budget ceiling
must stay *small*) with the constant checked alongside it.

`scripts/mutate-logic.sh` does the same for comparison operators and branch
conditions, which is where the sharper holes were: a `--max-price` ceiling
tested at 900 and 300 against a limit of 500 but never *at* 500, so `>` and
`>=` were indistinguishable and a find priced exactly at budget could have been
dropped; and an `is_error` check whose test passed only because the error text
contained no JSON array, staying green with the check removed entirely.

Clear `__pycache__` before trusting a re-run after editing a single constant —
same file size and same mtime second means Python will reuse the stale
bytecode and report a result for code you are no longer running. That happened
here, on source verified byte-identical to git.

## Running it unattended

`track run` is designed to be fired by a scheduler on a machine with nobody
logged in, which is why its exit codes are the way they are. Measured facts
from deploying it that way, rather than assumptions:

- **It needs nothing from the environment.** Verified under
  `env -i HOME=... PATH=/usr/local/bin:/usr/bin`, cwd `/tmp`: exit 0, summary
  posted. The database path, the market and the notify target all come from
  the assignment row, and `hotline-say` is resolved by absolute fallback. A
  systemd unit's environment is close to this bare.
- **Runtime is 45–110s** for five parallel scouts. Allow at least 300s.
- **The Claude access token is short-lived** (8 hours on a subscription) and
  the CLI refreshes it headlessly, rotating the refresh token as it does.
  Both were verified deliberately, by forcing `expiresAt` into the past: a
  single `claude -p` refreshed cleanly, and so did a full run launching five
  scouts *concurrently* against an already-expired token, with no failures.
  That race is the one worth knowing about — five processes noticing an
  expired token at the same instant is exactly what a scheduled run does.
- **Test the refresh in place, never against a copy of the credentials.**
  Refreshing rotates the refresh token, so a sandboxed copy that refreshes
  successfully leaves the real credentials holding an invalidated one — it
  would *cause* the outage it was meant to rule out.
- **Reporting does not depend on the research credentials.** `hotline-say`
  posts with its own Discord token, so a total scout failure still delivers an
  honest report and exits 1 rather than going silent.

## Limitations

- **Research only.** It never places an order, messages a seller, or holds
  payment credentials. By design, not as an unfinished feature.
- **Blocked sites come back with `price: null`.** eBay, Facebook Marketplace
  and Newegg all did in testing. A caged scout can see from a search snippet
  that a listing exists without being able to read its price, and reporting
  that honestly is the correct outcome — the `sources` view shows it as a low
  "priced" rate rather than hiding it. It is not a bug and there is no plan
  to route around it.
- **Scout quality is whatever the open web will give a read-only session.**
  There is no marketplace API access, no private inventory feed, no login.
- **The reference price is what other sellers are asking, not what anything
  sold for.** There is no sold-price feed, so an entire category listed
  optimistically reads as fairly priced. It measures dispersion within a
  market, not the market's level.
- **Comparables need a corpus.** Below roughly fourteen listings the
  similarity weights have too little to work with: sampling the GPU
  assignment down to six listings, only 59% of a known comparable group still
  found each other, against 98% at twenty. New assignments therefore lean on
  the cheapness fallback for their first run or two.
- **A reference from one peer is one seller's opinion.** `reference_n` says
  how many backed it and the score is weighted accordingly, but `n = 1` is
  common and it is a weak reference, not a market rate.
- **Nothing filters out a listing error.** An RTX 3090 posted at 1 EUR scores
  1.00, correctly by the arithmetic and uselessly in fact. The reference price
  is shown next to it so a human can see what happened; there is no
  too-good-to-be-true rule, because one that suppressed this would also
  suppress a genuine steal.
- **A scout is not a price oracle, measured.** Asked directly what six known
  items go for, one Sonnet scout was within 1.4% and 0.4% on the two it
  actually looked up and off by −37%, −41% and +39% on the three it answered
  from general knowledge after spending its three-call budget, and returned
  `null` for the sixth. Median absolute error 37%, against a bargain signal of
  20–35% — the instrument's error is larger than what it would measure, so
  scout-quoted reference prices are deliberately not used.
- **A run consumes whatever `claude` is authenticated with.** On an API key
  that is billable credit; on a Claude subscription it draws down the plan's
  usage allowance and bills nothing. Either way the figure track records is
  the list-price estimate the CLI reports, not necessarily a charge — roughly
  $0.10–$0.45 per run in testing, five scouts wide, and `track show` reports
  the running total per assignment. Treat it as a measure of how much work a
  run did.
- **Retirement is only as good as one scout's reading of a page.** A listing
  that is quietly reserved rather than marked sold reads as live, and a page
  that renders its status in JavaScript may read as either. The four states
  are what a read-only fetch can honestly distinguish, not ground truth.
- **The `wake` integration is one file.** `scheduler.py` is the only place
  that knows wake's CLI shape; if that contract changes, nothing else does.
- **Self-re-arming needs `wake` 7041d08 or newer.** Because `track run`
  re-arms the same task id while wake is still holding it open, an older wake
  stamped `status=fired` over the re-arm after the command returned — leaving
  a row with a correct future time and a dead status that never fired again,
  silently, exit 0 on both sides. Wake's fire path is now a compare-and-set
  and gives way to a task that re-armed itself. Against an older wake, use
  the systemd fallback instead.
- **The systemd fallback is Linux-only and cannot wake a suspended box.**
  It refuses `--wake-backend rtcwake`/`wol` outright rather than pretending.
  Waking a sleeping machine needs `wake` installed.
- **The systemd fallback catches up a missed run**, but through
  `OnActiveSec=0s` — the timer fires as soon as it is activated, including at
  every user-manager start — not through `Persistent=`, which only does that
  for `OnCalendar=` timers and used to sit in the unit reading like a
  guarantee it wasn't providing.
- **No wakeup is ever scheduled less than 60s out.** Every run re-arms the
  next one, so a past-dated wakeup would not fire once — it would spin as
  fast as the scheduler polls.

## Not built yet

- No per-source scheduling — every source is scouted on the same interval.
- No currency *conversion*. Prices are never pooled across currencies, which
  is the correctness-critical half, but track also cannot tell you whether
  6,900 RSD beats 60 EUR. It reports both and leaves the arithmetic to you.
- No condition or specification *matching* beyond what the assignment text
  tells a scout. Condition is now recorded when a listing states it, but
  nothing filters on it: track will happily report a 32GB kit for a 16GB
  assignment if a scout thought it was relevant.
- No alerting threshold — every run posts, even a quiet one.
- Nothing re-checks a listing whose only URL is a search or category page,
  which is 39 of 137 listings on one assignment and 9 of 42 on the other. That page renders
  fine whether or not the item is still on it, so a "live" answer would mean
  nothing and a "gone" answer would retire everything behind that URL at
  once. Those listings can be retired as superseded, never as gone. The same
  applies to a find that came back with no URL at all.
- Supersession almost never fires on the data measured so far, by design —
  see the table above. If the intent is "hide anything worse than the best
  find", that is a ranking question and the score already answers it.
