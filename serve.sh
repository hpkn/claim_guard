#!/usr/bin/env bash
# ClaimGuard on the server: check the setup, run the denial queue, serve the review page.
#
#   ./serve.sh              same as "start"
#   ./serve.sh check        python, streamlit, model endpoint, model installed, unit tests
#   ./serve.sh start        check, then run new cases in the background and start the review page
#   ./serve.sh run [args]   run cases in the foreground (default --new; --all, or --ids A-1001..A-1020)
#   ./serve.sh ui           start only the review page in the background
#   ./serve.sh status       what is running, how many cases have results
#   ./serve.sh logs         follow the run and review page logs
#   ./serve.sh stop         stop the run and the review page
#   ./serve.sh reset        every case back to new, clear out/ (asks first; audit log is kept)
#
# Options (before or after the command):
#   --lan                   serve the review page on 0.0.0.0 so another laptop can open it (demo only)
#   --port N                review page port (default 8080)
#   --test                  also run the unit tests during the check (skipped by default)
#
# Settings come from .env (CG_LLM_BASE_URL, CG_LLM_MODEL, CG_WORKERS, ...).

set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
SELF="$ROOT/$(basename "$0")"
STATE="$ROOT/.run"
mkdir -p "$STATE" out

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi
BASE_URL="${CG_LLM_BASE_URL:-http://localhost:11434/v1}"
MODEL="${CG_LLM_MODEL:-}"
PORT=8080
ADDRESS=127.0.0.1
PY="${PYTHON:-python3}"
RUN_TESTS=0

CMD=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --lan) ADDRESS=0.0.0.0 ;;
    --port) PORT="$2"; shift ;;
    --test) RUN_TESTS=1 ;;
    *) if [ -z "$CMD" ]; then CMD="$1"; else ARGS+=("$1"); fi ;;
  esac
  shift
done
CMD="${CMD:-start}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '  \033[31mfail\033[0m %s\n' "$*"; exit 1; }

alive() { [ -f "$STATE/$1.pid" ] && kill -0 "$(cat "$STATE/$1.pid")" 2>/dev/null; }

check() {
  say "Checking the setup"
  command -v "$PY" >/dev/null || die "python3 not found"
  ok "$("$PY" --version 2>&1)"

  if "$PY" -c "import streamlit" 2>/dev/null; then
    ok "streamlit $("$PY" -c 'import streamlit; print(streamlit.__version__)')"
  else
    warn "streamlit missing; installing it for this user"
    "$PY" -m pip install --user streamlit \
      || die "pip install failed. Try: python3 -m venv .venv && . .venv/bin/activate && pip install streamlit, then PYTHON=.venv/bin/python ./serve.sh"
    ok "streamlit installed"
  fi

  models="$(curl -s --max-time 5 "$BASE_URL/models" || true)"
  if [ -z "$models" ] && command -v ollama >/dev/null; then
    warn "no model endpoint at $BASE_URL; starting ollama serve"
    nohup ollama serve >"$STATE/ollama.log" 2>&1 &
    for _ in $(seq 1 30); do
      sleep 1
      models="$(curl -s --max-time 2 "$BASE_URL/models" || true)"
      [ -n "$models" ] && break
    done
  fi
  [ -n "$models" ] || die "no model endpoint at $BASE_URL (start Ollama: ollama serve, or sudo systemctl start ollama)"
  ok "model endpoint $BASE_URL"

  if [ -n "$MODEL" ]; then
    if printf '%s' "$models" | grep -q "\"$MODEL\""; then
      ok "model $MODEL is available"
    elif command -v ollama >/dev/null; then
      warn "model $MODEL not found; pulling it"
      ollama pull "$MODEL" || die "could not pull $MODEL"
    else
      die "model $MODEL is not served at $BASE_URL"
    fi
  else
    warn "CG_LLM_MODEL not set; the engine will use the first model the endpoint lists"
  fi

  if [ ! -f data/cases/A-1001.json ]; then
    warn "no cases yet; generating the 60 synthetic cases"
    "$PY" make_data.py --n 60 --seed 42
  fi
  ok "$(ls data/cases/*.json | wc -l | tr -d ' ') cases in data/cases"

  if [ "$RUN_TESTS" = 1 ]; then
    "$PY" -m unittest discover tests >"$STATE/tests.log" 2>&1 \
      || die "unit tests failed (see $STATE/tests.log)"
    ok "unit tests pass"
  fi
}

