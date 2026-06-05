import time
from typing import List

from hermes.agent import RoutingSkill, SkillCondition, SkillAction, SkillStatus


def bootstrap_routing_skills() -> List[RoutingSkill]:
    skills = []

    skills.append(RoutingSkill(
        name="privacy_local_route",
        conditions=[
            SkillCondition(pattern_type="require_local", pattern_value="true", weight=2.0),
        ],
        action=SkillAction(route_path="direct_local", fallback_path="gateway"),
        confidence=0.95,
        source="rule",
        tags=["privacy", "local", "constraint"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="simple_chat_local",
        conditions=[
            SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0),
            SkillCondition(pattern_type="complexity_range", pattern_value="0-14", weight=1.5),
        ],
        action=SkillAction(route_path="direct_local", model_hint="ollama/qwen2.5:3b", fallback_path="gateway"),
        confidence=0.85,
        source="rule",
        tags=["chat", "simple", "local"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="moderate_chat_gateway",
        conditions=[
            SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0),
            SkillCondition(pattern_type="complexity_range", pattern_value="15-39", weight=1.5),
        ],
        action=SkillAction(route_path="gateway", fallback_path="direct_local"),
        confidence=0.75,
        source="rule",
        tags=["chat", "moderate", "gateway"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="complex_agent_chain",
        conditions=[
            SkillCondition(pattern_type="complexity_range", pattern_value="40-59", weight=2.0),
        ],
        action=SkillAction(route_path="agent_chain", fallback_path="gateway"),
        confidence=0.80,
        source="rule",
        tags=["complex", "agent", "multi-step"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="highly_complex_agent_chain",
        conditions=[
            SkillCondition(pattern_type="complexity_range", pattern_value="60-200", weight=2.0),
        ],
        action=SkillAction(route_path="agent_chain", fallback_path="gateway"),
        confidence=0.90,
        source="rule",
        tags=["highly_complex", "agent", "critical"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="tool_call_gateway",
        conditions=[
            SkillCondition(pattern_type="has_tools", pattern_value="true", weight=2.0),
        ],
        action=SkillAction(route_path="gateway", model_hint="moonshot/kimi-k2.6", fallback_path="agent_chain"),
        confidence=0.85,
        source="rule",
        tags=["tool_call", "gateway", "api"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    for kw, path, conf, tags in [
        ("多步骤", "agent_chain", 0.80, ["keyword", "multi-step"]),
        ("自主", "agent_chain", 0.75, ["keyword", "autonomous"]),
        ("设计", "agent_chain", 0.70, ["keyword", "design"]),
        ("规划", "agent_chain", 0.75, ["keyword", "planning"]),
        ("执行计划", "agent_chain", 0.80, ["keyword", "execution"]),
        ("自主执行", "agent_chain", 0.80, ["keyword", "autonomous-execution"]),
        ("分析", "gateway", 0.65, ["keyword", "analysis"]),
        ("比较", "gateway", 0.60, ["keyword", "comparison"]),
        ("评估", "gateway", 0.65, ["keyword", "evaluation"]),
        ("优化", "gateway", 0.65, ["keyword", "optimization"]),
        ("搜索", "gateway", 0.70, ["keyword", "search"]),
        ("查询", "gateway", 0.60, ["keyword", "query"]),
    ]:
        skills.append(RoutingSkill(
            name=f"keyword_{kw}_to_{path}",
            conditions=[
                SkillCondition(pattern_type="keyword", pattern_value=kw, weight=1.5),
            ],
            action=SkillAction(route_path=path, fallback_path="gateway"),
            confidence=conf,
            source="rule",
            tags=tags,
            created_at=time.time(),
            updated_at=time.time(),
        ))

    skills.append(RoutingSkill(
        name="completion_local",
        conditions=[
            SkillCondition(pattern_type="request_type", pattern_value="completion", weight=1.0),
            SkillCondition(pattern_type="complexity_range", pattern_value="0-39", weight=1.0),
        ],
        action=SkillAction(route_path="direct_local", fallback_path="gateway"),
        confidence=0.70,
        source="rule",
        tags=["completion", "local"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="code_execution_agent",
        conditions=[
            SkillCondition(pattern_type="request_type", pattern_value="code_execution", weight=2.0),
        ],
        action=SkillAction(route_path="agent_chain", fallback_path="gateway"),
        confidence=0.80,
        source="rule",
        tags=["code", "execution", "agent"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="embedding_local",
        conditions=[
            SkillCondition(pattern_type="request_type", pattern_value="embedding", weight=1.5),
        ],
        action=SkillAction(route_path="direct_local", fallback_path="gateway"),
        confidence=0.75,
        source="rule",
        tags=["embedding", "local"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    skills.append(RoutingSkill(
        name="high_priority_latency",
        conditions=[
            SkillCondition(pattern_type="priority_range", pattern_value="1,2", weight=1.5),
            SkillCondition(pattern_type="complexity_range", pattern_value="0-39", weight=1.0),
        ],
        action=SkillAction(route_path="direct_local", fallback_path="gateway"),
        confidence=0.70,
        source="rule",
        tags=["priority", "latency", "local"],
        created_at=time.time(),
        updated_at=time.time(),
    ))

    return skills
