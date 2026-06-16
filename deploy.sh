#!/bin/bash
# ============================================================
# OpenClaw Multi-Agent 一键部署脚本
# 适用于全新机器，从零安装所有依赖并启动服务
#
# 用法:
#   chmod +x deploy.sh
#   ./deploy.sh                    # 交互式部署（推荐）
#   ./deploy.sh --non-interactive  # 非交互式（使用默认值或环境变量）
#   ./deploy.sh --skip-k8s         # 跳过 K8S 插件配置
#   ./deploy.sh --check            # 仅检查环境，不安装
#   ./deploy.sh --uninstall        # 卸载清理
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── 颜色与日志 ──────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok()      { echo -e "${GREEN}[ OK ]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()     { echo -e "${RED}[ERR]${NC} $1"; }
log_step()    { echo -e "\n${CYAN}${BOLD}━━━ $1 ━━━${NC}\n"; }
log_substep() { echo -e "  ${CYAN}▸${NC} $1"; }

# ── 参数解析 ────────────────────────────────────────────────
NON_INTERACTIVE=false
SKIP_K8S=false
CHECK_ONLY=false
UNINSTALL=false

for arg in "$@"; do
  case "$arg" in
    --non-interactive) NON_INTERACTIVE=true ;;
    --skip-k8s)        SKIP_K8S=true ;;
    --check)           CHECK_ONLY=true ;;
    --uninstall)       UNINSTALL=true ;;
    --help|-h)
      echo "用法: $0 [选项]"
      echo ""
      echo "选项:"
      echo "  --non-interactive  非交互式部署（使用默认值或环境变量）"
      echo "  --skip-k8s         跳过 K8S 插件配置"
      echo "  --check            仅检查环境，不安装"
      echo "  --uninstall        卸载清理"
      echo "  --help             显示帮助"
      exit 0
      ;;
    *)
      log_err "未知参数: $arg"
      exit 1
      ;;
  esac
done

# ── 卸载模式 ────────────────────────────────────────────────
if [ "$UNINSTALL" = true ]; then
  log_step "卸载 OpenClaw Multi-Agent"
  log_info "停止服务..."
  if [ -f "$SCRIPT_DIR/stop-all.sh" ]; then
    bash "$SCRIPT_DIR/stop-all.sh" force 2>/dev/null || true
  fi
  log_info "删除 Python 虚拟环境..."
  rm -rf "$SCRIPT_DIR/hermes-official-venv"
  log_info "删除 .env 文件..."
  rm -f "$SCRIPT_DIR/.env"
  log_info "删除 node_modules..."
  rm -rf "$SCRIPT_DIR/node_modules"
  log_ok "卸载完成（项目代码未删除，如需彻底请 rm -rf $SCRIPT_DIR）"
  exit 0
fi

# ── 系统检测 ────────────────────────────────────────────────
detect_os() {
  if [[ "$(uname)" == "Darwin" ]]; then
    echo "macos"
  elif [ -f /etc/os-release ]; then
    source /etc/os-release
    case "$ID" in
      ubuntu|debian) echo "debian" ;;
      centos|rhel|rocky|almalinux) echo "rhel" ;;
      *) echo "linux-unknown" ;;
    esac
  else
    echo "unknown"
  fi
}

OS=$(detect_os)
log_info "检测到操作系统: $OS"

# ── 工具函数 ────────────────────────────────────────────────
command_exists() {
  command -v "$1" &>/dev/null
}

version_gte() {
  # version_gte $actual $minimum → true if actual >= minimum
  local actual="$1" minimum="$2"
  [ "$(printf '%s\n' "$minimum" "$actual" | sort -V | head -n1)" = "$minimum" ]
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-Y}"
  if [ "$NON_INTERACTIVE" = true ]; then
    echo "$default"
    return
  fi
  local options
  if [ "$default" = "Y" ]; then
    options="[Y/n]"
  else
    options="[y/N]"
  fi
  while true; do
    echo -ne "  ${CYAN}?${NC} ${prompt} ${options} "
    read -r answer
    answer="${answer:-$default}"
    case "$answer" in
      [Yy]*) echo "Y"; return ;;
      [Nn]*) echo "N"; return ;;
    esac
  done
}

ask_input() {
  local prompt="$1"
  local default="$2"
  if [ "$NON_INTERACTIVE" = true ]; then
    echo "$default"
    return
  fi
  echo -ne "  ${CYAN}?${NC} ${prompt} [${default}]: "
  read -r answer
  echo "${answer:-$default}"
}

