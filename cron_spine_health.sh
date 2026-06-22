#!/usr/bin/env bash
# cron_spine_health.sh - CHG-P9-048-debt (Argos Sprint 1)
#
# Scheduled spine-health refresh. RUNS ON GARAGE via cron (NOT on the OptiPlex dev
# clone). The daily pricing_puller does NOT health-probe spine routes by design
# (the health gate owns spine liveness via route_health), so without this wrapper
# the spine routes' last_health goes stale with nothing refreshing it.
#
# This probes the 19 spine:litellm routes (~16 fast LiteLLM calls, mostly cache
# hits) and updates last_health in argos.db. Cheap enough to run every 4 hours.
#
# Operator: install this crontab line on garage (this script also tees its own
# output to the log, and the redirect below is belt-and-braces):
#
#   0 */4 * * * /home/andy/argos/cron_spine_health.sh >> /home/andy/argos/logs/spine_health.log 2>&1 # argos spine health 4h
#
# route_health.py reads LITELLM_KEY from the env and defaults LITELLM_BASE_URL to
# 127.0.0.1:4000 -- which is WRONG on garage (LiteLLM lives on UM780). So we set
# both explicitly here, reusing the established key file convention.
set -euo pipefail

KEYFILE="/home/andy/argos/.litellm-key"
LOG="/home/andy/argos/logs/spine_health.log"

KEY="$(cat "$KEYFILE" 2>/dev/null || true)"
if [ -z "$KEY" ]; then
    echo "ERROR: LiteLLM key empty or unreadable at $KEYFILE" >&2
    exit 1
fi

export LITELLM_KEY="$KEY"
export LITELLM_BASE_URL="http://192.168.4.10:4000"

cd /home/andy/argos

mkdir -p "$(dirname "$LOG")"
{
    echo "===== spine health refresh $(date -u +%Y-%m-%dT%H:%M:%S+00:00) ====="
    ./venv/bin/python route_health.py --only=spine:litellm
} >> "$LOG" 2>&1
