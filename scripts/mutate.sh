#!/bin/bash
# Kept in the repo because the four constants it caught were all "green for a
# reason weaker than it looked": assertions comparing a value against the
# constant that produced it, which pass just as happily when the constant is
# wrong. Re-run it after touching any tuning constant.
# Flip a constant, clear .pyc (wake-dev's stale-bytecode trap), run the suite.
cd ~/data/track
run() {
  local file="$1" from="$2" to="$3" label="$4"
  cp "$file" "$file.bak"
  sed -i "s|^$from|$to|" "$file"
  if ! grep -q "^$to" "$file"; then echo "  SKIP  $label (pattern did not apply)"; mv "$file.bak" "$file"; return; fi
  find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
  if .venv/bin/python -m pytest -q >/dev/null 2>&1; then
    echo "  GREEN $label   <-- test suite did not notice"
  else
    echo "  caught $label"
  fi
  mv "$file.bak" "$file"
}
S=src/track/scheduler.py; C=src/track/scouts.py; E=src/track/engine.py; R=src/track/report.py
run $S "RESUME_GRACE_SECONDS = 120" "RESUME_GRACE_SECONDS = 0"      "RESUME_GRACE_SECONDS 120->0 (run fires before box is up)"
run $S "MIN_LEAD_SECONDS = 60"      "MIN_LEAD_SECONDS = 0"          "MIN_LEAD_SECONDS 60->0 (spin guard gone)"
run $C 'SCOUT_MAX_BUDGET_USD = "0.50"' 'SCOUT_MAX_BUDGET_USD = "999"' "SCOUT_MAX_BUDGET_USD 0.50->999 (no spend ceiling)"
run $C 'SCOUT_STRICT_MCP = True'    'SCOUT_STRICT_MCP = False'      "SCOUT_STRICT_MCP True->False (MCP servers return)"
run $C 'SCOUT_SETTING_SOURCES = ""' 'SCOUT_SETTING_SOURCES = "user"' "SCOUT_SETTING_SOURCES inherits user settings"
run $E "REDISCOVER_EVERY = 10"      "REDISCOVER_EVERY = 1"          "REDISCOVER_EVERY 10->1 (rediscover every run)"
run $E "DEFAULT_SOURCE_LIMIT = 5"   "DEFAULT_SOURCE_LIMIT = 1"      "DEFAULT_SOURCE_LIMIT 5->1 (one scout only)"
run $R "TOP_N = 5"                  "TOP_N = 1"                     "TOP_N 5->1 (summary shows one find)"
run $R "TOP_SOURCES = 3"            "TOP_SOURCES = 0"               "TOP_SOURCES 3->0 (no source stats posted)"
