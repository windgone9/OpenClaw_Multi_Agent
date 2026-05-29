import re
import os

MD_FILE = "/Users/yangxu/MyWork/OpenClaw_Multi_Agent/OPENCLAW_CORE_ALGORITHM_FLOWCHARTS.md"
OUT_DIR = "/Users/yangxu/MyWork/OpenClaw_Multi_Agent/flowcharts_mmd"

CHART_NAMES = [
    "01_main_dispatch_flow",
    "02_dual_path_architecture",
    "03_classify_request_decision_tree",
    "04_select_strategy_agent_type",
    "05_strategy_router_main",
    "06_local_first_routing",
    "07_cloud_first_routing",
    "08_latency_optimized_routing",
    "09_cost_optimized_routing",
    "10_capability_optimized_routing",
    "11_privacy_first_routing",
    "12_fallback_mechanism",
    "13_bridge_fallback",
    "14_ema_weight_update",
    "15_circuit_breaker_state_machine",
    "16_ema_params_comparison",
    "17_pre_hook_pipeline",
    "18_pre_hook_order",
    "19_post_hook_order",
    "20_rate_limit_sliding_window",
    "21_retry_decision",
    "22_call_model_api",
    "23_gateway_route_mapping",
    "24_agent_message_handling",
    "25_power_of_two",
    "26_priority_scoring",
    "27_five_lb_algorithms",
    "28_bridge_adaptive_route",
    "29_bridge_select_best_model",
    "30_endpoint_state_ema_update",
    "31_endpoint_lifecycle_state",
    "32_ema_params_influence",
]

with open(MD_FILE, "r", encoding="utf-8") as f:
    content = f.read()

pattern = r"```mermaid\n(.*?)```"
matches = re.findall(pattern, content, re.DOTALL)

print(f"Found {len(matches)} mermaid blocks")

for i, mermaid_code in enumerate(matches):
    if i < len(CHART_NAMES):
        name = CHART_NAMES[i]
    else:
        name = f"chart_{i+1:02d}"

    filepath = os.path.join(OUT_DIR, f"{name}.mmd")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(mermaid_code)
    print(f"  Written: {name}.mmd ({len(mermaid_code)} chars)")