start_ui() {
  if alive ui; then
    ok "review page already running (pid $(cat "$STATE/ui.pid"))"
    return
  fi
  say "Starting the review page on $ADDRESS:$PORT"
  nohup "$PY" -m streamlit run review_app.py --server.headless true \
    --server.address "$ADDRESS" --server.port "$PORT" >"$STATE/ui.log" 2>&1 &
  echo $! >"$STATE/ui.pid"
  sleep 3
  alive ui || die "review page did not start (see $STATE/ui.log)"
  ok "review page: http://$( [ "$ADDRESS" = 0.0.0.0 ] && hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1 ):$PORT"
  [ "$ADDRESS" = 0.0.0.0 ] && warn "--lan is for the demo only: anyone on this network can open the page"
  return 0
}

start_run() {
  if alive run; then
    ok "a run is already going (pid $(cat "$STATE/run.pid"))"
    return
  fi
  local run_args=("${ARGS[@]:-}")
  [ -z "${run_args[0]}" ] && run_args=(--new)
  [ "${run_args[0]}" = "--all" ] && run_args=()
  say "Running cases in the background: cg.py run ${run_args[*]}"
  nohup "$PY" -u cg.py run ${run_args[@]+"${run_args[@]}"} >"$STATE/run.log" 2>&1 &
  echo $! >"$STATE/run.pid"

  # make sure the backend really started: wait for its first line
  for _ in $(seq 1 20); do
    grep -qE "^run run-|nothing to do|cannot reach|unknown claim" "$STATE/run.log" 2>/dev/null && break
    alive run || break
    sleep 1
  done
  if grep -q "nothing to do" "$STATE/run.log"; then
    warn "no cases with status new. Rerun everything: ./serve.sh start --all, or ./serve.sh reset first"
  elif grep -q "^run run-" "$STATE/run.log"; then
    ok "$(grep -m1 "^run run-" "$STATE/run.log")"
    ok "log: $STATE/run.log (results appear on the review page as each case finishes)"
  else
    tail -5 "$STATE/run.log" 2>/dev/null
    alive run && warn "backend is still starting; watch ./serve.sh logs" || die "backend did not start (see $STATE/run.log)"
  fi
  return 0
}

stop_one() {
  if alive "$1"; then
    kill "$(cat "$STATE/$1.pid")" && ok "stopped $1"
  else
    ok "$1 not running"
  fi
  rm -f "$STATE/$1.pid"
}

status() {
  say "Status"
  alive run && ok "run: going (pid $(cat "$STATE/run.pid"))" || ok "run: not running"
  alive ui && ok "review page: up on port $PORT (pid $(cat "$STATE/ui.pid"))" || ok "review page: not running"
  local done total
  done=$(ls out/results/*.json 2>/dev/null | wc -l | tr -d ' ')
  total=$(ls data/cases/*.json 2>/dev/null | wc -l | tr -d ' ')
  ok "cases with results: $done / $total"
  [ -f out/digest.txt ] && { echo; cat out/digest.txt; }
  return 0
}

case "$CMD" in
  check)  check ;;
  start)  check; start_run; start_ui; echo; say "Stop with ./serve.sh stop, watch with ./serve.sh logs" ;;
  run)    check
          run_args=("${ARGS[@]:-}"); [ -z "${run_args[0]}" ] && run_args=(--new)
          [ "${run_args[0]}" = "--all" ] && run_args=()
          "$PY" cg.py run ${run_args[@]+"${run_args[@]}"} ;;
  ui)     start_ui ;;
  status) status ;;
  logs)   touch "$STATE/run.log" "$STATE/ui.log"; tail -f "$STATE/run.log" "$STATE/ui.log" ;;
  stop)   stop_one run; stop_one ui ;;
  reset)  read -r -p "Set every case back to new and clear out/ (audit log kept)? [y/N] " a
          [ "$a" = y ] || [ "$a" = Y ] || { echo "cancelled"; exit 0; }
          stop_one run; "$PY" cg.py reset ;;
  *)      sed -n '2,20p' "$SELF"; exit 2 ;;
esac
