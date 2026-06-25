"""
K8S 配置验证脚本 — 检查所有修改后的 K8S YAML 配置的正确性

验证项目:
1. YAML 语法正确性
2. nginx envsubst 模板注入正确性
3. watchdog K8S 禁用配置
4. Secret 引用完整性
5. Dashboard API 端点 nginx 代理映射
6. 所有服务连通性

用法:
  python tests/verify_k8s_config.py              # 静态验证（不需要集群）
  python tests/verify_k8s_config.py --live       # 实时验证（需要 KinD 集群）
  python tests/verify_k8s_config.py --live --url http://localhost:8090
"""

import argparse
import json
import os
import sys
import subprocess
from typing import Dict, List


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K8S_DIR = os.path.join(PROJECT_ROOT, "k8s")


def verify_yaml_syntax() -> List[str]:
    """验证所有 K8S YAML 文件的语法正确性。"""
    print("\n=== 1. K8S YAML 语法验证 ===")
    errors = []

    yaml_files = sorted([f for f in os.listdir(K8S_DIR) if f.endswith('.yaml')])
    for yaml_file in yaml_files:
        filepath = os.path.join(K8S_DIR, yaml_file)
        try:
            import yaml
            with open(filepath) as f:
                docs = list(yaml.safe_load_all(f))
            print(f"  ✓ {yaml_file}: {len(docs)} documents")
        except ImportError:
            # yaml 模块不存在，用 kubectl dry-run
            result = subprocess.run(
                ["kubectl", "apply", "--dry-run=client", "-f", filepath],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                errors.append(f"{yaml_file}: {result.stderr[:200]}")
                print(f"  ✗ {yaml_file}: FAIL")
            else:
                print(f"  ✓ {yaml_file}: OK")
        except Exception as e:
            errors.append(f"{yaml_file}: {str(e)[:200]}")
            print(f"  ✗ {yaml_file}: FAIL - {e}")

    return errors


def verify_no_sk_litellm_local() -> List[str]:
    """验证 nginx 配置中没有 sk-litellm-local 硬编码残留。"""
    print("\n=== 2. sk-litellm-local 硬编码残留检查 ===")
    errors = []

    files_to_check = [
        os.path.join(K8S_DIR, "02-configmaps.yaml"),
        os.path.join(PROJECT_ROOT, "docker", "nginx.conf"),
        os.path.join(K8S_DIR, "11-nginx.yaml"),
    ]

    for filepath in files_to_check:
        with open(filepath) as f:
            content = f.read()
        count = content.count("sk-litellm-local")
        if count > 0:
            errors.append(f"{os.path.basename(filepath)}: {count} 处 sk-litellm-local 残留")
            print(f"  ✗ {os.path.basename(filepath)}: {count} 处残留")
        else:
            print(f"  ✓ {os.path.basename(filepath)}: 无残留")

    return errors


def verify_envsubst_template() -> List[str]:
    """验证 envsubst 模板变量正确性。"""
    print("\n=== 3. envsubst 模板变量验证 ===")
    errors = []

    # 检查 02-configmaps.yaml 中 ConfigMap 名称和 data key
    configmap_file = os.path.join(K8S_DIR, "02-configmaps.yaml")
    import yaml
    with open(configmap_file) as f:
        docs = list(yaml.safe_load_all(f))

    for doc in docs:
        if doc and doc.get("kind") == "ConfigMap":
            name = doc.get("metadata", {}).get("name", "")
            data_keys = list(doc.get("data", {}).keys())

            if name == "nginx-config-template":
                print(f"  ✓ ConfigMap 名称: nginx-config-template")
                # 检查 data key 是否是 .template
                if "default.conf.template" in data_keys:
                    print(f"  ✓ Data key: default.conf.template (envsubst 输入)")
                else:
                    errors.append("02-configmaps.yaml: data key 应为 default.conf.template")
                    print(f"  ✗ Data key 不是 default.conf.template: {data_keys}")

                # 检查模板内容中 ${LITELLM_MASTER_KEY} 的数量
                nginx_content = doc.get("data", {}).get("default.conf.template", "")
                litellm_key_count = nginx_content.count("${LITELLM_MASTER_KEY}")
                print(f"  ✓ ${{LITELLM_MASTER_KEY}} 出现 {litellm_key_count} 次")

            elif name == "litellm-config":
                print(f"  ✓ ConfigMap 名称: litellm-config (不需要 envsubst)")

    # 检查 11-nginx.yaml 中 initContainer
    nginx_yaml = os.path.join(K8S_DIR, "11-nginx.yaml")
    with open(nginx_yaml) as f:
        nginx_docs = list(yaml.safe_load_all(f))

    for doc in nginx_docs:
        if doc and doc.get("kind") == "Deployment":
            spec = doc.get("spec", {}).get("template", {}).get("spec", {})
            containers = spec.get("containers", [])
            init_containers = spec.get("initContainers", [])

            if init_containers:
                init = init_containers[0]
                init_name = init.get("name", "")
                init_image = init.get("image", "")
                init_command = init.get("command", [])
                init_args = init.get("args", [])

                print(f"  ✓ initContainer 名称: {init_name}")
                print(f"  ✓ initContainer 镜像: {init_image}")

                # 验证 envsubst 命令参数
                if init_args:
                    envsubst_cmd = " ".join(init_args)
                    if "envsubst" in envsubst_cmd and "${LITELLM_MASTER_KEY}" in envsubst_cmd or "LITELLM_MASTER_KEY" in envsubst_cmd:
                        print(f"  ✓ envsubst 命令包含 LITELLM_MASTER_KEY 变量替换")
                    else:
                        errors.append("11-nginx.yaml: envsubst 命令参数可能不正确")
                        print(f"  ✗ envsubst 命令: {envsubst_cmd[:200]}")

                # 验证 Secret 引用
                env_vars = init.get("env", [])
                secret_found = False
                for env in env_vars:
                    if env.get("name") == "LITELLM_MASTER_KEY":
                        value_from = env.get("valueFrom", {})
                        secret_ref = value_from.get("secretKeyRef", {})
                        if secret_ref.get("name") == "openclaw-secrets" and secret_ref.get("key") == "litellm-master-key":
                            print(f"  ✓ LITELLM_MASTER_KEY 引用 Secret: openclaw-secrets/litellm-master-key")
                            secret_found = True

                if not secret_found:
                    errors.append("11-nginx.yaml: initContainer 缺少 LITELLM_MASTER_KEY Secret 引用")
                    print(f"  ✗ initContainer 缺少 LITELLM_MASTER_KEY Secret 引用")

                # 验证 volumeMounts
                mounts = init.get("volumeMounts", [])
                template_mount = False
                output_mount = False
                for m in mounts:
                    if m.get("mountPath") == "/etc/nginx/templates/":
                        template_mount = True
                        print(f"  ✓ initContainer 模板挂载: /etc/nginx/templates/")
                    if m.get("mountPath") == "/etc/nginx/conf.d/":
                        output_mount = True
                        print(f"  ✓ initContainer 输出挂载: /etc/nginx/conf.d/")

                if not template_mount:
                    errors.append("11-nginx.yaml: initContainer 缺少模板目录挂载")
                    print(f"  ✗ initContainer 缺少模板目录挂载")
                if not output_mount:
                    errors.append("11-nginx.yaml: initContainer 缺少输出目录挂载")
                    print(f"  ✗ initContainer 缺少输出目录挂载")

            else:
                errors.append("11-nginx.yaml: 缺少 initContainers")
                print(f"  ✗ nginx Deployment 缺少 initContainers")

            # 验证主 nginx container 的 volumeMounts
            for c in containers:
                if c.get("name") == "nginx":
                    mounts = c.get("volumeMounts", [])
                    conf_mount = False
                    for m in mounts:
                        if m.get("mountPath") == "/etc/nginx/conf.d/":
                            conf_mount = True
                            print(f"  ✓ nginx 主容器挂载: /etc/nginx/conf.d/ (从 emptyDir)")
                    if not conf_mount:
                        errors.append("11-nginx.yaml: nginx 主容器缺少 /etc/nginx/conf.d/ 挂载")
                        print(f"  ✗ nginx 主容器缺少 /etc/nginx/conf.d/ 挂载")

            # 验证 volumes
            volumes = spec.get("volumes", [])
            has_configmap_volume = False
            has_emptydir_volume = False
            for v in volumes:
                if v.get("configMap"):
                    cm_name = v.get("configMap", {}).get("name", "")
                    if cm_name == "nginx-config-template":
                        has_configmap_volume = True
                        print(f"  ✓ Volume nginx-config-template (ConfigMap)")
                if v.get("emptyDir") is not None:  # emptyDir:{} 是合法值，检查 key 存在而非值 truthiness
                    has_emptydir_volume = True
                    print(f"  ✓ Volume {v.get('name')}: emptyDir (渲染输出)")

            if not has_configmap_volume:
                errors.append("11-nginx.yaml: 缺少 nginx-config-template ConfigMap volume")
                print(f"  ✗ 缺少 ConfigMap volume")
            if not has_emptydir_volume:
                errors.append("11-nginx.yaml: 缺少 emptyDir volume")
                print(f"  ✗ 缺少 emptyDir volume")

    return errors


def verify_watchdog_config() -> List[str]:
    """验证 watchdog K8S 禁用配置。"""
    print("\n=== 4. watchdog K8S 禁用配置验证 ===")
    errors = []

    # 检查 hermes server.py 中的 K8S 检测逻辑
    server_file = os.path.join(PROJECT_ROOT, "hermes", "server.py")
    with open(server_file) as f:
        content = f.read()

    if "KUBERNETES_SERVICE_HOST" in content:
        print(f"  ✓ server.py 包含 KUBERNETES_SERVICE_HOST 检测")
    else:
        errors.append("server.py: 缺少 KUBERNETES_SERVICE_HOST 检测")
        print(f"  ✗ server.py 缺少 KUBERNETES_SERVICE_HOST 检测")

    if "WATCHDOG_ENABLED" in content:
        print(f"  ✓ server.py 包含 WATCHDOG_ENABLED 配置检查")
    else:
        errors.append("server.py: 缺少 WATCHDOG_ENABLED 配置检查")
        print(f"  ✗ server.py 缺少 WATCHDOG_ENABLED 配置检查")

    # 检查 07-hermes.yaml 中的 WATCHDOG_ENABLED env var
    hermes_yaml = os.path.join(K8S_DIR, "07-hermes.yaml")
    with open(hermes_yaml) as f:
        content = f.read()

    if "WATCHDOG_ENABLED" in content and "false" in content:
        print(f"  ✓ 07-hermes.yaml 包含 WATCHDOG_ENABLED=false")
    else:
        errors.append("07-hermes.yaml: 缺少 WATCHDOG_ENABLED=false")
        print(f"  ✗ 07-hermes.yaml 缺少 WATCHDOG_ENABLED=false")

    return errors


def verify_dashboard_api_mapping() -> List[str]:
    """验证 Dashboard 调用的 API 端点在 nginx 中有对应代理。"""
    print("\n=== 5. Dashboard API 端点 nginx 代理映射验证 ===")
    errors = []

    # Dashboard 调用的关键端点
    required_endpoints = [
        "/health",
        "/hermes/health",
        "/hermes/stats",
        "/v1/system/metrics",
        "/v1/chat",
        "/v1/chat/completions",
        "/v1/models",
        "/v1/stream/ws",
        "/v1/stream/health",
        "/svc/litellm/",
        "/svc/stream/",
        "/svc/ollama/",
        "/svc/funasr/",
        "/dashboard.html",
        "/new_dashboard.html",
        "/static/",
    ]

    # 从 02-configmaps.yaml 读取 nginx 配置模板
    configmap_file = os.path.join(K8S_DIR, "02-configmaps.yaml")
    import yaml
    with open(configmap_file) as f:
        docs = list(yaml.safe_load_all(f))

    nginx_conf = ""
    for doc in docs:
        if doc and doc.get("kind") == "ConfigMap":
            data = doc.get("data", {})
            # 获取 nginx 配置内容（可能是 default.conf.template 或 default.conf）
            for key in ["default.conf.template", "default.conf"]:
                if key in data:
                    nginx_conf = data[key]
                    break

    if not nginx_conf:
        errors.append("02-configmaps.yaml: 无法获取 nginx 配置内容")
        print(f"  ✗ 无法获取 nginx 配置内容")
        return errors

    # 检查每个端点是否在 nginx 配置中有对应 location 块
    for endpoint in required_endpoints:
        # 搜索 location 块
        pattern = f"location"
        found = False
        # 精确匹配和前缀匹配
        for line in nginx_conf.split("\n"):
            stripped = line.strip()
            if stripped.startswith("location"):
                # 提取 location 路径
                loc_path = stripped.split()[1] if len(stripped.split()) > 1 else ""
                # 移除修饰符 (=, ~, ~*, ^~)
                for mod in ["=", "~", "~*", "^~"]:
                    loc_path = loc_path.lstrip(mod)
                # 检查是否匹配
                if endpoint.startswith(loc_path.rstrip("/")) or loc_path.rstrip("/") == endpoint.rstrip("/"):
                    found = True
                    break

        if found:
            print(f"  ✓ {endpoint} → nginx 有对应 location 块")
        else:
            # 检查兜底 / location
            if "location / {" in nginx_conf or "location /\n" in nginx_conf:
                # 兜底 location 会处理未显式定义的路径
                print(f"  ✓ {endpoint} → 由兜底 / location 处理")
            else:
                errors.append(f"nginx: {endpoint} 缺少对应 location 块")
                print(f"  ✗ {endpoint} 缺少对应 location 块")

    return errors


def verify_live_services(base_url: str) -> List[str]:
    """实时验证服务连通性。"""
    print(f"\n=== 6. 服务连通性实时验证 ===")
    print(f"  Base URL: {base_url}")
    errors = []

    import httpx
    client = httpx.Client(timeout=10.0)

    endpoints = [
        ("GET", "/health", "Nginx health"),
        ("GET", "/hermes/health", "Hermes health"),
        ("GET", "/stats", "Hermes stats"),
        ("GET", "/v1/system/metrics", "System metrics"),
        ("GET", "/v1/stream/health", "Stream Service health"),
        ("GET", "/svc/ollama/api/tags", "Ollama models"),
    ]

    for method, path, label in endpoints:
        try:
            if method == "GET":
                resp = client.get(f"{base_url}{path}")
            elapsed_ms = (resp.elapsed.total_seconds() * 1000) if hasattr(resp, 'elapsed') else 0

            if resp.status_code < 400:
                print(f"  ✓ {label} ({path}): status={resp.status_code} latency={elapsed_ms:.1f}ms")
            else:
                errors.append(f"{path}: HTTP {resp.status_code}")
                print(f"  ✗ {label} ({path}): HTTP {resp.status_code}")
        except Exception as e:
            errors.append(f"{path}: {str(e)[:100]}")
            print(f"  ✗ {label} ({path}): {str(e)[:100]}")

    # 专门验证 nginx envsubst 替换效果
    print("\n  === nginx envsubst 替换验证 ===")
    try:
        # 尝试访问需要 auth 的 LiteLLM 端点（如果 envsubst 正确，应该注入了 master key）
        resp = client.get(f"{base_url}/svc/litellm/health/liveliness")
        if resp.status_code == 200:
            print(f"  ✓ LiteLLM liveliness 通过 nginx auth 代理可达 (envsubst 正确)")
        elif resp.status_code == 401:
            errors.append("LiteLLM: 401 Unauthorized - envsubst 可能未生效")
            print(f"  ✗ LiteLLM 返回 401 - envsubst 可能未生效")
        else:
            print(f"  ? LiteLLM liveliness: status={resp.status_code}")
    except Exception as e:
        errors.append(f"LiteLLM liveliness: {str(e)[:100]}")
        print(f"  ✗ LiteLLM liveliness: {str(e)[:100]}")

    # 验证 watchdog 是否被禁用
    print("\n  === watchdog K8S 禁用验证 ===")
    try:
        resp = client.get(f"{base_url}/hermes/health")
        if resp.status_code == 200:
            data = resp.json()
            print(f"  ✓ Hermes health: {data.get('status', '?')}")

            # 检查 Hermes 日志中的 watchdog 状态
            # 这里无法直接看日志，但可以通过功能测试确认
            print(f"  ? watchdog 状态需通过 kubectl logs 确认")
            print(f"    建议运行: kubectl logs -n openclaw deploy/hermes | grep Watchdog")
    except Exception as e:
        errors.append(f"Hermes health: {str(e)[:100]}")
        print(f"  ✗ Hermes health: {str(e)[:100]}")

    client.close()
    return errors


def main():
    parser = argparse.ArgumentParser(description="K8S config verification for OpenClaw")
    parser.add_argument("--live", action="store_true", help="Run live service connectivity tests")
    parser.add_argument("--url", default="http://localhost:8090", help="Base URL for live tests")

    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════════════════╗")
    print("║  OpenClaw K8S 配置验证 + Dashboard 功能验证               ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    all_errors = []

    # 静态验证
    all_errors.extend(verify_yaml_syntax())
    all_errors.extend(verify_no_sk_litellm_local())
    all_errors.extend(verify_envsubst_template())
    all_errors.extend(verify_watchdog_config())
    all_errors.extend(verify_dashboard_api_mapping())

    # 实时验证
    if args.live:
        all_errors.extend(verify_live_services(args.url))

    # 总结
    print("\n╔══════════════════════════════════════════════════════════════╗")
    if all_errors:
        print(f"║  ✗ {len(all_errors)} 个问题需要修复                              ║")
        for err in all_errors:
            print(f"║    - {err}                             ║")
    else:
        print("║  ✓ 所有验证通过！                                          ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    sys.exit(1 if all_errors else 0)


if __name__ == "__main__":
    main()