check_port() {
  lsof -ti:"$1" &>/dev/null 2>&1
}

wait_for_port() {
  local port="$1"
  local max_wait="${2:-15}"
  local waited=0
  while ! check_port "$port"; do
    sleep 1
    waited=$((waited + 1))
    if [ $waited -ge $max_wait ]; then
      return 1
    fi
  done
  return 0
}

# ── 统计 ────────────────────────────────────────────────────
STEPS_DONE=0
STEPS_FAILED=0
STEPS_SKIPPED=0

step_done()   { STEPS_DONE=$((STEPS_DONE + 1)); log_ok "$1"; }
step_skip()   { STEPS_SKIPPED=$((STEPS_SKIPPED + 1)); log_warn "$1 (跳过)"; }
step_fail()   { STEPS_FAILED=$((STEPS_FAILED + 1)); log_err "$1"; }

# ================================================================
#  Step 1: 检查系统环境
# ================================================================
log_step "Step 1/8: 检查系统环境"

check_python() {
  if command_exists python3; then
    local ver
    ver=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    if version_gte "$ver" "3.9"; then
      step_done "Python $ver 已安装"
      return 0
    else
      log_warn "Python $ver 版本过低 (需要 >= 3.9)"
      return 1
    fi
  fi
  return 1
}

check_node() {
  if command_exists node; then
    local ver
    ver=$(node -v | sed 's/v//')
    if version_gte "$ver" "22.0.0"; then
      step_done "Node.js $ver 已安装"
      return 0
    else
      log_warn "Node.js $ver 版本过低 (需要 >= 22.x)"
      return 1
    fi
  fi
  return 1
}

check_git() {
  if command_exists git; then
    step_done "Git 已安装"
    return 0
  fi
  return 1
}

check_curl() {
  if command_exists curl; then
    step_done "curl 已安装"
    return 0
  fi
  return 1
}

check_kubectl() {
  if command_exists kubectl; then
    step_done "kubectl 已安装"
    return 0
  fi
  return 1
}

PYTHON_OK=false
NODE_OK=false
GIT_OK=false
CURL_OK=false
KUBECTL_OK=false

check_python && PYTHON_OK=true
check_node   && NODE_OK=true
check_git    && GIT_OK=true
check_curl   && CURL_OK=true
check_kubectl && KUBECTL_OK=true

if [ "$CHECK_ONLY" = true ]; then
  log_step "环境检查结果"
  echo "  Python >= 3.9:  $([ "$PYTHON_OK" = true ] && echo 'OK' || echo 'MISSING')"
  echo "  Node.js >= 22:  $([ "$NODE_OK" = true ] && echo 'OK' || echo 'MISSING')"
  echo "  Git:            $([ "$GIT_OK" = true ] && echo 'OK' || echo 'MISSING')"
  echo "  curl:           $([ "$CURL_OK" = true ] && echo 'OK' || echo 'MISSING')"
  echo "  kubectl:        $([ "$KUBECTL_OK" = true ] && echo 'OK' || echo 'MISSING')"
  exit 0
fi

# ================================================================
#  Step 2: 安装系统依赖
# ================================================================
log_step "Step 2/8: 安装系统依赖"

install_system_deps() {
  case "$OS" in
    debian)
      log_info "更新 apt..."
      sudo apt update -qq

      if ! $GIT_OK || ! $CURL_OK; then
        log_substep "安装 git, curl, wget, build-essential..."
        sudo apt install -y git curl wget build-essential
      fi

      if ! $PYTHON_OK; then
        log_substep "安装 Python 3 + venv + pip..."
        sudo apt install -y python3 python3-pip python3-venv
        PYTHON_OK=true
      fi

      if ! $NODE_OK; then
        log_substep "安装 Node.js 22.x (via NodeSource)..."
        curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
        sudo apt install -y nodejs
        NODE_OK=true
      fi
      ;;
    rhel)
      if ! $GIT_OK || ! $CURL_OK; then
        log_substep "安装 git, curl, wget..."
        sudo yum groupinstall -y "Development Tools" 2>/dev/null || true
        sudo yum install -y git curl wget
      fi

      if ! $PYTHON_OK; then
        log_substep "安装 Python 3..."
        sudo yum install -y python3 python3-pip
        PYTHON_OK=true
      fi

      if ! $NODE_OK; then
        log_substep "安装 Node.js 22.x (via NodeSource)..."
        curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -
        sudo yum install -y nodejs
        NODE_OK=true
      fi
      ;;
    macos)
      if ! command_exists brew; then
        log_substep "安装 Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
      fi

      if ! $GIT_OK || ! $CURL_OK || ! $PYTHON_OK || ! $NODE_OK; then
        log_substep "安装缺失依赖..."
        [ ! $GIT_OK ]    && brew install git
        [ ! $CURL_OK ]   && brew install curl
        [ ! $PYTHON_OK ] && brew install python3
        [ ! $NODE_OK ]   && brew install node
        PYTHON_OK=true
        NODE_OK=true
      fi
      ;;
    *)
      log_err "不支持的操作系统: $OS"
      log_info "请手动安装: Python >= 3.9, Node.js >= 22, Git, curl"
      exit 1
      ;;
  esac
}

