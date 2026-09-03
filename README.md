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
track run <assignment-id> [--no-post] [--force]
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

_scouts: $0.292_

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

**Scoring.** A finding's score is the share of the assignment's known prices it
undercuts: `1.00` is cheaper than everything on record, `0.50` means there was
no history to judge against yet. The history is snapshotted once at the start
of a run and not extended while that run scores, so two identical runs cannot
disagree just because their scouts returned in a different order.

**Findings are append-only.** A run never rewrites or rescores an earlier one.
A listing seen five times has five rows, which is how a price drop is visible
at all; anything reasoning about the current market collapses each listing to
its latest row first, so a stale listing cannot outvote a rare cheap one
purely by surviving more runs.

**Source statistics** are derived from the findings by query rather than kept
as counters, because a counter can drift out of step with the rows it claims
to summarise and a query cannot.

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
- **Scores are relative, not absolute.** `1.00` means "cheapest this
  assignment has ever seen", which on a young assignment with three data
  points means very little. It is a ranking signal, not a valuation.
- **A run consumes whatever `claude` is authenticated with.** On an API key
  that is billable credit; on a Claude subscription it draws down the plan's
  usage allowance and bills nothing. Either way the figure track records is
  the list-price estimate the CLI reports, not necessarily a charge — roughly
  $0.10–$0.45 per run in testing, five scouts wide, and `track show` reports
  the running total per assignment. Treat it as a measure of how much work a
  run did.
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
- No condition or specification matching beyond what the assignment text tells
  a scout. It will happily report a 32GB kit for a 16GB assignment if a scout
  thought it was relevant.
- No alerting threshold — every run posts, even a quiet one.
