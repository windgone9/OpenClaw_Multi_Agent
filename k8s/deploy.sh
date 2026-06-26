#!/usr/bin/env bash
# ============================================
# OpenClaw Multi-Agent — Kind 一键部署脚本
#
# 使用方式:
#   ./k8s/deploy.sh              # 完整部署
#   ./k8s/deploy.sh --skip-build # 跳过镜像构建
#   ./k8s/deploy.sh --destroy    # 销毁集群
#   ./k8s/deploy.sh --status     # 查看状态
#   ./k8s/deploy.sh --pull-model # 拉取 Ollama 模型
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CLUSTER_NAME="openclaw"
NAMESPACE="openclaw"
K8S_DIR="$SCRIPT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ==================== 前置检查 ====================
check_prerequisites() {
    info "检查前置依赖..."
    # 尝试多个路径查找 kind
    if ! command -v kind >/dev/null 2>&1; then
        if [ -x /opt/homebrew/bin/kind ]; then
            alias kind=/opt/homebrew/bin/kind 2>/dev/null || export PATH="/opt/homebrew/bin:$PATH"
        else
            error "kind 未安装，请运行: brew install kind"
        fi
    fi
    if ! command -v kubectl >/dev/null 2>&1; then
        if [ -x /opt/homebrew/bin/kubectl ]; then
            export PATH="/opt/homebrew/bin:$PATH"
        else
            error "kubectl 未安装，请运行: brew install kubectl"
        fi
    fi
    command -v docker >/dev/null 2>&1  || error "docker 未安装，请运行: brew install docker"
    ok "前置依赖检查通过"
}

# ==================== 构建镜像 ====================
build_images() {
    info "构建 Docker 镜像..."

    cd "$PROJECT_DIR/docker"

    # LiteLLM
    info "构建 openclaw-litellm..."
    docker build -t openclaw-litellm:latest -f Dockerfile.litellm . 2>&1 | tail -1

    # Hermes (仅 K8S workloads)
    info "构建 openclaw-hermes..."
    docker build -t openclaw-hermes:latest -f Dockerfile.hermes .. 2>&1 | tail -1

    # Proxy Pod (预处理 + API 网关)
    info "构建 openclaw-proxy..."
    docker build -t openclaw-proxy:latest -f Dockerfile.proxy .. 2>&1 | tail -1

    # Stream Service
    info "构建 openclaw-stream..."
    docker build -t openclaw-stream:latest -f Dockerfile.stream .. 2>&1 | tail -1

    # FunASR
    info "构建 openclaw-funasr..."
    docker build -t openclaw-funasr:latest -f Dockerfile.funasr .. 2>&1 | tail -1

    cd "$PROJECT_DIR"
    ok "镜像构建完成"
}

# ==================== 创建 Kind 集群 ====================
create_cluster() {
    if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
        warn "Kind 集群 '$CLUSTER_NAME' 已存在，跳过创建"
    else
        info "创建 Kind 集群 '$CLUSTER_NAME'..."
        kind create cluster --config "$K8S_DIR/kind-config.yaml"
        ok "Kind 集群创建完成"
    fi

    # 设置 kubectl 上下文
    kubectl config set-context --current --namespace="$NAMESPACE" 2>/dev/null || true
}

# ==================== 加载镜像到 Kind ====================
load_images() {
    info "加载自定义镜像到 Kind 集群..."
    local custom_images=(
        "openclaw-litellm:latest"
        "openclaw-hermes:latest"
        "openclaw-proxy:latest"
        "openclaw-stream:latest"
        "openclaw-funasr:latest"
    )
    for img in "${custom_images[@]}"; do
        info "  加载 $img..."
        kind load docker-image "$img" --name "$CLUSTER_NAME" 2>&1 | tail -1
    done

    info "加载第三方镜像到 Kind 集群..."
    local third_party_images=(
        "ollama/ollama:latest"
        "postgres:16-alpine"
        "minio/minio:latest"
        "nginx:1.25-alpine"
    )
    for img in "${third_party_images[@]}"; do
        info "  加载 $img..."
        # kind load 对多架构镜像可能失败，使用 docker save + ctr import 方式
        local tar_path="/tmp/openclaw-$(echo "$img" | tr '/:' '_').tar"
        docker save "$img" -o "$tar_path" 2>/dev/null || { warn "  镜像 $img 不在本地，跳过（K8s 将尝试从 registry 拉取）"; continue; }
        docker exec --privileged -i "${CLUSTER_NAME}-control-plane" \
            bash -c "ctr --namespace=k8s.io images import /dev/stdin" < "$tar_path" 2>&1 | tail -1
        rm -f "$tar_path"
    done

    ok "镜像加载完成"
}

# ==================== 部署 K8s 资源 ====================
deploy_resources() {
    info "部署 K8s 资源..."

    # 按顺序部署（依赖关系）
    local manifests=(
        "00-namespace.yaml"
        "01-secrets.yaml"
        "02-configmaps.yaml"
        "03-storage.yaml"
        "04-ollama.yaml"
        "05-litellm-db.yaml"
        "06-litellm.yaml"
        "07-hermes.yaml"
        "08-stream-service.yaml"
        "09-funasr.yaml"
        "10-minio.yaml"
        "11-nginx.yaml"
        "12-proxy.yaml"
    )

    for manifest in "${manifests[@]}"; do
        info "  应用 $manifest..."
        kubectl apply -f "$K8S_DIR/$manifest" 2>&1 | sed 's/^/    /'
    done

    ok "K8s 资源部署完成"
}

