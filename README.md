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
track pause <assignment-id>
track resume <assignment-id>
track remove <assignment-id>
```

| Option | Meaning |
| --- | --- |
| `--interval` | how often to re-check: `6h`, `30m`, `2d`, or bare seconds |
| `--max-price` | ceiling; dearer finds are still recorded, just kept out of the summary |
| `--notify` | hotline agent whose Discord channel gets the summary — **needed for scheduled runs** (see below) |
| `--wake-backend` | `shell` for a machine that stays up; `rtcwake`/`wol` to resume a sleeping one first |
| `--wake-target` | MAC address, for `--wake-backend wol` |
| `--wake-on` | wake origin name of the machine that should run the check |
| `--no-post` | run without posting to Discord |
| `--force` | run an assignment that is paused |
| `-v, --verbose` | detailed progress on stderr |
| `-q, --quiet` | suppress non-error output |
| `--db PATH` | override the sqlite path (default `~/.local/share/track/track.db`) |
| `--version` | print the version and exit |

Exit codes: `0` success, `1` an operation failed, `2` usage error. stdout
carries real output only — assignment ids, listings, summaries; everything
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
- **A run costs real money.** Roughly $0.10–$0.45 per run in testing, five
  scouts wide. `track show` reports the running total per assignment.
- **The `wake` integration is one file.** `scheduler.py` is the only place
  that knows wake's CLI shape; if that contract changes, nothing else does.
- **The systemd fallback is Linux-only and cannot wake a suspended box.**
  It refuses `--wake-backend rtcwake`/`wol` outright rather than pretending.
  Waking a sleeping machine needs `wake` installed.

## Not built yet

- No per-source scheduling — every source is scouted on the same interval.
- No currency normalisation. Prices are compared as numbers, so an assignment
  whose sources quote in different currencies will score them against each
  other as if they were the same unit.
- No condition or specification matching beyond what the assignment text tells
  a scout. It will happily report a 32GB kit for a 16GB assignment if a scout
  thought it was relevant.
- No alerting threshold — every run posts, even a quiet one.