NEED_INSTALL=false
$PYTHON_OK && $NODE_OK && $GIT_OK && $CURL_OK || NEED_INSTALL=true

if $NEED_INSTALL; then
  if [ "$(ask_yes_no '检测到缺失依赖，是否自动安装？' 'Y')" = "Y" ]; then
    install_system_deps
    step_done "系统依赖安装完成"
  else
    step_fail "跳过系统依赖安装，后续步骤可能失败"
  fi
else
  step_skip "系统依赖已满足"
fi

# 验证关键工具
python3 --version
node --version

# ================================================================
#  Step 3: 获取项目代码
# ================================================================
log_step "Step 3/8: 获取项目代码"

PROJECT_DIR="$SCRIPT_DIR"
REPO_URL="https://github.com/windgone9/OpenClaw_Multi_Agent.git"

if [ -f "$PROJECT_DIR/hermes/server.py" ] && [ -f "$PROJECT_DIR/requirements.txt" ]; then
  step_skip "项目代码已存在: $PROJECT_DIR"
else
  log_info "项目代码不完整，需要重新克隆"
  PARENT_DIR=$(dirname "$PROJECT_DIR")
  if [ "$(basename "$PROJECT_DIR")" = "OpenClaw_Multi_Agent" ] && [ ! -f "$PROJECT_DIR/hermes/server.py" ]; then
    # 目录存在但代码不完整，可能是刚创建的空目录
    cd "$PARENT_DIR"
    rm -rf "$PROJECT_DIR"
    git clone "$REPO_URL"
    cd "$PROJECT_DIR"
  else
    cd /tmp
    git clone "$REPO_URL"
    log_info "项目已克隆到 /tmp/OpenClaw_Multi_Agent"
    log_info "请将项目移动到目标位置后重新运行此脚本"
    exit 0
  fi
  step_done "项目代码已克隆"
fi

# ================================================================
#  Step 4: 安装 Hermes Agent (Python)
# ================================================================
log_step "Step 4/8: 安装 Hermes Agent (Python 调度层)"

VENV_DIR="$PROJECT_DIR/hermes-official-venv"

if [ -f "$VENV_DIR/bin/python" ] && "$VENV_DIR/bin/python" -c "import fastapi" 2>/dev/null; then
  step_skip "Python 虚拟环境已存在且依赖已安装"
else
  log_substep "创建 Python 虚拟环境..."
  python3 -m venv "$VENV_DIR"

  log_substep "升级 pip..."
  "$VENV_DIR/bin/pip" install --upgrade pip -q

  log_substep "安装 Python 依赖..."
  "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" -q

  step_done "Hermes Agent 依赖安装完成"
fi

# 验证
"$VENV_DIR/bin/python" -c "import fastapi, uvicorn, httpx, pydantic, psutil, dotenv; print('OK')"

# ================================================================
#  Step 5: 安装 OpenClaw Gateway
# ================================================================
log_step "Step 5/8: 安装 OpenClaw Gateway"

OPENCLAW_INSTALLED=false

if command_exists openclaw; then
  step_skip "OpenClaw CLI 已安装: $(openclaw --version 2>/dev/null || echo 'unknown version')"
  OPENCLAW_INSTALLED=true
elif [ -f "$PROJECT_DIR/node_modules/.bin/openclaw" ]; then
  step_skip "OpenClaw 已在项目本地安装"
  OPENCLAW_INSTALLED=true
