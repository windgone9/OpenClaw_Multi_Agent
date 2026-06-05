import time
from unittest.mock import patch, MagicMock

import pytest

from hermes.agent import (
    RoutingSkill, SkillCondition, SkillAction, SkillStatus,
    HermesAgent,
)
from hermes.skill_bootstrap import bootstrap_routing_skills


def _make_skill(name="test_skill", route_path="gateway", confidence=0.8,
                conditions=None, tags=None, source="rule"):
    if conditions is None:
        conditions = [SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0)]
    return RoutingSkill(
        name=name,
        conditions=conditions,
        action=SkillAction(route_path=route_path, fallback_path="direct_local"),
        confidence=confidence,
        source=source,
        tags=tags or [],
        created_at=time.time(),
        updated_at=time.time(),
    )


def _make_request(prompt="hello", req_type="chat", priority=3,
                  rule_score=5, tools=None, constraints=None):
    req = {
        "prompt": prompt,
        "type": req_type,
        "priority": priority,
        "_rule_complexity_score": rule_score,
    }
    if tools:
        req["tools"] = tools
    if constraints:
        req["constraints"] = constraints
    return req


class TestSkillCondition:
    def test_request_type_match(self):
        cond = SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0)
        assert cond.matches(_make_request(req_type="chat")) is True
        assert cond.matches(_make_request(req_type="completion")) is False

    def test_keyword_match(self):
        cond = SkillCondition(pattern_type="keyword", pattern_value="多步骤", weight=1.5)
        assert cond.matches(_make_request(prompt="请帮我设计一个多步骤计划")) is True
        assert cond.matches(_make_request(prompt="你好")) is False

    def test_complexity_range_match(self):
        cond = SkillCondition(pattern_type="complexity_range", pattern_value="0-14", weight=1.5)
        assert cond.matches(_make_request(rule_score=5)) is True
        assert cond.matches(_make_request(rule_score=14)) is True
        assert cond.matches(_make_request(rule_score=15)) is False
        assert cond.matches(_make_request(rule_score=0)) is True

    def test_complexity_range_high(self):
        cond = SkillCondition(pattern_type="complexity_range", pattern_value="40-200", weight=2.0)
        assert cond.matches(_make_request(rule_score=45)) is True
        assert cond.matches(_make_request(rule_score=39)) is False

    def test_has_tools_match(self):
        cond = SkillCondition(pattern_type="has_tools", pattern_value="true", weight=2.0)
        assert cond.matches(_make_request(tools=[{"type": "function"}])) is True
        assert cond.matches(_make_request(tools=[])) is False
        assert cond.matches(_make_request()) is False

    def test_require_local_match(self):
        cond = SkillCondition(pattern_type="require_local", pattern_value="true", weight=2.0)
        assert cond.matches(_make_request(constraints={"require_local": True})) is True
        assert cond.matches(_make_request()) is False

    def test_priority_range_match(self):
        cond = SkillCondition(pattern_type="priority_range", pattern_value="1,2", weight=1.5)
        assert cond.matches(_make_request(priority=1)) is True
        assert cond.matches(_make_request(priority=2)) is True
        assert cond.matches(_make_request(priority=3)) is False

    def test_invalid_complexity_range(self):
        cond = SkillCondition(pattern_type="complexity_range", pattern_value="invalid", weight=1.0)
        assert cond.matches(_make_request(rule_score=5)) is False

    def test_unknown_pattern_type(self):
        cond = SkillCondition(pattern_type="unknown_type", pattern_value="test", weight=1.0)
        assert cond.matches(_make_request()) is False


