#!/bin/bash
# Argos predictions retention - keep last 10000, prune monthly
LOG="/home/andy/logs/argos-predictions-prune.log"
DB="/home/andy/argos/argos.db"
mkdir -p "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

BEFORE=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM predictions' 2>/dev/null || echo 0)
sqlite3 "$DB" 'DELETE FROM predictions WHERE rowid NOT IN (SELECT rowid FROM predictions ORDER BY decision_id DESC LIMIT 10000)' 2>/dev/null
sqlite3 "$DB" 'VACUUM' 2>/dev/null
AFTER=$(sqlite3 "$DB" 'SELECT COUNT(*) FROM predictions' 2>/dev/null || echo 0)
echo "[$(ts)] retention: $BEFORE -> $AFTER (kept last 10000)" >> "$LOG"