else
  # 尝试 npm 全局安装
  log_substep "通过 npm 安装 OpenClaw..."
  if npm install -g openclaw 2>/dev/null; then
    step_done "OpenClaw 已全局安装"
    OPENCLAW_INSTALLED=true
  else
    log_warn "全局安装失败，尝试项目本地安装..."
    cd "$PROJECT_DIR"
    if [ -f package.json ]; then
      npm install 2>/dev/null && OPENCLAW_INSTALLED=true
    fi
    if [ "$OPENCLAW_INSTALLED" = true ]; then
      step_done "OpenClaw 已在项目本地安装"
    else
      step_fail "OpenClaw 安装失败，请手动安装: npm install -g openclaw"
    fi
  fi
fi

# 安装 Node.js 项目依赖（Bridge/Gateway 等）
if [ -f "$PROJECT_DIR/package.json" ] && [ ! -d "$PROJECT_DIR/node_modules" ]; then
  log_substep "安装 Node.js 项目依赖..."
  cd "$PROJECT_DIR"
  npm install 2>/dev/null || log_warn "npm install 部分失败（非关键）"
fi

# ================================================================
#  Step 6: 配置环境变量
# ================================================================
log_step "Step 6/8: 配置环境变量"

ENV_FILE="$PROJECT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
  step_skip ".env 文件已存在"
else
  log_substep "从 .env.example 创建 .env..."
  cp "$PROJECT_DIR/.env.example" "$ENV_FILE"

  # 交互式配置 API Keys
  log_info "配置 API Keys（留空跳过，后续可手动编辑 .env）："

  MOONSHOT_KEY=$(ask_input "Moonshot API Key" "")
  if [ -n "$MOONSHOT_KEY" ]; then
    sed -i.bak "s/^MOONSHOT_API_KEY=.*/MOONSHOT_KEY=${MOONSHOT_KEY}/" "$ENV_FILE" 2>/dev/null || \
    sed -i '' "s/^MOONSHOT_API_KEY=.*/MOONSHOT_API_KEY=${MOONSHOT_KEY}/" "$ENV_FILE" 2>/dev/null || true
  fi

  DEEPSEEK_KEY=$(ask_input "DeepSeek API Key" "")
  if [ -n "$DEEPSEEK_KEY" ]; then
    sed -i.bak "s/^DEEPSEEK_API_KEY=.*/DEEPSEEK_API_KEY=${DEEPSEEK_KEY}/" "$ENV_FILE" 2>/dev/null || \
    sed -i '' "s/^DEEPSEEK_API_KEY=.*/DEEPSEEK_API_KEY=${DEEPSEEK_KEY}/" "$ENV_FILE" 2>/dev/null || true
  fi

  # 生成随机 OPENCLAW_TOKEN（用于 K8S 插件认证）
  K8S_TOKEN=$(openssl rand -hex 16 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(16))")
  if [ -n "$K8S_TOKEN" ]; then
    if grep -q "^OPENCLAW_TOKEN=" "$ENV_FILE"; then
      sed -i.bak "s/^OPENCLAW_TOKEN=.*/OPENCLAW_TOKEN=${K8S_TOKEN}/" "$ENV_FILE" 2>/dev/null || \
      sed -i '' "s/^OPENCLAW_TOKEN=.*/OPENCLAW_TOKEN=${K8S_TOKEN}/" "$ENV_FILE" 2>/dev/null || true
    else
      echo "OPENCLAW_TOKEN=${K8S_TOKEN}" >> "$ENV_FILE"
    fi
    log_info "已自动生成 OPENCLAW_TOKEN: ${K8S_TOKEN}"
    log_warn "请将此 Token 配置到 OpenClaw 的 openclaw.json 中"
  fi

  # 清理 sed 备份
  rm -f "$ENV_FILE.bak"

  # 保护 .env
  chmod 600 "$ENV_FILE"

  step_done ".env 配置完成"
fi

# 加载 .env
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

# ================================================================
#  Step 7: 配置 K8S AIWorkload 集成
# ================================================================
log_step "Step 7/8: 配置 K8S AIWorkload 集成"

if [ "$SKIP_K8S" = true ]; then
  step_skip "K8S 配置已跳过 (--skip-k8s)"