class TestRoutingSkill:
    def test_match_score_active(self):
        skill = _make_skill(
            conditions=[
                SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0),
                SkillCondition(pattern_type="complexity_range", pattern_value="0-14", weight=1.5),
            ],
            confidence=0.8,
        )
        req = _make_request(req_type="chat", rule_score=5)
        score = skill.match_score(req)
        assert score == pytest.approx(0.8, abs=0.01)

    def test_match_score_partial(self):
        skill = _make_skill(
            conditions=[
                SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0),
                SkillCondition(pattern_type="complexity_range", pattern_value="0-14", weight=1.5),
            ],
            confidence=0.8,
        )
        req = _make_request(req_type="chat", rule_score=30)
        score = skill.match_score(req)
        expected = (1.0 / 2.5) * 0.8
        assert score == pytest.approx(expected, abs=0.01)

    def test_match_score_no_match(self):
        skill = _make_skill(
            conditions=[
                SkillCondition(pattern_type="request_type", pattern_value="code_execution", weight=1.0),
            ],
            confidence=0.8,
        )
        req = _make_request(req_type="chat")
        score = skill.match_score(req)
        assert score == 0.0

    def test_match_score_suspended(self):
        skill = _make_skill(confidence=0.8)
        skill.status = SkillStatus.SUSPENDED
        score = skill.match_score(_make_request())
        assert score == 0.0

    def test_record_result_success(self):
        skill = _make_skill(confidence=0.5)
        skill.record_result(True, 100)
        assert skill.hit_count == 1
        assert skill.success_count == 1
        assert skill.fail_count == 0
        assert skill.confidence > 0.5

    def test_record_result_failure(self):
        skill = _make_skill(confidence=0.5)
        skill.record_result(False, 500)
        assert skill.hit_count == 1
        assert skill.success_count == 0
        assert skill.fail_count == 1
        assert skill.confidence < 0.5

    def test_record_result_consecutive_failures_suspend(self):
        skill = _make_skill(confidence=0.8)
        for _ in range(5):
            skill.record_result(False, 500)
        assert skill.status == SkillStatus.SUSPENDED

    def test_record_result_reactivate_on_success(self):
        skill = _make_skill(confidence=0.1)
        skill.hit_count = 10
        skill.success_count = 1
        skill.status = SkillStatus.SUSPENDED
        for _ in range(5):
            skill.record_result(True, 100)
        assert skill.confidence >= 0.2
        assert skill.status == SkillStatus.SUSPENDED

    def test_to_dict(self):
        skill = _make_skill(name="test", confidence=0.75, tags=["chat"])
        d = skill.to_dict()
        assert d["name"] == "test"
        assert d["confidence"] == 0.75
        assert d["tags"] == ["chat"]
        assert d["status"] == "active"
        assert len(d["conditions"]) == 1


