#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $1"; }

check_port() {
  lsof -ti:"$1" >/dev/null 2>&1
}

check_service() {
  local port=$1
  local path=$2
  curl -s -o /dev/null -w "%{http_code}" "http://localhost:${port}${path}" 2>/dev/null | grep -q "200"
}

kill_port() {
  local pid
  pid=$(lsof -ti:"$1" 2>/dev/null || true)
  if [ -n "$pid" ]; then
    log_warn "端口 $1 已被占用 (pid=$pid)，正在终止..."
    kill -9 $pid 2>/dev/null || true
    sleep 1
  fi
}

wait_for_port() {
  local port=$1
  local max_wait=${2:-10}
  local waited=0
  while ! check_port "$port"; do
    sleep 1
    waited=$((waited + 1))
    if [ $waited -ge $max_wait ]; then
      log_err "端口 $port 等待超时 (${max_wait}s)"
      return 1
    fi
  done
  return 0
}

ensure_service() {
  local name="$1"
  local port="$2"
  local health_path="$3"
  local start_cmd="$4"
  local log_file="$5"
  local wait_secs="${6:-10}"

  if check_port "$port" && check_service "$port" "$health_path"; then
    log_ok "$name (:$port) 已运行"
    return 0
  fi

  if check_port "$port"; then
    log_warn "端口 $port 被非预期进程占用，正在清理..."
    kill_port "$port"
  fi

  log_info "启动 $name (:$port)..."
  eval "$start_cmd"

  if wait_for_port "$port" "$wait_secs" && check_service "$port" "$health_path"; then
    log_ok "$name 启动成功"
    return 0
  elif check_port "$port"; then
    log_warn "$name 端口已监听但健康检查未通过，可能仍在初始化"
    return 0
  else
    log_err "$name 启动失败，查看日志: $log_file"
    return 1
  fi
}

echo ""
echo "🦞 OpenClaw Multi-Agent 一键启动脚本"
echo "======================================"
echo ""

if [ "$1" = "stop" ]; then
  log_info "停止所有服务..."
  kill_port 8082; log_ok "Hermes (:8082) 已停止"
  kill_port 3001; log_ok "Bridge (:3001) 已停止"
  kill_port 3000; log_ok "Custom Gateway (:3000) 已停止"
  kill_port 3005; log_ok "Official Gateway (:3005) 已停止"
  kill_port 8000; log_ok "Python Scheduler (:8000) 已停止"
  echo ""
  log_ok "所有服务已停止"
  exit 0
fi

if [ "$1" = "status" ]; then
  echo "服务状态:"
  for svc in "8082:/health:Hermes" "3000:/health:Custom Gateway" "3001:/health:Bridge" "3005:/health:Official Gateway" "8000:/health:Python Scheduler" "11434::Ollama"; do
    port="${svc%%:*}"
    rest="${svc#*:}"
    health_path="${rest%%:*}"
    name="${rest#*:}"
    if ! check_port "$port"; then
      log_err "$name (:$port) 未启动"
    elif [ -n "$health_path" ] && ! check_service "$port" "$health_path"; then
      log_warn "$name (:$port) 端口已占用但服务异常"
    else
      log_ok "$name (:$port) 运行中"
    fi
  done
  exit 0
fi

STARTED=0
FAILED=0

log_info "1/6 检查 Ollama..."
if check_port 11434; then
  log_ok "Ollama (:11434) 已运行"
else
  log_warn "Ollama 未启动，尝试启动..."
  nohup ollama serve &>/dev/null &
  if wait_for_port 11434 5; then
    log_ok "Ollama 启动成功"
    STARTED=$((STARTED + 1))
  else
    log_warn "Ollama 启动失败，本地模型将不可用"
    FAILED=$((FAILED + 1))
  fi
fi

log_info "2/6 启动 Custom Gateway (:3000)..."
if ensure_service "Custom Gateway" 3000 "/health" \
  "nohup node gateway/gateway.mjs &>/tmp/openclaw-gw.log &" \
  "/tmp/openclaw-gw.log" 8; then
  STARTED=$((STARTED + 1))
else
  FAILED=$((FAILED + 1))
fi

log_info "3/6 启动 Bridge (:3001)..."
if ensure_service "Bridge" 3001 "/health" \
  "nohup node bridge/orchestrator.mjs &>/tmp/openclaw-bridge.log &" \
  "/tmp/openclaw-bridge.log" 8; then
  STARTED=$((STARTED + 1))
else
  FAILED=$((FAILED + 1))
fi

log_info "4/6 启动 OpenClaw 官方 Gateway (:3005)..."
OPENCLAW_BIN=""
if [ -f "$HOME/MyWork/OpenClaw/openclaw/openclaw.mjs" ]; then
  OPENCLAW_BIN="$HOME/MyWork/OpenClaw/openclaw/openclaw.mjs"
elif command -v openclaw &>/dev/null; then
  OPENCLAW_BIN="$(which openclaw)"
fi
if [ -n "$OPENCLAW_BIN" ]; then
  if ensure_service "Official Gateway" 3005 "/health" \
    "nohup node \"$OPENCLAW_BIN\" gateway run --port 3005 --auth none --force &>/tmp/openclaw-official.log &" \
    "/tmp/openclaw-official.log" 15; then
    STARTED=$((STARTED + 1))
  else
    FAILED=$((FAILED + 1))
  fi
else
  log_warn "未找到 OpenClaw，跳过 Official Gateway (可通过 Dashboard 按钮启动)"
fi

log_info "5/6 启动 Python 调度器 (:8000)..."
if ensure_service "Python Scheduler" 8000 "/health" \
  "nohup ./venv/bin/python run.py &>/tmp/openclaw-scheduler.log &" \
  "/tmp/openclaw-scheduler.log" 10; then
  STARTED=$((STARTED + 1))
else
  FAILED=$((FAILED + 1))
fi

log_info "6/6 启动 Hermes 智能路由 (:8082)..."
HERMES_PYTHON="${HERMES_PYTHON:-./venv/bin/python}"
if [ ! -x "$HERMES_PYTHON" ]; then
  HERMES_PYTHON="$(which python3 2>/dev/null || which python 2>/dev/null)"
fi
if ensure_service "Hermes 智能路由" 8082 "/health" \
  "nohup $HERMES_PYTHON -m hermes.server &>/tmp/openclaw-hermes.log &" \
  "/tmp/openclaw-hermes.log" 10; then
  STARTED=$((STARTED + 1))
else
  FAILED=$((FAILED + 1))
fi

echo ""
echo "======================================"
if [ $FAILED -eq 0 ]; then
  echo -e "🚀 服务启动完成！(新启动: ${STARTED}个)"
else
  echo -e "⚠️  服务启动完成，${FAILED}个服务失败 (新启动: ${STARTED}个)"
fi
echo ""
echo "  Dashboard:    http://localhost:3001/static/dashboard.html"
echo "  Hermes:       http://localhost:8082/health"
echo "  Bridge API:   http://localhost:3001/health"
echo "  Gateway:      http://localhost:3000/health"
echo "  Official GW:  http://localhost:3005"
echo "  Scheduler:    http://localhost:8000"
echo ""
echo "  停止所有服务: $0 stop"
echo "  查看服务状态: $0 status"
echo ""