else
  # 检查 kubectl
  if ! $KUBECTL_OK; then
    if [ "$(ask_yes_no 'kubectl 未安装，是否安装？' 'Y')" = "Y" ]; then
      log_substep "安装 kubectl..."
      curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
      sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
      rm -f kubectl
      KUBECTL_OK=true
      step_done "kubectl 安装完成"
    else
      step_skip "kubectl 未安装，跳过 K8S 配置"
    fi
  fi

  if $KUBECTL_OK; then
    # 检查 K8S 集群连通性
    if kubectl cluster-info &>/dev/null; then
      step_done "K8S 集群连通"

      # 检查 AIWorkload CRD
      if kubectl get crd aiworkloads.ai.aischeduler.io &>/dev/null 2>&1; then
        step_skip "AIWorkload CRD 已安装"
      else
        if [ "$(ask_yes_no 'AIWorkload CRD 未安装，是否安装？' 'N')" = "Y" ]; then
          log_substep "安装 AIWorkload CRD..."
          kubectl apply -f https://raw.githubusercontent.com/aischeduler/aiworkload-operator/main/config/crd/bases/ai.aischeduler.io_aiworkloads.yaml 2>/dev/null || \
            log_warn "CRD 安装失败，请手动安装"
        fi
      fi

      # 创建租户命名空间
      DEFAULT_NS=$(ask_input "默认 K8S 命名空间" "default")
      if ! kubectl get namespace "$DEFAULT_NS" &>/dev/null 2>&1; then
        if [ "$(ask_yes_no "命名空间 '$DEFAULT_NS' 不存在，是否创建？" 'Y')" = "Y" ]; then
          kubectl create namespace "$DEFAULT_NS"
          step_done "命名空间 '$DEFAULT_NS' 已创建"
        fi
      fi
    else
      log_warn "K8S 集群不可达，跳过 K8S 配置"
      log_info "请确保 kubeconfig 已正确配置后手动执行 K8S 相关步骤"
    fi
  fi

  # 配置 OpenClaw K8S 插件
  OPENCLAW_CONFIG="$HOME/.openclaw/openclaw.json"
  if [ -f "$OPENCLAW_CONFIG" ]; then
    if grep -q "k8s-integration" "$OPENCLAW_CONFIG"; then
      step_skip "OpenClaw K8S 插件已配置"
    else
      log_warn "OpenClaw 配置中未找到 k8s-integration 插件"
      log_info "请手动在 $OPENCLAW_CONFIG 中添加 K8S 插件配置"
      log_info "参考: docs/deployment-guide.md 第 6.2 节"
    fi
  else
    log_info "OpenClaw 配置文件尚未创建（将在首次 openclaw onboard 时生成）"
    log_info "配置 K8S 插件时请参考: docs/deployment-guide.md 第 6.2 节"
  fi
fi

# ================================================================
#  Step 8: 启动服务并验证
# ================================================================
log_step "Step 8/8: 启动服务并验证"

# ── 启动 Ollama（可选）──
INSTALL_OLLAMA=$(ask_yes_no "是否安装并启动 Ollama（本地模型推理）？" "N")
if [ "$INSTALL_OLLAMA" = "Y" ]; then
  if ! command_exists ollama; then
    log_substep "安装 Ollama..."
    case "$OS" in
      macos) brew install ollama ;;
      debian|rhel) curl -fsSL https://ollama.com/install.sh | sh ;;
    esac
  fi

  if ! check_port 11434; then
    log_substep "启动 Ollama..."
    nohup ollama serve &>/dev/null &
    if wait_for_port 11434 10; then
      step_done "Ollama 启动成功 (:11434)"

      # 拉取默认模型
      PULL_MODEL=$(ask_yes_no "是否拉取默认模型 qwen2.5:3b？" "Y")
      if [ "$PULL_MODEL" = "Y" ]; then
        log_substep "拉取 qwen2.5:3b（可能需要几分钟）..."
        ollama pull qwen2.5:3b || log_warn "模型拉取失败，可稍后手动执行: ollama pull qwen2.5:3b"
      fi
    else
      step_fail "Ollama 启动失败"
    fi
  else
    step_skip "Ollama 已运行 (:11434)"
  fi
else
  step_skip "Ollama 未安装（使用云端模型）"
fi

# ── 启动 OpenClaw Gateway ──
if ! check_port 3005; then
  log_substep "启动 OpenClaw Gateway (:3005)..."
  if command_exists openclaw; then
    nohup openclaw gateway run --port 3005 --force --allow-unconfigured &>/tmp/openclaw-gw-deploy.log &
  elif [ -f "$PROJECT_DIR/node_modules/.bin/openclaw" ]; then
    nohup "$PROJECT_DIR/node_modules/.bin/openclaw" gateway run --port 3005 --force --allow-unconfigured &>/tmp/openclaw-gw-deploy.log &
  else
    log_warn "未找到 openclaw 命令，跳过 Gateway 启动"
  fi

  if wait_for_port 3005 20; then
    step_done "OpenClaw Gateway 启动成功 (:3005)"
  else
    log_warn "OpenClaw Gateway 启动超时，查看日志: /tmp/openclaw-gw-deploy.log"
  fi