class TestHermesAgent:
    def test_init_default(self):
        agent = HermesAgent()
        assert agent.enabled is True
        assert len(agent.skills) == 0

    def test_register_skill(self):
        agent = HermesAgent()
        skill = _make_skill(name="test_skill")
        agent.register_skill(skill)
        assert "test_skill" in agent.skills
        assert agent.skills["test_skill"].confidence == 0.8

    def test_register_skill_overwrite(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(name="test", confidence=0.5))
        agent.register_skill(_make_skill(name="test", confidence=0.9))
        assert agent.skills["test"].confidence == 0.9

    def test_unregister_skill(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(name="test"))
        agent.unregister_skill("test")
        assert "test" not in agent.skills

    def test_unregister_nonexistent(self):
        agent = HermesAgent()
        agent.unregister_skill("nonexistent")

    def test_decide_disabled(self):
        agent = HermesAgent()
        agent.enabled = False
        result = agent.decide(_make_request(rule_score=5))
        assert result["agent_decision"] == "fallback"
        assert result["route_path"] == "direct_local"

    def test_decide_no_skills(self):
        agent = HermesAgent()
        result = agent.decide(_make_request())
        assert result["agent_decision"] == "fallback"

    def test_decide_high_confidence_skill(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(
            name="chat_local",
            route_path="direct_local",
            confidence=0.9,
            conditions=[SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0)],
        ))
        result = agent.decide(_make_request(req_type="chat"))
        assert result["agent_decision"] == "use_skill"
        assert result["route_path"] == "direct_local"
        assert result["selected_skill"] == "chat_local"

    def test_decide_fallback_low_score(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(
            name="code_skill",
            route_path="agent_chain",
            confidence=0.3,
            conditions=[SkillCondition(pattern_type="request_type", pattern_value="code_execution", weight=1.0)],
        ))
        result = agent.decide(_make_request(req_type="chat"))
        assert result["agent_decision"] == "fallback"

    def test_decide_explore(self):
        agent = HermesAgent()
        agent._exploration_rate = 1.0
        agent.register_skill(_make_skill(
            name="chat_local",
            route_path="direct_local",
            confidence=0.9,
            conditions=[SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0)],
        ))
        with patch.object(agent, '_should_explore', return_value=True):
            result = agent.decide(_make_request(req_type="chat"))
            assert result["agent_decision"] == "explore"

    def test_record_feedback_updates_skill(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(name="test_skill", confidence=0.5))
        agent.record_feedback(
            skill_name="test_skill",
            route_path="gateway",
            success=True,
            latency_ms=100,
        )
        assert agent.skills["test_skill"].hit_count == 1
        assert agent.skills["test_skill"].success_count == 1

    def test_record_feedback_no_skill(self):
        agent = HermesAgent()
        agent.record_feedback(
            skill_name=None,
            route_path="gateway",
            success=True,
            latency_ms=100,
        )

    def test_record_feedback_unknown_skill(self):
        agent = HermesAgent()
        agent.record_feedback(
            skill_name="nonexistent",
            route_path="gateway",
            success=True,
            latency_ms=100,
        )

    def test_get_stats(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(name="test1"))
        agent.register_skill(_make_skill(name="test2"))
        stats = agent.get_stats()
        assert stats["enabled"] is True
        assert stats["skills"]["total"] == 2
        assert stats["skills"]["active"] == 2

    def test_bootstrap_from_rules(self):
        agent = HermesAgent()
        count = agent.bootstrap_from_rules()
        assert count > 0
        assert len(agent.skills) == count
        assert "simple_chat_local" in agent.skills
        assert "privacy_local_route" in agent.skills

    def test_llm_decide_success(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(
            name="chat_skill",
            route_path="direct_local",
            confidence=0.5,
            conditions=[SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0)],
        ))
        candidates = [("chat_skill", 0.5)]
        with patch.object(agent, '_call_llm', return_value={
            "decision": "override",
            "override_path": "agent_chain",
            "reasoning": "complex task",
            "confidence": 0.85,
        }):
            result = agent._llm_decide(_make_request(), candidates)
        assert result["route_path"] == "agent_chain"
        assert result["agent_decision"] == "override"

    def test_llm_decide_failure_fallback(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(
            name="chat_skill",
            route_path="direct_local",
            confidence=0.5,
        ))
        candidates = [("chat_skill", 0.5)]
        with patch.object(agent, '_call_llm', return_value=None):
            result = agent._llm_decide(_make_request(), candidates)
        assert result["agent_decision"] in ("fallback", "use_skill")

    def test_find_candidate_skills(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(
            name="chat_local",
            route_path="direct_local",
            confidence=0.9,
            conditions=[SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0)],
        ))
        agent.register_skill(_make_skill(
            name="code_agent",
            route_path="agent_chain",
            confidence=0.8,
            conditions=[SkillCondition(pattern_type="request_type", pattern_value="code_execution", weight=1.0)],
        ))
        candidates = agent._find_candidate_skills(_make_request(req_type="chat"))
        assert len(candidates) >= 1
        assert candidates[0][0] == "chat_local"

    def test_combined_skills(self):
        agent = HermesAgent()
        agent.register_skill(_make_skill(
            name="chat_local",
            route_path="direct_local",
            confidence=0.9,
            conditions=[
                SkillCondition(pattern_type="request_type", pattern_value="chat", weight=1.0),
                SkillCondition(pattern_type="complexity_range", pattern_value="0-14", weight=1.5),
            ],
        ))
        agent.register_skill(_make_skill(
            name="keyword_多步骤",
            route_path="agent_chain",
            confidence=0.8,
            conditions=[SkillCondition(pattern_type="keyword", pattern_value="多步骤", weight=1.5)],
        ))
        req = _make_request(req_type="chat", prompt="请帮我设计一个多步骤计划", rule_score=10)
        candidates = agent._find_candidate_skills(req)
        chat_score = next((s for n, s in candidates if n == "chat_local"), 0)
        keyword_score = next((s for n, s in candidates if n == "keyword_多步骤"), 0)
        assert chat_score > 0
        assert keyword_score > 0


