#!/bin/bash
# Logic mutations, the companion to mutate.sh.
#
# Constants were the easy half. The misses that survived a constant sweep were
# both boundaries: a --max-price ceiling that had only ever been tested well
# above and well below, never *on*, and an is_error check whose test passed
# because the error text happened to contain no JSON array -- right outcome,
# wrong reason, and green with the check deleted.
#
# Re-run after touching a comparison operator or a branch condition.
cd ~/data/track
mut() {
  local f="src/track/$1" from="$2" to="$3" label="$4"
  cp "$f" "$f.bak"
  python3 - "$f" "$from" "$to" <<'PY'
import sys, pathlib
p=pathlib.Path(sys.argv[1]); t=p.read_text()
if sys.argv[2] not in t: sys.exit(3)
p.write_text(t.replace(sys.argv[2], sys.argv[3], 1))
PY
  if [ $? -eq 3 ]; then echo "  SKIP    $label"; mv "$f.bak" "$f"; return; fi
  find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  if .venv/bin/python -m pytest -q >/dev/null 2>&1; then echo "  MISSED  $label"; else echo "  caught  $label"; fi
  mv "$f.bak" "$f"
}
mut engine.py "assignment.runs_count > 0 and" "assignment.runs_count >= 0 and" "rediscover: fires on run 0 too (double discovery on first run)"
mut engine.py "backend != \"wake\" or assignment.status != \"active\"" "backend != \"wake\" and assignment.status != \"active\"" "rearm: or -> and (a paused assignment re-arms itself)"
mut engine.py "is_new = not store.has_seen(assignment.id, key)" "is_new = True" "every sighting reported as new (Discord repeats forever)"
mut engine.py "history = store.price_history(assignment.id)  # frozen for the whole run, on purpose" "history = []  # frozen" "run scores against no history"
mut report.py "f.price > assignment.max_price" "f.price >= assignment.max_price" "ceiling: > -> >= (a find exactly at budget is excluded)"
mut report.py "        if not f.is_new:\n            continue" "        if False:\n            continue" "summary reports every listing every run"
mut report.py "reverse=True" "reverse=False" "summary ranks the WORST finds first"
mut scoring.py "if h >= price" "if h > price" "score: ties no longer count as beaten"
mut scoring.py ".lower().rstrip(\"/\")" ".lower()" "dedup: trailing slash makes a new listing every run"
mut scoring.py "(s.cheapest is None, s.cheapest" "(False, s.cheapest" "stats: unreadable sources sort as cheapest"
mut store.py "            reverse=True," "            reverse=False," "best_findings returns the worst finds"
mut scheduler.py "int(base + max(interval_seconds, MIN_LEAD_SECONDS))" "int(base + min(interval_seconds, MIN_LEAD_SECONDS))" "floor: max -> min (6h interval becomes 60s)"
mut scouts.py "    if envelope.get(\"is_error\"):" "    if False:" "a failed scout is parsed as if it succeeded"
mut cli.py "status != \"active\" and not args.force" "status != \"active\" or not args.force" "run: and -> or (active assignments refuse to run)"
mut store.py "GROUP BY dedup_key" "GROUP BY id" "latest_findings stops collapsing duplicates"
find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
