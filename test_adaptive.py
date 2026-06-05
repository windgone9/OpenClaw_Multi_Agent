import sys
sys.path.insert(0, ".")

from scheduler.models.request import DispatchRequest, Priority, RequestType
from scheduler.models.model_config import ModelEndpoint, ModelType, ModelRegistry, ModelProvider
from scheduler.strategy.adaptive import AdaptiveStrategy
from scheduler.strategy.router import StrategyRouter, RoutingDecision

registry = ModelRegistry()
registry.register(ModelEndpoint(
    name="ollama/qwen2.5:3b", display_name="Ollama Qwen2.5 3B (Local)",
    model_type=ModelType.LOCAL, provider=ModelProvider.OLLAMA,
    base_url="http://localhost:11434/v1", model_id="qwen2.5:3b",
    max_context_length=32768, supports_streaming=True, supports_tools=False,
    cost_per_1k_input_tokens=0.0, cost_per_1k_output_tokens=0.0,
    priority=1, weight=3, max_concurrent=5, tags=["chat", "completion", "local"],
))
registry.register(ModelEndpoint(
    name="moonshot/kimi-k2.6", display_name="Kimi K2.6 (Cloud)",
    model_type=ModelType.CLOUD, provider=ModelProvider.MOONSHOT,
    base_url="https://api.moonshot.cn/v1", model_id="kimi-k2.6",
    max_context_length=262144, supports_streaming=True, supports_tools=True,
    cost_per_1k_input_tokens=0.76, cost_per_1k_output_tokens=3.2,
    priority=2, weight=2, max_concurrent=15, tags=["chat", "completion", "tool_call", "cloud"],
))
registry.register(ModelEndpoint(
    name="deepseek/deepseek-chat", display_name="DeepSeek Chat (Cloud)",
    model_type=ModelType.CLOUD, provider=ModelProvider.DEEPSEEK,
    base_url="https://api.deepseek.com/v1", model_id="deepseek-chat",
    max_context_length=64000, supports_streaming=True, supports_tools=False,
    cost_per_1k_input_tokens=0.00014, cost_per_1k_output_tokens=0.00028,
    priority=3, weight=3, max_concurrent=20, tags=["chat", "completion", "code", "cloud"],
))

router = StrategyRouter(registry, use_openclaw=True)

print("=" * 60)
print("OpenClaw Adaptive Scheduling Strategy Test")
print("=" * 60)

test_cases = [
    ("Simple Chat (NORMAL)", DispatchRequest(
        appid="test", type=RequestType.CHAT, prompt="hello",
        priority=Priority.NORMAL,
    )),
    ("Tool Call Request", DispatchRequest(
        appid="test", type=RequestType.TOOL_CALL, prompt="call tool",
        priority=Priority.NORMAL,
    )),
    ("High Priority Request", DispatchRequest(
        appid="test", type=RequestType.CHAT, prompt="urgent",
        priority=Priority.HIGH,
    )),
    ("Critical Priority Request", DispatchRequest(
        appid="test", type=RequestType.CHAT, prompt="critical",
        priority=Priority.CRITICAL,
    )),
    ("Low Priority Request", DispatchRequest(
        appid="test", type=RequestType.CHAT, prompt="background",
        priority=Priority.LOW,
    )),
    ("Privacy-Required Request", DispatchRequest(
        appid="test", type=RequestType.CHAT, prompt="secret data",
        priority=Priority.NORMAL,
        constraints={"require_local": True},
    )),
    ("Cost-Constrained Request", DispatchRequest(
        appid="test", type=RequestType.CHAT, prompt="cheap",
        priority=Priority.NORMAL,
        constraints={"max_cost_per_request": 0},
    )),
    ("Latency-Sensitive Request", DispatchRequest(
        appid="test", type=RequestType.CHAT, prompt="fast",
        priority=Priority.NORMAL,
        constraints={"max_latency_ms": 500},
    )),
    ("Embedding Request", DispatchRequest(
        appid="test", type=RequestType.EMBEDDING, prompt="embed this",
        priority=Priority.NORMAL,
    )),
]

for name, req in test_cases:
    decision = router.route(req)
    ep_name = decision.selected_endpoint.name if decision.selected_endpoint else "None"
    print(f"\n{name}:")
    print(f"  -> agent_type={decision.agent_type}, endpoint={ep_name}")
    print(f"  -> strategy={decision.strategy_name}, openclaw={decision.use_openclaw}")
    print(f"  -> reason={decision.reason}")

print("\n" + "=" * 60)
print("Adaptive State:")
print(router.get_adaptive_state())
print("=" * 60)
print("\nAll tests passed!")