else
  step_skip "OpenClaw Gateway 已运行 (:3005)"
fi

# ── 启动 Hermes Agent ──
if ! check_port 8082; then
  log_substep "启动 Hermes Agent (:8082)..."
  nohup "$VENV_DIR/bin/python" -m hermes.server &>/tmp/hermes-deploy.log &

  if wait_for_port 8082 15; then
    step_done "Hermes Agent 启动成功 (:8082)"
  else
    log_warn "Hermes Agent 启动超时，查看日志: /tmp/hermes-deploy.log"
  fi
else
  step_skip "Hermes Agent 已运行 (:8082)"
fi

# ── 验证服务 ──
log_substep "验证服务状态..."

echo ""
echo "  服务状态:"
echo "  ┌─────────────────────────────────────────────────────────┐"

# Hermes
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8082/health 2>/dev/null | grep -q "200"; then
  echo "  │  Hermes Agent  :8082   ✅ 运行中                       │"
else
  echo "  │  Hermes Agent  :8082   ❌ 不可达                       │"
fi

# OpenClaw GW
if curl -s -o /dev/null -w "%{http_code}" http://localhost:3005/health 2>/dev/null | grep -q "200"; then
  echo "  │  OpenClaw GW   :3005   ✅ 运行中                       │"
else
  echo "  │  OpenClaw GW   :3005   ❌ 不可达                       │"
fi

# Ollama
if check_port 11434; then
  echo "  │  Ollama        :11434  ✅ 运行中                       │"
else
  echo "  │  Ollama        :11434  ⬚ 未启动                       │"
fi

echo "  └─────────────────────────────────────────────────────────┘"

# ── 功能测试 ──
echo ""
log_substep "快速功能测试..."

CHAT_RESULT=$(curl -s -X POST 'http://localhost:8082/queue/submit-sync?timeout=30' \
  -H 'Content-Type: application/json' \
  -d '{"appid":"deploy-test","type":"chat","priority":3,"prompt":"1+1=?"}' 2>/dev/null || echo '{"status":"failed"}')

CHAT_STATUS=$(echo "$CHAT_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
if [ "$CHAT_STATUS" = "success" ]; then
  step_done "Chat 请求测试通过"
else
  log_warn "Chat 请求测试未通过 (status=$CHAT_STATUS)，可能需要配置模型 API Key"
fi

# ================================================================
#  部署完成
# ================================================================
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}${BOLD}  部署完成！${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  完成步骤: ${STEPS_DONE}    跳过: ${STEPS_SKIPPED}    失败: ${STEPS_FAILED}"
echo ""
echo "  访问地址:"
echo "    Dashboard:       http://localhost:8082/static/dashboard.html"
echo "    Hermes API:      http://localhost:8082/health"
echo "    OpenClaw GW:     http://localhost:3005/health"
echo ""
echo "  常用命令:"
echo "    启动服务:  ./start-all.sh"
echo "    停止服务:  ./stop-all.sh"
echo "    查看日志:  tail -f /tmp/hermes-deploy.log"
echo "    编辑配置:  vim .env"
echo ""
echo "  K8S AIWorkload 测试:"
echo "    curl -X POST 'http://localhost:8082/k8s/workloads?appid=test' \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"taskType\":\"batch-inference\",\"intent\":{...}}'"
echo ""

if [ "$STEPS_FAILED" -gt 0 ]; then
  echo -e "  ${YELLOW}⚠ 有 ${STEPS_FAILED} 个步骤失败，请查看上方日志排查${NC}"
  echo "  详细文档: docs/deployment-guide.md"
  echo ""
fi

# 提示未完成事项
if [ ! -f "$HOME/.openclaw/openclaw.json" ] || ! grep -q "k8s-integration" "$HOME/.openclaw/openclaw.json" 2>/dev/null; then
  echo -e "  ${YELLOW}⚠ K8S 插件尚未配置到 OpenClaw${NC}"
  echo "  请参考 docs/deployment-guide.md 第 6 节完成 K8S 集成配置"
  echo ""
fi
