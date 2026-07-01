#!/usr/bin/env bash
# ============================================
# pin-to-node.sh — 把 OpenClaw deployment 钉到指定节点
# ============================================
# 多节点集群 + 镜像只在某节点 docker store 时 (runtime=docker), 用本脚本把
# hermes/proxy/stream-service/funasr 钉到镜像所在节点 (+ control-plane toleration),
# 让 IfNotPresent 在该节点找到镜像。minio/nginx 用公共镜像, 不需钉。
#
# 用法:
#   bash k8s/server/pin-to-node.sh k8s-master02-ceph-01
#   bash k8s/server/pin-to-node.sh                     # 默认 k8s-master02-ceph-01
# ============================================
set -e
# 默认本机 hostname 小写化 (kubelet 注册节点名时小写化, 避免大小写不匹配)
NODE="${1:-$(hostname | tr 'A-Z' 'a-z')}"
NS="${NAMESPACE:-openclaw}"

# 校验节点存在
if ! kubectl get node "$NODE" >/dev/null 2>&1; then
  echo "错误: 节点 '${NODE}' 不存在 (hostname=$(hostname))。用法: $0 <节点名>  (kubectl get nodes 看真名)" >&2
  exit 1
fi

# patch 文件 (heredoc 在脚本文件内, EOF 在行首, 不受终端粘贴影响)
cat > /tmp/pin-node.yaml <<EOF
spec:
  template:
    spec:
      nodeSelector:
        kubernetes.io/hostname: ${NODE}
      tolerations:
        - key: node-role.kubernetes.io/control-plane
          effect: NoSchedule
EOF

echo "钉 hermes/proxy/stream-service/funasr -> ${NODE}"
for d in hermes proxy stream-service funasr; do
  kubectl -n "${NS}" patch deploy "${d}" --type=strategic --patch-file=/tmp/pin-node.yaml 2>&1 | sed 's/^/    /'
done

echo "=== pods (等 Running) ==="
kubectl get pods -n "${NS}" -o wide
