# OpenClaw Multi-Agent 安装部署运行指南

本文档指导在一台全新机器上从零部署 OpenClaw Multi-Agent 智能调度系统，包含 Hermes Agent、OpenClaw 官方 Gateway、K8S AIWorkload 集成的完整安装和运行步骤。

---

## 目录

1. [系统要求](#1-系统要求)
2. [安装基础依赖](#2-安装基础依赖)
3. [获取项目代码](#3-获取项目代码)
4. [安装 Hermes Agent（Python 调度层）](#4-安装-hermes-agentpython-调度层)
5. [安装 OpenClaw 官方 Gateway](#5-安装-openclaw-官方-gateway)
6. [配置 K8S AIWorkload 集成](#6-配置-k8s-aiworkload-集成)
7. [配置环境变量](#7-配置环境变量)
8. [启动服务](#8-启动服务)
9. [验证部署](#9-验证部署)
10. [K8S AIWorkload 端到端测试](#10-k8s-aiworkload-端到端测试)
11. [生产环境部署建议](#11-生产环境部署建议)
12. [常见问题排查](#12-常见问题排查)

---

## 1. 系统要求

### 硬件要求

| 组件 | 最低配置 | 推荐配置 |
|------|----------|----------|
| CPU | 2 核 | 4 核+ |
| 内存 | 4 GB | 8 GB+ |
| 磁盘 | 10 GB | 50 GB+ |
| GPU | 无（可选，用于本地模型推理） | NVIDIA GPU 8GB+ VRAM |

### 软件要求

| 软件 | 版本要求 | 说明 |
|------|----------|------|
| 操作系统 | Ubuntu 22.04+ / macOS 12+ / CentOS 8+ | 推荐 Ubuntu 22.04 LTS |
| Python | >= 3.9 | Hermes Agent 运行环境 |
| Node.js | >= 22.x | OpenClaw Gateway 运行环境 |
| Git | >= 2.30 | 代码拉取 |
| curl | 任意 | 接口测试验证 |
| K8S 集群 | >= 1.28 | 已部署，kubectl 可访问 |

### 网络要求

- 本机可访问 K8S API Server
- 本机可访问外网（用于拉取 npm/pip 包和调用云端模型 API）
- 以下端口可用：`8082`（Hermes）、`3005`（OpenClaw GW）、`11434`（Ollama，可选）

---

## 2. 安装基础依赖

### 2.1 Ubuntu / Debian

```bash
# 更新系统
sudo apt update && sudo apt upgrade -y

# 安装基础工具
sudo apt install -y git curl wget build-essential

# ── 安装 Python 3 ──
sudo apt install -y python3 python3-pip python3-venv
python3 --version  # 确认 >= 3.9

# ── 安装 Node.js 22.x ──
# 方式一：使用 NodeSource（推荐）
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs

# 方式二：使用 nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash
source ~/.bashrc
nvm install 22

node --version   # 确认 v22.x.x
npm --version    # 确认 npm 可用
```

### 2.2 macOS

```bash
# 安装 Homebrew（如未安装）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 安装基础工具
brew install git curl python3 node

python3 --version  # 确认 >= 3.9
node --version     # 确认 >= 22.x
```

### 2.3 CentOS / RHEL

```bash
# 安装基础工具
sudo yum groupinstall -y "Development Tools"
sudo yum install -y git curl wget

# 安装 Python 3
sudo yum install -y python3 python3-pip
python3 --version

# 安装 Node.js 22.x
curl -fsSL https://rpm.nodesource.com/setup_22.x | sudo bash -
sudo yum install -y nodejs
node --version
```

---

## 3. 获取项目代码

```bash
# 克隆项目
git clone https://github.com/windgone9/OpenClaw_Multi_Agent.git
cd OpenClaw_Multi_Agent

# 查看项目结构
ls -la
```

---

## 4. 安装 Hermes Agent（Python 调度层）

Hermes Agent 是系统的核心调度层，负责请求队列管理、路由决策和分发。

### 4.1 创建 Python 虚拟环境

```bash
# 在项目根目录创建虚拟环境
python3 -m venv hermes-official-venv

# 激活虚拟环境
source hermes-official-venv/bin/activate

# 升级 pip
pip install --upgrade pip
```

### 4.2 安装 Python 依赖

```bash
# 安装项目依赖
pip install -r requirements.txt
```

当前 `requirements.txt` 包含：

```
fastapi>=0.100.0
uvicorn>=0.20.0
httpx>=0.24.0
pydantic>=2.0.0
psutil>=5.9.0
python-dotenv>=1.0.0
```

### 4.3 验证 Hermes 可启动

```bash
# 快速验证（启动后 Ctrl+C 退出）
python -m hermes.server
# 应看到：Uvicorn running on http://0.0.0.0:8082
```

---

## 5. 安装 OpenClaw 官方 Gateway

OpenClaw Gateway 是 AI Agent 网关，Hermes 通过它向 K8S 提交 AIWorkload。

### 5.1 安装方式一：npm 全局安装（推荐）

```bash
# 全局安装 OpenClaw
npm install -g openclaw

# 验证安装
openclaw --version

# 初始化配置（首次安装）
openclaw onboard
```

`openclaw onboard` 会引导完成：
- 选择 AI 模型提供商
- 配置 API 密钥
- 设置基本参数

### 5.2 安装方式二：一键安装脚本

```bash
# macOS / Linux
curl -fsSL https://openclaw.ai/install.sh | bash

# 初始化
openclaw onboard
```

### 5.3 安装方式三：项目本地安装

如果无法全局安装（权限限制等），可在项目内本地安装：

```bash
cd OpenClaw_Multi_Agent

# 安装 Node.js 依赖（package.json 中已包含 openclaw）
npm install

# 使用本地 openclaw
npx openclaw --version
```

### 5.4 配置 OpenClaw Gateway

OpenClaw 配置文件位于 `~/.openclaw/openclaw.json`（全局）或项目目录下的 `openclaw.json`。

**最小可用配置**（项目已自带 `openclaw.json`，通常无需修改）：

```json5
{
  "agent": {
    "model": {
      "primary": "ollama/qwen2.5:3b",
      "fallbacks": ["moonshot/kimi-k2.6", "deepseek/deepseek-chat"]
    }
  },
  "gateway": {
    "port": 3005,
    "host": "0.0.0.0",
    "auth": {
      "enabled": false
    }
  }
}
```

### 5.5 验证 Gateway 可启动

```bash
# 前台启动（调试用，Ctrl+C 退出）
openclaw gateway run --port 3005 --force --allow-unconfigured

# 或使用后台模式
openclaw gateway start --port 3005

# 验证
curl http://localhost:3005/health
```

---

## 6. 配置 K8S AIWorkload 集成

K8S 集群已部署，需要安装 OpenClaw K8S 插件并配置租户命名空间。

### 6.1 安装 K8S AIWorkload CRD 和 Operator

在 K8S 集群上执行：

```bash
# 确认 kubectl 可访问集群
kubectl cluster-info
kubectl get nodes

# 安装 AIWorkload CRD（如果尚未安装）
# 具体安装方式取决于 AIWorkload Operator 的部署方式
# 示例：使用 kubectl apply
kubectl apply -f https://raw.githubusercontent.com/aischeduler/aiworkload-operator/main/config/crd/bases/ai.aischeduler.io_aiworkloads.yaml

# 验证 CRD 已注册
kubectl get crd | grep aiworkload
```

### 6.2 安装 OpenClaw K8S 集成插件

K8S 集成插件（`k8s-integration`）需要注册到 OpenClaw Gateway 中。在 OpenClaw 配置文件 `openclaw.json` 中添加插件配置：

```json5
{
  // ... 现有配置 ...

  "plugins": {
    "entries": {
      "k8s-integration": {
        "enabled": true,
        "config": {
          "secret": "your-shared-secret-here",  // Bearer Token 认证密钥
          "kubeconfig": "",  // 留空则使用集群内 ServiceAccount
          "defaultNamespace": "default",
          "tenantNamespace": {
            "template": "tenant-{{id}}",
            "overrides": {
              "team-a": "team-a-workloads",
              "team-b": "team-b-workloads"
            }
          }
        }
      }
    }
  }
}
```

**配置说明**：

| 字段 | 说明 |
|------|------|
| `secret` | Bearer Token 认证密钥，Hermes 侧的 `OPENCLAW_TOKEN` 需与此一致 |
| `kubeconfig` | K8S 集群访问凭证，集群内部署留空即可 |
| `defaultNamespace` | 默认命名空间 |
| `tenantNamespace.template` | 租户命名空间模板，`{{id}}` 替换为 tenant.id |
| `tenantNamespace.overrides` | 特定租户的命名空间映射 |

### 6.3 创建租户命名空间

K8S 插件不会自动创建命名空间，需要集群管理员预先创建：

```bash
# 创建默认命名空间
kubectl create namespace default

# 创建租户命名空间（根据 tenantNamespace.overrides 配置）
kubectl create namespace team-a-workloads
kubectl create namespace team-b-workloads

# 验证
kubectl get namespaces
```

### 6.4 配置 K8S 插件的 RBAC（集群内部署时）

如果 OpenClaw Gateway 部署在 K8S 集群内，需要为插件配置 RBAC 权限：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: openclaw-k8s-plugin
  namespace: openclaw
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: openclaw-k8s-plugin
rules:
  - apiGroups: ["ai.aischeduler.io"]
    resources: ["aiworkloads"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["ai.aischeduler.io"]
    resources: ["aiworkloads/status"]
    verbs: ["get", "watch"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "delete"]
  - apiGroups: [""]
    resources: ["namespaces"]
    verbs: ["get", "list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: openclaw-k8s-plugin
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: openclaw-k8s-plugin
subjects:
  - kind: ServiceAccount
    name: openclaw-k8s-plugin
    namespace: openclaw
```

```bash
kubectl apply -f openclaw-k8s-plugin-rbac.yaml
```

### 6.5 重启 Gateway 使插件生效

```bash
# 重启 OpenClaw Gateway
openclaw gateway restart

# 或前台模式
openclaw gateway run --port 3005 --force --allow-unconfigured
```

### 6.6 验证 K8S 插件已注册

```bash
# 测试 K8S 插件端点（应返回 401 而非 404）
curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:3005/plugins/k8s/v1/workloads

# 带 Token 测试（应返回 200 或 400，而非 404）
curl -s -w "\nHTTP_CODE: %{http_code}\n" \
  -H "Authorization: Bearer your-shared-secret-here" \
  http://localhost:3005/plugins/k8s/v1/workloads
```

- 返回 `404`：插件未注册，检查 `openclaw.json` 配置和 Gateway 日志
- 返回 `401`：插件已注册但未认证，说明配置正确
- 返回 `400` 或 `200`：插件正常工作

---

## 7. 配置环境变量

### 7.1 创建 .env 文件

```bash
cd OpenClaw_Multi_Agent
cp .env.example .env
```

### 7.2 编辑 .env 文件

```bash
vim .env  # 或使用其他编辑器
```

**必填项**：

```bash
# ── API Keys（至少配置一个云端模型 Key）──
MOONSHOT_API_KEY=sk-your-moonshot-api-key
DEEPSEEK_API_KEY=sk-your-deepseek-api-key

# ── OpenClaw Gateway 认证 ──
# 必须与 openclaw.json 中 plugins.entries.k8s-integration.config.secret 一致
OPENCLAW_TOKEN=your-shared-secret-here
```

**K8S 集成配置**：

```bash
# OpenClaw Gateway 地址（K8S 插件所在网关）
OPENCLAW_OFFICIAL_GATEWAY_URL=http://127.0.0.1:3005

# K8S 插件网关地址（默认复用 OFFICIAL_GATEWAY_URL，可独立配置）
# 如果 K8S 插件部署在集群内，可设置为集群内地址
# OPENCLAW_K8S_GATEWAY_URL=http://openclaw-gateway.openclaw.svc.cluster.local:18789
```

**可选项**：

```bash
# 服务端口
HERMES_PORT=8082

# 路由配置
DEFAULT_ROUTE_MODE=smart
SMART_ROUTER_THRESHOLD=40

# 限流
RATE_LIMIT_RPM=60
MAX_RETRIES=2
DEFAULT_TIMEOUT_MS=30000
```

### 7.3 保护 .env 文件

```bash
# 设置文件权限（仅所有者可读写）
chmod 600 .env

# 确保 .env 不被 git 追踪
grep -q ".env" .gitignore 2>/dev/null || echo ".env" >> .gitignore
```

---

## 8. 启动服务

### 8.1 方式一：一键启动（推荐）

```bash
cd OpenClaw_Multi_Agent

# 赋予执行权限
chmod +x start-all.sh stop-all.sh

# 启动所有服务
./start-all.sh
```

启动脚本会自动按顺序启动：
1. Ollama（本地模型，可选）
2. OpenClaw Official Gateway（:3005）
3. Hermes Agent（:8082）

### 8.2 方式二：手动逐个启动

**终端 1：启动 Ollama（可选，本地模型推理）**

```bash
# 安装 Ollama（如未安装）
# macOS: brew install ollama
# Linux: curl -fsSL https://ollama.com/install.sh | sh

# 启动 Ollama 服务
ollama serve

# 拉取模型（另开终端）
ollama pull qwen2.5:3b
```

**终端 2：启动 OpenClaw Gateway**

```bash
# 方式一：使用 openclaw CLI
openclaw gateway run --port 3005 --force --allow-unconfigured

# 方式二：后台守护进程
openclaw gateway start --port 3005

# 方式三：使用项目本地安装
cd OpenClaw_Multi_Agent
npx openclaw gateway run --port 3005 --force --allow-unconfigured
```

**终端 3：启动 Hermes Agent**

```bash
cd OpenClaw_Multi_Agent

# 激活虚拟环境
source hermes-official-venv/bin/activate

# 启动 Hermes
python -m hermes.server
```

### 8.3 使用 systemd 管理服务（生产环境推荐）

**Hermes Agent 服务文件**：`/etc/systemd/system/hermes-agent.service`

```ini
[Unit]
Description=Hermes Agent - AI Request Scheduler
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/path/to/OpenClaw_Multi_Agent
ExecStart=/path/to/OpenClaw_Multi_Agent/hermes-official-venv/bin/python -m hermes.server
Restart=on-failure
RestartSec=5
Environment=PATH=/path/to/OpenClaw_Multi_Agent/hermes-official-venv/bin:/usr/bin
EnvironmentFile=/path/to/OpenClaw_Multi_Agent/.env

[Install]
WantedBy=multi-user.target
```

```bash
# 启用并启动
sudo systemctl daemon-reload
sudo systemctl enable hermes-agent
sudo systemctl start hermes-agent

# 查看状态
sudo systemctl status hermes-agent

# 查看日志
sudo journalctl -u hermes-agent -f
```

**OpenClaw Gateway 服务文件**：`/etc/systemd/system/openclaw-gateway.service`

```ini
[Unit]
Description=OpenClaw Gateway
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/your-user
ExecStart=/usr/bin/openclaw gateway run --port 3005 --force --allow-unconfigured
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable openclaw-gateway
sudo systemctl start openclaw-gateway
sudo systemctl status openclaw-gateway
```

---

## 9. 验证部署

### 9.1 健康检查

```bash
# Hermes Agent
curl http://localhost:8082/health

# OpenClaw Gateway
curl http://localhost:3005/health

# Ollama（如已安装）
curl http://localhost:11434/api/tags
```

### 9.2 基础功能测试

```bash
# 普通对话请求（应路由到 direct_local 或 gateway）
curl -X POST 'http://localhost:8082/queue/submit-sync?timeout=60' \
  -H 'Content-Type: application/json' \
  -d '{"appid":"test","type":"chat","priority":3,"prompt":"你好，1+1等于几？"}'

# 预期：返回 JSON，status=success，routing.route_path 为 direct_local 或 gateway
```

### 9.3 Dashboard 验证

打开浏览器访问：http://localhost:8082/static/dashboard.html

- 确认 Dashboard 页面正常加载
- 确认系统状态面板显示各服务状态
- 确认请求类型下拉框包含 `k8s_workload` 选项
- 确认批量测试按钮区包含 `☸ K8S工作负载` 按钮

---

## 10. K8S AIWorkload 端到端测试

### 10.1 测试 K8S 插件连通性

```bash
# 测试 OpenClaw K8S 插件端点（带认证）
curl -s -w "\nHTTP_CODE: %{http_code}\n" \
  -H "Authorization: Bearer $OPENCLAW_TOKEN" \
  -H "Content-Type: application/json" \
  -X POST http://localhost:3005/plugins/k8s/v1/workloads \
  -d '{
    "requestId": "test-001",
    "tenant": {"id": "team-a"},
    "taskType": "batch-inference",
    "intent": {
      "batchInference": {
        "model": {"name": "resnet50", "framework": "pytorch"},
        "input": {"uri": "s3://datasets/test/"},
        "output": {"uri": "s3://results/test/"},
        "scale": {"items": 100, "batchSize": 32}
      }
    }
  }'
```

预期返回（成功）：
```json
{
  "ok": true,
  "payload": {
    "workloadId": "wl-test-001-xxxxx",
    "namespace": "team-a-workloads",
    "status": "Pending",
    "aiworkloadRef": { ... }
  },
  "duplicate": false
}
```

### 10.2 通过 Hermes 独立 API 测试

```bash
# 提交 AIWorkload
curl -X POST 'http://localhost:8082/k8s/workloads?appid=test-team' \
  -H 'Content-Type: application/json' \
  -d '{
    "taskType": "batch-inference",
    "intent": {
      "batchInference": {
        "model": {"name": "resnet50", "framework": "pytorch"},
        "input": {"uri": "s3://datasets/test/"},
        "output": {"uri": "s3://results/test/"},
        "scale": {"items": 100, "batchSize": 32}
      }
    }
  }'

# 预期：HTTP 202，返回含 k8s_workload 字段的 JSON
```

### 10.3 通过 Hermes 队列接口测试

```bash
# 队列方式提交 K8S 请求
curl -X POST 'http://localhost:8082/queue/submit-sync?timeout=30' \
  -H 'Content-Type: application/json' \
  -d '{
    "appid":"test",
    "type":"k8s_workload",
    "priority":3,
    "prompt":"提交批量推理任务",
    "k8s_workload":{
      "taskType":"batch-inference",
      "intent":{
        "batchInference":{
          "model":{"name":"resnet50","framework":"pytorch"},
          "input":{"uri":"s3://datasets/test/"},
          "output":{"uri":"s3://results/test/"},
          "scale":{"items":100,"batchSize":32}
        }
      }
    }
  }'

# 预期：routing.route_path=k8s_gateway, routing.agent_decision=k8s_fast_path
```

### 10.4 查询和删除工作负载

```bash
# 查询状态（替换为实际的 workloadId）
curl 'http://localhost:8082/k8s/workloads/wl-test-001-xxxxx'

# 删除
curl -X DELETE 'http://localhost:8082/k8s/workloads/wl-test-001-xxxxx'
```

### 10.5 Dashboard K8S 批量测试

1. 打开 http://localhost:8082/static/dashboard.html
2. 点击 `☸ K8S工作负载` 按钮
3. 观察结果卡片中是否显示 K8S 工作负载信息（Workload ID、状态、命名空间）

---

## 11. 生产环境部署建议

### 11.1 架构建议

```
                    ┌──────────────┐
                    │  Nginx/LB    │
                    │  :443/:80    │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
    ┌─────────▼──┐  ┌──────▼─────┐  ┌──▼──────────┐
    │ Hermes     │  │ OpenClaw   │  │ Ollama      │
    │ Agent      │  │ Gateway    │  │ (可选)      │
    │ :8082      │  │ :3005      │  │ :11434      │
    └─────┬──────┘  └──────┬─────┘  └─────────────┘
          │                │
          │    ┌───────────▼───────────┐
          │    │  K8S Cluster          │
          │    │  ┌─────────────────┐  │
          └───▶│  │ AIWorkload CRD │  │
               │  │ AIWorkload Op. │  │
               │  │ tenant ns      │  │
               │  └─────────────────┘  │
               └───────────────────────┘
```

### 11.2 安全配置

1. **启用 Gateway 认证**：`openclaw.json` 中设置 `gateway.auth.enabled: true`
2. **配置 OPENCLAW_TOKEN**：确保 Hermes 和 OpenClaw 使用相同的共享密钥
3. **使用 HTTPS**：通过 Nginx 反向代理配置 TLS
4. **限制网络访问**：使用防火墙或 K8S NetworkPolicy 限制端口访问
5. **保护 .env 文件**：`chmod 600 .env`，不要提交到版本控制

### 11.3 Nginx 反向代理配置示例

```nginx
upstream hermes {
    server 127.0.0.1:8082;
}

upstream openclaw_gw {
    server 127.0.0.1:3005;
}

server {
    listen 443 ssl http2;
    server_name your-domain.com;

    ssl_certificate     /etc/ssl/certs/your-cert.pem;
    ssl_certificate_key /etc/ssl/private/your-key.pem;

    # Hermes Agent API
    location / {
        proxy_pass http://hermes;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }

    # OpenClaw Gateway（仅内部访问，可选对外暴露）
    location /openclaw/ {
        proxy_pass http://openclaw_gw/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### 11.4 日志管理

Hermes Agent 日志输出到 stdout，可通过 systemd journal 或重定向到文件：

```bash
# systemd 方式
sudo journalctl -u hermes-agent -f

# 手动启动时重定向
python -m hermes.server 2>&1 | tee /var/log/hermes-agent.log
```

K8S 相关日志标签（方便 grep 过滤）：

```
[K8S API]     — Hermes API 端点日志
[K8SGateway]  — dispatch_worker K8S 转发日志
[K8S Callback]— K8S 状态回调日志
[K8S Dispatch]— K8S 请求路由拦截日志
```

### 11.5 监控指标

```bash
# Hermes 系统状态
curl http://localhost:8082/stats

# 自适应权重状态
curl http://localhost:8082/openclaw/adaptive/state
```

---

## 12. 常见问题排查

### Q1: `pip install -r requirements.txt` 失败

```bash
# 使用国内镜像
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 或
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
```

### Q2: `npm install` / `npm install -g openclaw` 失败

```bash
# 使用国内镜像
npm config set registry https://registry.npmmirror.com

# 重新安装
npm install -g openclaw
```

### Q3: Hermes 启动报 `ModuleNotFoundError: No module named 'hermes'`

```bash
# 确保在项目根目录启动
cd /path/to/OpenClaw_Multi_Agent

# 确保虚拟环境已激活
source hermes-official-venv/bin/activate

# 使用模块方式启动
python -m hermes.server
```

### Q4: OpenClaw Gateway 启动报 `EADDRINUSE: address already in use :::3005`

```bash
# 查找占用进程
lsof -i :3005

# 终止占用进程
kill $(lsof -ti:3005)

# 或使用其他端口
openclaw gateway run --port 3006 --force
# 同时修改 .env 中的 OPENCLAW_OFFICIAL_GATEWAY_URL
```

### Q5: K8S AIWorkload 提交返回 502 K8S_GATEWAY_ERROR

**排查步骤**：

```bash
# 1. 确认 OpenClaw Gateway 运行中
curl http://localhost:3005/health

# 2. 确认 K8S 插件端点可达
curl -s -o /dev/null -w "%{http_code}" \
  http://localhost:3005/plugins/k8s/v1/workloads
# 返回 404 = 插件未注册
# 返回 401 = 插件已注册但未认证（正常）

# 3. 确认 OPENCLAW_TOKEN 配置正确
# .env 中的 OPENCLAW_TOKEN 必须与 openclaw.json 中
# plugins.entries.k8s-integration.config.secret 一致

# 4. 查看 Hermes 日志
grep "K8SGateway" /var/log/hermes-agent.log
# 或
sudo journalctl -u hermes-agent | grep "K8S"

# 5. 查看 OpenClaw Gateway 日志
openclaw gateway logs
```

### Q6: K8S 插件返回 400 namespace_not_found

```bash
# 检查租户命名空间是否已创建
kubectl get namespaces

# 创建缺失的命名空间
kubectl create namespace team-a-workloads

# 检查 openclaw.json 中 tenantNamespace 配置是否正确
```

### Q7: K8S 插件返回 401 Unauthorized

```bash
# 确认 Token 一致
# Hermes 侧：
grep OPENCLAW_TOKEN .env

# OpenClaw 侧：
cat ~/.openclaw/openclaw.json | grep -A5 "k8s-integration"

# 两边的 secret 值必须完全一致
```

### Q8: Ollama 模型不可用，但不想安装 Ollama

可以跳过 Ollama，使用云端模型。修改 `openclaw.json`：

```json5
{
  "agent": {
    "model": {
      "primary": "moonshot/kimi-k2.6",  // 改为云端模型
      "fallbacks": ["deepseek/deepseek-chat"]
    }
  }
}
```

### Q9: 端口全部被占用

```bash
# 一键停止所有服务
./stop-all.sh

# 或手动清理
kill $(lsof -ti:8082)  # Hermes
kill $(lsof -ti:3005)  # OpenClaw GW
kill $(lsof -ti:11434) # Ollama
```

### Q10: 如何查看详细的 K8S 请求日志

Hermes 已内置详细的 K8S 日志打印，日志级别说明：

| 日志标签 | 级别 | 内容 |
|----------|------|------|
| `[K8S Dispatch]` | INFO | K8S 请求路由拦截 |
| `[K8SGateway]` | INFO | K8S Gateway 转发（请求/响应/错误） |
| `[K8SGateway]` | DEBUG | 完整 payload、HTTP 响应头 |
| `[K8S API]` | INFO | K8S API 端点请求/响应 |
| `[K8S Callback]` | INFO | K8S 状态回调接收 |

```bash
# 过滤 K8S 相关日志
sudo journalctl -u hermes-agent -f | grep -E "\[K8S"
```

---

## 附录 A：环境变量完整参考

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `MOONSHOT_API_KEY` | - | 推荐 | Moonshot API Key |
| `DEEPSEEK_API_KEY` | - | 推荐 | DeepSeek API Key |
| `OPENCLAW_TOKEN` | - | K8S 时必填 | OpenClaw Gateway 认证 Token |
| `OPENCLAW_API_KEY` | - | 否 | OpenClaw API Key |
| `OPENCLAW_K8S_GATEWAY_URL` | 同 OFFICIAL_GW_URL | 否 | K8S 插件网关地址 |
| `OPENCLAW_OFFICIAL_GATEWAY_URL` | `http://127.0.0.1:3005` | 否 | 官方 Gateway 地址 |
| `HERMES_PORT` | 8082 | 否 | Hermes Agent 端口 |
| `GATEWAY_PORT` | 3000 | 否 | Custom Gateway 端口 |
| `BRIDGE_PORT` | 3001 | 否 | Bridge 端口 |
| `USE_OPENCLAW_GATEWAY` | true | 否 | 是否启用 Gateway 路由 |
| `DEFAULT_ROUTE_MODE` | smart | 否 | 默认路由模式 |
| `SMART_ROUTER_THRESHOLD` | 40 | 否 | Smart Router 复杂度阈值 |
| `RATE_LIMIT_RPM` | 60 | 否 | 全局限流 RPM |
| `MAX_RETRIES` | 2 | 否 | 最大重试次数 |
| `DEFAULT_TIMEOUT_MS` | 30000 | 否 | 默认超时（毫秒） |

## 附录 B：服务端口一览

| 服务 | 端口 | 说明 |
|------|------|------|
| Hermes Agent | :8082 | 核心调度层（队列 + 路由 + K8S API） |
| Dashboard | :8082 | Hermes 提供的静态文件 |
| OpenClaw Gateway | :3005 | AI Agent 网关 + K8S 插件 |
| Ollama | :11434 | 本地模型推理（可选） |

## 附录 C：K8S AIWorkload 支持的 taskType

| taskType | intent slice | 说明 |
|----------|-------------|------|
| `batch-inference` | `batchInference` | 批量推理任务 |
| `distributed-training` | `distributedTraining` | 分布式训练任务 |
| `hyperparameter-tuning` | `hyperparameterTuning` | 超参调优任务 |

详细接口规范参见 [openclaw-k8s-integration-plugin-spec.md](openclaw-k8s-integration-plugin-spec.md)。
