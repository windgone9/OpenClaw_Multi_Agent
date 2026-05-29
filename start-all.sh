#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $1"; }

check_port() {
  lsof -ti:"$1" >/dev/null 2>&1
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

echo ""
echo "🦞 OpenClaw Multi-Agent 一键启动脚本"
echo "======================================"
echo ""

if [ "$1" = "stop" ]; then
  log_info "停止所有服务..."
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
  for svc in "3000:Custom Gateway" "3001:Bridge" "3005:Official Gateway" "8000:Python Scheduler" "11434:Ollama"; do
    port=${svc%%:*}
    name=${svc#*:}
    if check_port "$port"; then
      log_ok "$name (:$port) 运行中"
    else
      log_err "$name (:$port) 未启动"
    fi
  done
  exit 0
fi

STARTED=0
FAILED=0

log_info "1/5 检查 Ollama..."
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

log_info "2/5 启动 Custom Gateway (:3000)..."
if check_port 3000; then
  log_ok "Custom Gateway (:3000) 已运行"
else
  kill_port 3000
  nohup node gateway/gateway.mjs &>/tmp/openclaw-gw.log &
  if wait_for_port 3000 8; then
    log_ok "Custom Gateway 启动成功"
    STARTED=$((STARTED + 1))
  else
    log_err "Custom Gateway 启动失败，查看日志: /tmp/openclaw-gw.log"
    FAILED=$((FAILED + 1))
  fi
fi

log_info "3/5 启动 Bridge (:3001)..."
if check_port 3001; then
  log_ok "Bridge (:3001) 已运行"
else
  kill_port 3001
  nohup node bridge/orchestrator.mjs &>/tmp/openclaw-bridge.log &
  if wait_for_port 3001 8; then
    log_ok "Bridge 启动成功"
    STARTED=$((STARTED + 1))
  else
    log_err "Bridge 启动失败，查看日志: /tmp/openclaw-bridge.log"
    FAILED=$((FAILED + 1))
  fi
fi

log_info "4/5 启动 OpenClaw 官方 Gateway (:3005)..."
if check_port 3005; then
  log_ok "Official Gateway (:3005) 已运行"
else
  OPENCLAW_BIN=""
  if [ -f "$HOME/MyWork/OpenClaw/openclaw/openclaw.mjs" ]; then
    OPENCLAW_BIN="$HOME/MyWork/OpenClaw/openclaw/openclaw.mjs"
  elif command -v openclaw &>/dev/null; then
    OPENCLAW_BIN="$(which openclaw)"
  fi
  if [ -n "$OPENCLAW_BIN" ]; then
    kill_port 3005
    nohup node "$OPENCLAW_BIN" gateway run --port 3005 --auth none --force &>/tmp/openclaw-official.log &
    if wait_for_port 3005 15; then
      log_ok "Official Gateway 启动成功"
      STARTED=$((STARTED + 1))
    else
      log_warn "Official Gateway 启动超时，查看日志: /tmp/openclaw-official.log"
      FAILED=$((FAILED + 1))
    fi
  else
    log_warn "未找到 OpenClaw，跳过 Official Gateway (可通过 Dashboard 按钮启动)"
  fi
fi

log_info "5/5 启动 Python 调度器 (:8000)..."
if check_port 8000; then
  log_ok "Python Scheduler (:8000) 已运行"
else
  kill_port 8000
  nohup python scheduler/main.py &>/tmp/openclaw-scheduler.log &
  if wait_for_port 8000 10; then
    log_ok "Python Scheduler 启动成功"
    STARTED=$((STARTED + 1))
  else
    log_warn "Python Scheduler 启动超时，查看日志: /tmp/openclaw-scheduler.log"
    FAILED=$((FAILED + 1))
  fi
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
echo "  Bridge API:   http://localhost:3001/health"
echo "  Gateway:      http://localhost:3000/health"
echo "  Official GW:  http://localhost:3005"
echo "  Scheduler:    http://localhost:8000"
echo ""
echo "  停止所有服务: $0 stop"
echo "  查看服务状态: $0 status"
echo ""
