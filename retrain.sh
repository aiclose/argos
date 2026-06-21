#!/bin/bash
# Weekly retrain of argos quality_predictor.pkl
# Sunday 4am, snapshot prior model, retrain, hot-reload, ntfy delta
LOG="/home/andy/logs/argos-retrain.log"
DIR="/home/andy/argos"
HISTORY="$DIR/quality_predictor_history"
mkdir -p "$HISTORY" "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Snapshot current
if [ -f "$DIR/quality_predictor.pkl" ]; then
    cp "$DIR/quality_predictor.pkl" "$HISTORY/quality_predictor-$(date +%Y-%m-%d).pkl"
fi

# Read pre-train metadata
PRE_ACC=$($DIR/venv/bin/python -c "import pickle; m = pickle.load(open('$DIR/quality_predictor.pkl', 'rb')); print(round(m.get('training_accuracy', 0), 4))" 2>/dev/null || echo "0")
PRE_SIZE=$($DIR/venv/bin/python -c "import pickle; m = pickle.load(open('$DIR/quality_predictor.pkl', 'rb')); print(m.get('training_size', 0))" 2>/dev/null || echo "0")

# Retrain
$DIR/venv/bin/python $DIR/quality_trainer.py 2>&1 | tail -5 >> "$LOG"

# Hot-reload via /reload-quality-model
RELOAD=$(curl -s -X POST http://127.0.0.1:3020/reload-quality-model 2>/dev/null)

# Read post-train metadata
POST_ACC=$($DIR/venv/bin/python -c "import pickle; m = pickle.load(open('$DIR/quality_predictor.pkl', 'rb')); print(round(m.get('training_accuracy', 0), 4))" 2>/dev/null || echo "0")
POST_SIZE=$($DIR/venv/bin/python -c "import pickle; m = pickle.load(open('$DIR/quality_predictor.pkl', 'rb')); print(m.get('training_size', 0))" 2>/dev/null || echo "0")

# Telegram alert with delta
DELTA=$(echo "$POST_ACC - $PRE_ACC" | bc -l 2>/dev/null || echo "0")
MSG="🤖 Argos retrain weekly: training_size $PRE_SIZE → $POST_SIZE, accuracy $PRE_ACC → $POST_ACC (delta $DELTA)"
curl -sf -X POST "https://api.telegram.org/bot8260412478:AAFKKD0knEhEorQ08__ZDIlSJTmngJ-FakY/sendMessage" \
    -d "chat_id=8796667560" -d "text=$MSG" >/dev/null

# Rotate history (keep last 8)
ls -t "$HISTORY"/*.pkl 2>/dev/null | tail -n +9 | xargs rm -f 2>/dev/null

echo "[$(ts)] retrain done: $PRE_SIZE → $POST_SIZE" >> "$LOG"