# ==================== 等待服务就绪 ====================
wait_for_ready() {
    info "等待服务就绪（可能需要几分钟）..."

    # Ollama 需要先就绪，其他服务依赖它
    info "  等待 Ollama..."
    kubectl rollout status deployment/ollama -n "$NAMESPACE" --timeout=120s 2>&1 | tail -1 || warn "Ollama 尚未就绪（可能需要手动拉取模型）"

    info "  等待 LiteLLM DB..."
    kubectl rollout status deployment/litellm-db -n "$NAMESPACE" --timeout=60s 2>&1 | tail -1 || true

    info "  等待 LiteLLM..."
    kubectl rollout status deployment/litellm -n "$NAMESPACE" --timeout=180s 2>&1 | tail -1 || true

    info "  等待 Proxy Pod..."
    kubectl rollout status deployment/proxy -n "$NAMESPACE" --timeout=120s 2>&1 | tail -1 || true

    info "  等待 Hermes (K8S only)..."
    kubectl rollout status deployment/hermes -n "$NAMESPACE" --timeout=120s 2>&1 | tail -1 || true

    info "  等待 Stream Service..."
    kubectl rollout status deployment/stream-service -n "$NAMESPACE" --timeout=120s 2>&1 | tail -1 || true

    info "  等待 FunASR（启动较慢，约 2-3 分钟）..."
    kubectl rollout status deployment/funasr -n "$NAMESPACE" --timeout=300s 2>&1 | tail -1 || true

    info "  等待 MinIO..."
    kubectl rollout status deployment/minio -n "$NAMESPACE" --timeout=60s 2>&1 | tail -1 || true

    info "  等待 Nginx..."
    kubectl rollout status deployment/nginx -n "$NAMESPACE" --timeout=60s 2>&1 | tail -1 || true

    ok "服务就绪检查完成"
}

# ==================== 拉取 Ollama 模型 ====================
pull_model() {
    local model="${1:-qwen2.5:7b}"
    info "拉取 Ollama 模型: $model"

    # 获取 Ollama Pod 名称
    local pod
    pod=$(kubectl get pods -n "$NAMESPACE" -l app=ollama -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

    if [ -z "$pod" ]; then
        error "未找到 Ollama Pod，请先部署服务"
    fi

    info "  在 Pod $pod 中拉取模型..."
    kubectl exec -n "$NAMESPACE" "$pod" -- ollama pull "$model"

    ok "模型 $model 拉取完成"
}

# ==================== 显示状态 ====================
show_status() {
    echo ""
    info "===== OpenClaw K8s 集群状态 ====="
    echo ""
    kubectl get nodes 2>/dev/null || true
    echo ""
    kubectl get all -n "$NAMESPACE" 2>/dev/null || true
    echo ""
    info "===== 访问地址 ====="
    echo "  Dashboard:     http://localhost:8090/new_dashboard.html"
    echo "  LiteLLM UI:    http://localhost:8090/ui/"
    echo "  MinIO Console: http://localhost:9002"
    echo "  Ollama API:    http://localhost:11435"
    echo ""
}

# ==================== 销毁集群 ====================
destroy_cluster() {
    warn "即将销毁 Kind 集群 '$CLUSTER_NAME'..."
    read -p "确认销毁? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        kind delete cluster --name "$CLUSTER_NAME"
        ok "集群已销毁"
    else
        info "取消销毁"
    fi
}

# ==================== 主流程 ====================
main() {
    local skip_build=false

    case "${1:-}" in
        --skip-build)
            skip_build=true
            ;;
        --destroy)
            destroy_cluster
            exit 0
            ;;
        --status)
            show_status
            exit 0
            ;;
        --pull-model)
            pull_model "${2:-qwen2.5:7b}"
            exit 0
            ;;
        --help|-h)
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  (无)            完整部署（构建镜像 + 创建集群 + 部署服务）"
            echo "  --skip-build    跳过镜像构建，仅部署 K8s 资源"
            echo "  --destroy       销毁 Kind 集群"
            echo "  --status        查看集群状态"
            echo "  --pull-model    拉取 Ollama 模型（默认 qwen2.5:7b）"
            echo "  --help          显示帮助"
            exit 0
            ;;
    esac

    echo ""
    info "===== OpenClaw Multi-Agent — Kind 部署 ====="
    echo ""

    check_prerequisites

    if [ "$skip_build" = false ]; then
        build_images
    else
        warn "跳过镜像构建"
    fi

    create_cluster
    load_images
    deploy_resources
    wait_for_ready

    echo ""
    ok "===== 部署完成! ====="
    show_status

    info "下一步:"
    echo "  1. 拉取 Ollama 模型:  $0 --pull-model qwen2.5:7b"
    echo "  2. 访问 Dashboard:     http://localhost:8090/new_dashboard.html"
    echo "  3. 查看集群状态:       $0 --status"
}

main "$@"
