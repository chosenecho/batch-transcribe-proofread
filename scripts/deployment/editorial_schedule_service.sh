#!/usr/bin/env bash
# Independent editorial scheduling; restart without unloading the GPU workers.
set -euo pipefail
BASE=/root/autodl-tmp
PROJECT=$BASE/batch-transcribe-proofread
OPS=$BASE/batch-processing-ops/ai-schedule
PIDFILE=$OPS/controller.pid
live() {
    [[ -f "$PIDFILE" ]] || return 1
    PID=$(cat "$PIDFILE")
    [[ "$PID" =~ ^[0-9]+$ ]] || return 1
    kill -0 "$PID" 2>/dev/null || return 1
    [[ "$(tr '\0' ' ' < /proc/"$PID"/cmdline)" == *"$OPS/run_editorial_schedule.py"* ]]
}
case ${1:-status} in
start)
    if live; then echo "AI scheduler already running: $PID"; exit 0; fi
    mkdir -p "$OPS"
    rm -f "$OPS/STOP"
    nohup "$BASE/envs/batch-transcribe-runtime/bin/python" -u "$OPS/run_editorial_schedule.py" \
        --project-root "$PROJECT" --archive-root "$PROJECT/archive" --ops-root "$OPS" \
        --policy "$OPS/policy.json" --bin /usr/local/bin/batch-transcribe-prod \
        > "$OPS/controller.log" 2>&1 < /dev/null &
    echo $! > "$PIDFILE"
    sleep 3
    live || { tail -30 "$OPS/controller.log"; exit 1; }
    echo "AI scheduler started: $PID"
    ;;
stop)
    touch "$OPS/STOP"
    if live; then kill -TERM "$PID"; echo "AI drain requested: $PID"; else echo 'AI scheduler stopped'; fi
    ;;
status)
    if live; then echo "AI scheduler running: $PID"; else echo 'AI scheduler stopped'; fi
    if [[ -f "$OPS/state.json" ]]; then cat "$OPS/state.json"; fi
    ;;
*) echo 'Usage: bili-asr-ai-service start|stop|status'; exit 2 ;;
esac
