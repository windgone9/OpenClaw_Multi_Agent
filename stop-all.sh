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

check_service() {
  local port=$1
  local path=$2
  curl -s -o /dev/null -w "%{http_code}" "http://localhost:${port}${path}" 2>/dev/null | grep -q "200"
}

kill_port() {
  local port=$1
  local pids
  pids=$(lsof -ti:"$port" 2>/dev/null || true)
  if [ -z "$pids" ]; then
    return 0
  fi
  for pid in $pids; do
    local cmd
    cmd=$(ps -p "$pid" -o command= 2>/dev/null || echo "unknown")
    log_info "终止进程 pid=$pid ($cmd)"
    kill "$pid" 2>/dev/null || true
  done
  sleep 2
  pids=$(lsof -ti:"$port" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    log_warn "进程未响应 SIGTERM，强制终止..."
    for pid in $pids; do
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1
  fi
}

verify_stopped() {
  local name="$1"
  local port="$2"
  local health_path="$3"

  if ! check_port "$port"; then
    log_ok "$name (:$port) 已停止"
    return 0
  fi

  if [ -n "$health_path" ] && ! check_service "$port" "$health_path"; then
    log_warn "$name (:$port) 端口仍被占用但服务已停止，清理残留进程..."
    kill_port "$port"
    if ! check_port "$port"; then
      log_ok "$name (:$port) 残留进程已清理"
      return 0
    else
      log_err "$name (:$port) 端口仍被占用，请手动检查: lsof -i :$port"
      return 1
    fi
  fi

  log_err "$name (:$port) 停止失败，服务仍在运行"
  return 1
}

stop_service() {
  local name="$1"
  local port="$2"
  local health_path="$3"

  if ! check_port "$port"; then
    log_ok "$name (:$port) 未运行"
    return 0
  fi

  log_info "停止 $name (:$port)..."
  kill_port "$port"
  verify_stopped "$name" "$port" "$health_path"
}

clean_temp_files() {
  local cleaned=0
  for f in /tmp/openclaw-gw.log /tmp/openclaw-bridge.log /tmp/openclaw-official.log /tmp/openclaw-scheduler.log /tmp/openclaw-hermes.log; do
    if [ -f "$f" ]; then
      rm -f "$f"
      cleaned=$((cleaned + 1))
    fi
  done
  if [ $cleaned -gt 0 ]; then
    log_info "已清理 ${cleaned} 个临时日志文件"
  fi
}

echo ""
echo "🦞 OpenClaw Multi-Agent 服务停止脚本"
echo "======================================"
echo ""

if [ "$1" = "force" ]; then
  log_warn "强制停止模式：跳过健康检查，直接终止所有进程"
  FORCE_MODE=1
else
  FORCE_MODE=0
fi

STOPPED=0
FAILED=0
SKIPPED=0

SERVICES=(
  "8082:/health:Hermes 智能路由"
  "8000:/health:Python 调度器"
  "3005:/health:Official Gateway"
  "3001:/health:Bridge"
  "3000:/health:Custom Gateway"
)

echo "按依赖顺序停止服务（从上层到下层）:"
echo ""

for svc in "${SERVICES[@]}"; do
  port="${svc%%:*}"
  rest="${svc#*:}"
  health_path="${rest%%:*}"
  name="${rest#*:}"

  if [ $FORCE_MODE -eq 1 ]; then
    if check_port "$port"; then
      log_info "强制终止 $name (:$port)..."
      pids=$(lsof -ti:"$port" 2>/dev/null || true)
      for pid in $pids; do
        kill -9 "$pid" 2>/dev/null || true
      done
      sleep 1
      if ! check_port "$port"; then
        log_ok "$name (:$port) 已强制停止"
        STOPPED=$((STOPPED + 1))
      else
        log_err "$name (:$port) 强制停止失败"
        FAILED=$((FAILED + 1))
      fi
    else
      log_ok "$name (:$port) 未运行"
      SKIPPED=$((SKIPPED + 1))
    fi
  else
    if stop_service "$name" "$port" "$health_path"; then
      if check_port "$port" 2>/dev/null; then
        SKIPPED=$((SKIPPED + 1))
      else
        STOPPED=$((STOPPED + 1))
      fi
    else
      FAILED=$((FAILED + 1))
    fi
  fi
done

echo ""
log_info "检查 Ollama..."
if check_port 11434; then
  read -p "是否停止 Ollama? [y/N] " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    kill_port 11434
    if ! check_port 11434; then
      log_ok "Ollama 已停止"
      STOPPED=$((STOPPED + 1))
    else
      log_err "Ollama 停止失败"
      FAILED=$((FAILED + 1))
    fi
  else
    log_ok "Ollama 保持运行"
    SKIPPED=$((SKIPPED + 1))
  fi
else
  log_ok "Ollama 未运行"
  SKIPPED=$((SKIPPED + 1))
fi

echo ""
echo "======================================"
if [ $FAILED -eq 0 ]; then
  echo -e "🛑 服务已全部停止 (停止: ${STOPPED}个, 跳过: ${SKIPPED}个)"
else
  echo -e "⚠️  停止完成，${FAILED}个服务失败 (停止: ${STOPPED}个, 跳过: ${SKIPPED}个)"
  echo ""
  echo "  排查命令:"
  for svc in "${SERVICES[@]}"; do
    port="${svc%%:*}"
    if check_port "$port"; then
      echo "    lsof -i :$port"
    fi
  done
fi

echo ""
if [ "$1" = "clean" ] || [ "$2" = "clean" ]; then
  clean_temp_files
else
  echo "  清理临时文件: $0 clean"
fi
echo "  强制停止所有: $0 force"
echo "  查看服务状态: ./start-all.sh status"
echo ""
