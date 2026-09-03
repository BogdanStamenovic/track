# track

Give it an assignment -- "a powerful but cheap laptop", "a used Eurorack
filter under $80" -- and it researches who realistically sells that kind of
thing cheap and where, schedules itself to re-check on an interval, and
posts a summary of what it found to Discord.

Each run spins up Sonnet scouts (short-lived `claude -p` sessions) to search
the sources it knows about, scores what they find against everything ever
seen for that assignment (not just this run), and only surfaces genuinely
new, genuinely underpriced listings. `track` never buys anything or
contacts a seller -- it's a research tool, not a checkout bot.

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
track add "<what to track>" [--interval 6h] [--max-price N] [--no-schedule]
track list
track show <assignment-id> [--limit N]
track run <assignment-id> [--no-post]
track pause <assignment-id>
track resume <assignment-id>
track remove <assignment-id>
```

| Option | Meaning |
| --- | --- |
| `-v, --verbose` | print detailed progress to stderr |
| `-q, --quiet` | suppress non-error output |
| `--db PATH` | override the sqlite database path (default `~/.local/share/track/track.db`) |
| `--version` | print the version and exit |

### Examples

```
$ track add "a powerful but cheap laptop" --interval 4h
a1b2c3d4

$ track list
a1b2c3d4    active    a powerful but cheap laptop    (last run: never)

$ track run a1b2c3d4
track: "a powerful but cheap laptop" (5 sources checked, 8 listings seen)
- ThinkPad P52 32GB/1TB — 310.00 USD @ eBay (score 0.91)
  https://...
...
```

`track run` is also what a scheduled wakeup invokes. On `add`, `track`
schedules its own recurring check with the sibling [`wake`][wake] CLI. Since
`wake` tasks are one-shot, each run that used the `wake` backend re-arms its
own successor right before it returns. If `wake` isn't installed yet, `track`
falls back to a `systemd --user` timer, which recurs on its own.

[wake]: https://github.com/BogdanStamenovic/wake

## Limitations

- Research-only. It never places an order, messages a seller, or holds
  payment credentials -- by design, not as an unfinished feature.
- Scout quality depends entirely on what the underlying Claude session can
  actually find on the open web; it has no special access to marketplace
  APIs or private inventory feeds. Scouts run with only `WebSearch` and
  `WebFetch` (no shell, no proxies) and a hard `--max-budget-usd` /
  tool-call ceiling, deliberately -- an early version, given full tool
  access, responded to eBay's anti-bot 403s by spoofing headers and piping
  requests through third-party proxies instead of reporting what it found.
  Sites that block scraping (eBay confirmed) mean listings often come back
  with `price: null` rather than a fetched exact price -- that's the honest
  result of a read-only scout respecting the block, not a bug.
- The `wake` integration is one choke point (`scheduler.py`) built against
  `wake`'s v1 CLI contract; if that contract changes, only that file needs
  to change.
- The systemd fallback is Linux-only (`systemctl --user`) and needs a
  running (not suspended) box -- it doesn't attempt WoL or rtcwake itself;
  that's `wake`'s job once it's installed.