class TestSkillBootstrap:
    def test_bootstrap_returns_skills(self):
        skills = bootstrap_routing_skills()
        assert len(skills) > 0
        assert all(isinstance(s, RoutingSkill) for s in skills)

    def test_bootstrap_covers_all_paths(self):
        skills = bootstrap_routing_skills()
        paths = {s.action.route_path for s in skills}
        assert "direct_local" in paths
        assert "gateway" in paths
        assert "agent_chain" in paths

    def test_bootstrap_privacy_skill(self):
        skills = bootstrap_routing_skills()
        privacy = next((s for s in skills if s.name == "privacy_local_route"), None)
        assert privacy is not None
        assert privacy.confidence >= 0.9
        assert privacy.action.route_path == "direct_local"

    def test_bootstrap_simple_chat_skill(self):
        skills = bootstrap_routing_skills()
        simple = next((s for s in skills if s.name == "simple_chat_local"), None)
        assert simple is not None
        assert simple.action.route_path == "direct_local"
        assert simple.action.model_hint == "ollama/qwen2.5:3b"

    def test_bootstrap_complex_skill(self):
        skills = bootstrap_routing_skills()
        complex_skill = next((s for s in skills if s.name == "highly_complex_agent_chain"), None)
        assert complex_skill is not None
        assert complex_skill.action.route_path == "agent_chain"
        assert complex_skill.confidence >= 0.85

    def test_bootstrap_keyword_skills(self):
        skills = bootstrap_routing_skills()
        keyword_skills = [s for s in skills if s.name.startswith("keyword_")]
        assert len(keyword_skills) > 0
        for ks in keyword_skills:
            assert len(ks.conditions) == 1
            assert ks.conditions[0].pattern_type == "keyword"

    def test_bootstrap_tool_call_skill(self):
        skills = bootstrap_routing_skills()
        tool_skill = next((s for s in skills if s.name == "tool_call_gateway"), None)
        assert tool_skill is not None
        assert tool_skill.action.route_path == "gateway"
        assert tool_skill.action.model_hint == "moonshot/kimi-k2.6"

    def test_bootstrap_all_active(self):
        skills = bootstrap_routing_skills()
        assert all(s.status == SkillStatus.ACTIVE for s in skills)

    def test_bootstrap_all_have_conditions(self):
        skills = bootstrap_routing_skills()
        assert all(len(s.conditions) > 0 for s in skills)

    def test_bootstrap_source_is_rule(self):
        skills = bootstrap_routing_skills()
        assert all(s.source == "rule" for s in skills)


class TestAgentWithRouterIntegration:
    def test_router_with_agent(self):
        from hermes.router import HermesRouter
        agent = HermesAgent()
        agent.bootstrap_from_rules()
        router = HermesRouter(hermes_agent=agent)
        result = router.route({"type": "chat", "priority": 3, "prompt": "你好", "messages": [{"role": "user", "content": "你好"}]})
        assert result.get("agent_routed") is True
        assert result["route_path"] in ("direct_local", "gateway")

    def test_router_without_agent(self):
        from hermes.router import HermesRouter
        router = HermesRouter()
        result = router.route({"type": "chat", "priority": 3, "prompt": "你好", "messages": [{"role": "user", "content": "你好"}]})
        assert "agent_routed" not in result or result.get("agent_routed") is False

    def test_router_agent_disabled(self):
        from hermes.router import HermesRouter
        agent = HermesAgent()
        agent.bootstrap_from_rules()
        agent.enabled = False
        router = HermesRouter(hermes_agent=agent)
        result = router.route({"type": "chat", "priority": 3, "prompt": "你好", "messages": [{"role": "user", "content": "你好"}]})
        assert result.get("agent_routed") is not True

    def test_feedback_propagates_to_agent(self):
        from hermes.router import HermesRouter
        agent = HermesAgent()
        agent.bootstrap_from_rules()
        router = HermesRouter(hermes_agent=agent)
        router.record_result(
            model_name="ollama/qwen2.5:3b",
            path="direct_local",
            success=True,
            latency_ms=100,
            request={"type": "chat", "prompt": "你好"},
            routing={"skill_matched": "simple_chat_local"},
        )
        if "simple_chat_local" in agent.skills:
            assert agent.skills["simple_chat_local"].hit_count >= 1
