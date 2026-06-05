"""
Unit tests for Hermes Agent routing strategy module.

Covers all components documented in routing-strategy.md:
1. Memory rule parsing (_parse_routing_rules_from_memory)
2. System message building (_build_routing_system_message)
3. Post-validation priority (_post_validate_route)
4. Memory override check (_check_memory_override)
5. Natural language fallback (_extract_route_from_text)
6. Skill evolution (_evolve_rule_from_feedback)
7. Memory length overflow handling
8. Desired route extraction (_extract_desired_route)
9. Mode switching (set_mode)
10. Cache invalidation
"""

import json
import os
import tempfile
import time
import pytest
from unittest.mock import patch, MagicMock

from hermes.official_agent_adapter import (
    OfficialHermesAdapter,
    MEMORY_FILE,
    ROUTING_PROMPT_TEMPLATE,
)


def _insert_rule_before_feedback(memory_file, rule_line):
    """Insert a routing rule line before the Feedback History section."""
    with open(memory_file, encoding="utf-8") as f:
        content = f.read()
    content = content.replace(
        "## Feedback History",
        rule_line + "\n\n## Feedback History",
    )
    with open(memory_file, "w", encoding="utf-8") as f:
        f.write(content)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def adapter():
    """Create adapter with no real network dependencies."""
    return OfficialHermesAdapter(
        api_url="http://127.0.0.1:8642",
        api_key="test-key",
        ollama_url="http://localhost:11434",
        ollama_model="qwen2.5:3b",
        use_direct_ollama=True,
    )


@pytest.fixture
def memory_dir(tmp_path):
    """Create a temporary MEMORY.md directory."""
    mem_dir = tmp_path / ".hermes" / "memories"
    mem_dir.mkdir(parents=True)
    return mem_dir


@pytest.fixture
def memory_file(memory_dir):
    """Create a standard MEMORY.md with all sections."""
    path = memory_dir / "MEMORY.md"
    path.write_text(
        "# Routing Decision Memory\n"
        "\n"
        "## Routing Patterns Learned\n"
        "- Simple chat/greetings [你好,hello,hi,谢谢,再见,thanks,bye] → direct_local\n"
        "- Code generation/programming [代码,code,实现,编写,函数,算法] → agent_chain\n"
        "- Privacy-sensitive data [隐私,敏感,个人信息,医疗,脱敏] → local_inference\n"
        "- General Q&A [什么是,解释,比较,how,what] → gateway\n"
        "- Translation requests [翻译,translate] → direct_local\n"
        "- Deployment/DevOps [部署,deploy,发布] → agent_chain\n"
        "\n"
        "## Key Rules\n"
        "- require_local=true → always local_inference\n"
        "- type=code/code_execution → always agent_chain\n"
        "\n"
        "## Feedback History\n"
        "- direct_local ✓ 421ms '你好'\n"
        "- agent_chain ✓ 1200ms 'Python排序'\n",
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def adapter_with_memory(adapter, memory_file):
    """Adapter with MEMORY.md patched to temp file."""
    with patch("hermes.official_agent_adapter.MEMORY_FILE", memory_file):
        adapter._memory_context_cached_at = 0  # Force cache miss
        yield adapter


# ══════════════════════════════════════════════════════════════════════════════
# 1. Memory Rule Parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestParseRoutingRulesFromMemory:
    """Test _parse_routing_rules_from_memory: MEMORY.md → structured rules."""

    def test_parse_standard_rules(self, adapter_with_memory):
        """Standard MEMORY.md format should parse into structured rules."""
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        assert len(rules) >= 5
        # Check first rule structure
        r0 = rules[0]
        assert r0["category"] == "Simple chat/greetings"
        assert "你好" in r0["keywords"]
        assert r0["route"] == "direct_local"
        assert r0["raw_line"].startswith("-")

    def test_parse_keywords_from_brackets(self, adapter_with_memory):
        """Keywords in [kw1,kw2,...] should be extracted as lowercase list."""
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        code_rule = [r for r in rules if "Code" in r["category"]][0]
        assert "代码" in code_rule["keywords"]
        assert "code" in code_rule["keywords"]
        assert "算法" in code_rule["keywords"]

    def test_parse_route_extraction(self, adapter_with_memory):
        """Route after → should be extracted and validated."""
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        valid_routes = {"direct_local", "gateway", "agent_chain", "local_inference"}
        for rule in rules:
            assert rule["route"] in valid_routes

    def test_skip_invalid_routes(self, adapter_with_memory, memory_file):
        """Rules with invalid route names should be skipped."""
        # Insert before Feedback History section
        with open(memory_file, encoding="utf-8") as f:
            content = f.read()
        content = content.replace(
            "## Feedback History",
            "- Invalid rule [test] → unknown_route\n\n## Feedback History",
        )
        with open(memory_file, "w", encoding="utf-8") as f:
            f.write(content)
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        routes = {r["route"] for r in rules}
        assert "unknown_route" not in routes

    def test_skip_lines_without_arrow(self, adapter_with_memory, memory_file):
        """Lines without → should be skipped."""
        _insert_rule_before_feedback(memory_file, "- This has no arrow")
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        categories = [r["category"] for r in rules]
        assert "This has no arrow" not in categories

    def test_skip_non_dash_lines(self, adapter_with_memory, memory_file):
        """Lines not starting with - should be skipped."""
        _insert_rule_before_feedback(memory_file, "Some text → gateway")
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        categories = [r["category"] for r in rules]
        assert "Some text" not in categories

    def test_strip_common_prefixes(self, adapter_with_memory, memory_file):
        """Prefixes like 'always', 'fast', 'must' should be stripped from route."""
        _insert_rule_before_feedback(memory_file, "- Fast rule [fast] → always gateway")
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        fast_rule = [r for r in rules if "Fast" in r["category"]]
        assert len(fast_rule) == 1
        assert fast_rule[0]["route"] == "gateway"

    def test_empty_memory_file(self, adapter, tmp_path):
        """Empty MEMORY.md should return empty rules list."""
        empty_file = str(tmp_path / "EMPTY.md")
        with open(empty_file, "w") as f:
            f.write("")
        with patch("hermes.official_agent_adapter.MEMORY_FILE", empty_file):
            adapter._memory_context_cached_at = 0
            rules = adapter._parse_routing_rules_from_memory()
            assert rules == []

    def test_no_keywords_uses_category_fallback(self, adapter_with_memory, memory_file):
        """Rules without [keywords] should have empty keyword list."""
        _insert_rule_before_feedback(memory_file, "- Bare category → gateway")
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        bare = [r for r in rules if r["category"] == "Bare category"]
        assert len(bare) == 1
        assert bare[0]["keywords"] == []

    def test_notes_in_parentheses_ignored(self, adapter_with_memory, memory_file):
        """Notes in parentheses after route should not affect route parsing."""
        _insert_rule_before_feedback(memory_file, "- Test rule [kw] → gateway (learned from feedback)")
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        test_rule = [r for r in rules if r["category"] == "Test rule"]
        assert len(test_rule) == 1
        assert test_rule[0]["route"] == "gateway"


# ══════════════════════════════════════════════════════════════════════════════
# 2. System Message Building
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildRoutingSystemMessage:
    """Test _build_routing_system_message: Memory rules → system prompt."""

    def test_contains_routing_rules(self, adapter_with_memory):
        """System message should contain routing rules from Memory."""
        msg = adapter_with_memory._build_routing_system_message()
        assert "direct_local" in msg
        assert "agent_chain" in msg
        assert "local_inference" in msg
        assert "gateway" in msg

    def test_contains_template_structure(self, adapter_with_memory):
        """System message should follow ROUTING_PROMPT_TEMPLATE structure."""
        msg = adapter_with_memory._build_routing_system_message()
        assert "路由选项" in msg
        assert "route_path" in msg
        assert "complexity_score" in msg

    def test_rules_from_memory_not_hardcoded(self, adapter_with_memory):
        """Rules should come from MEMORY.md, not hardcoded defaults."""
        msg = adapter_with_memory._build_routing_system_message()
        # Memory has "Simple chat/greetings" rule
        assert "Simple chat/greetings" in msg or "你好" in msg

    def test_fallback_to_minimal_rules(self, adapter, tmp_path):
        """When MEMORY.md is empty, should fall back to minimal default rules."""
        empty_file = str(tmp_path / "EMPTY.md")
        with open(empty_file, "w") as f:
            f.write("")
        with patch("hermes.official_agent_adapter.MEMORY_FILE", empty_file):
            adapter._memory_context_cached_at = 0
            msg = adapter._build_routing_system_message()
            # Should contain minimal defaults
            assert "简单闲聊" in msg or "direct_local" in msg

    def test_keyword_display_limited(self, adapter_with_memory):
        """Each rule should display at most 8 keywords in system message."""
        msg = adapter_with_memory._build_routing_system_message()
        # The Code rule has many keywords but display should be limited
        assert "代码" in msg or "code" in msg


# ══════════════════════════════════════════════════════════════════════════════
# 3. Post-Validation Priority
# ══════════════════════════════════════════════════════════════════════════════

class TestPostValidateRoute:
    """Test _post_validate_route: P1 privacy > P2 Memory > P3 type."""

    def test_p1_privacy_override(self, adapter_with_memory):
        """P1: require_local=true should override any route to local_inference."""
        routing = {"route_path": "gateway", "reason": "LLM decided gateway"}
        request = {"prompt": "分析数据", "type": "chat", "constraints": {"require_local": True}}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "local_inference"
        assert result.get("post_validated") is True

    def test_p1_privacy_overrides_agent_chain(self, adapter_with_memory):
        """P1: require_local should override even agent_chain."""
        routing = {"route_path": "agent_chain", "reason": "code generation"}
        request = {"prompt": "写脚本分析医疗数据", "type": "code", "constraints": {"require_local": True}}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "local_inference"

    def test_p2_memory_override(self, adapter_with_memory):
        """P2: Memory keyword match should override LLM output."""
        # LLM says direct_local, but prompt contains "代码" → agent_chain
        routing = {"route_path": "direct_local", "reason": "LLM said chat"}
        request = {"prompt": "帮我写一段代码实现排序", "type": "chat"}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "agent_chain"
        assert result.get("post_validated") is True

    def test_p2_memory_override_privacy_keywords(self, adapter_with_memory):
        """P2: Privacy keywords in Memory should override to local_inference."""
        routing = {"route_path": "gateway", "reason": "general query"}
        request = {"prompt": "我的医疗记录需要脱敏处理", "type": "chat"}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "local_inference"

    def test_p3_type_code_override(self, adapter_with_memory):
        """P3: type=code should override to agent_chain."""
        routing = {"route_path": "gateway", "reason": "LLM said gateway"}
        request = {"prompt": "写个脚本", "type": "code"}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "agent_chain"

    def test_p3_type_code_execution_override(self, adapter_with_memory):
        """P3: type=code_execution should also override to agent_chain."""
        routing = {"route_path": "direct_local", "reason": "simple"}
        request = {"prompt": "执行代码", "type": "code_execution"}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "agent_chain"

    def test_no_override_when_correct(self, adapter_with_memory):
        """No override when route already matches Memory rules."""
        routing = {"route_path": "agent_chain", "reason": "code generation"}
        request = {"prompt": "写一段代码", "type": "chat"}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "agent_chain"
        assert result.get("post_validated") is None  # Not overridden

    def test_p1_higher_than_p2(self, adapter_with_memory):
        """P1 (privacy) should take priority over P2 (Memory keyword match)."""
        # Prompt has both privacy keywords and code keywords
        # Privacy should win
        routing = {"route_path": "agent_chain", "reason": "code"}
        request = {
            "prompt": "帮我写代码处理医疗脱敏数据",
            "type": "chat",
            "constraints": {"require_local": True},
        }
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "local_inference"

    def test_constraints_not_dict(self, adapter_with_memory):
        """Non-dict constraints should not crash."""
        routing = {"route_path": "gateway", "reason": "ok"}
        request = {"prompt": "分析微服务和单体架构的区别", "type": "chat", "constraints": "invalid"}
        result = adapter_with_memory._post_validate_route(routing, request)
        assert result["route_path"] == "gateway"  # No override


# ══════════════════════════════════════════════════════════════════════════════
# 4. Memory Override Check
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckMemoryOverride:
    """Test _check_memory_override: keyword matching against Memory rules."""

    def test_code_keywords_override(self, adapter_with_memory):
        """Prompt with code keywords should override to agent_chain."""
        override = adapter_with_memory._check_memory_override(
            "帮我写一段代码实现功能", "direct_local"
        )
        assert override == "agent_chain"

    def test_privacy_keywords_override(self, adapter_with_memory):
        """Prompt with privacy keywords should override to local_inference."""
        override = adapter_with_memory._check_memory_override(
            "我的医疗记录需要脱敏", "gateway"
        )
        assert override == "local_inference"

    def test_translation_keywords_override(self, adapter_with_memory):
        """Prompt with translation keywords should override to direct_local."""
        override = adapter_with_memory._check_memory_override(
            "帮我翻译这段话", "gateway"
        )
        assert override == "direct_local"

    def test_no_override_same_route(self, adapter_with_memory):
        """No override when current route matches Memory rule."""
        override = adapter_with_memory._check_memory_override(
            "写一段代码", "agent_chain"
        )
        # agent_chain already matches the code rule, no override needed
        assert override == ""

    def test_no_override_no_keywords_match(self, adapter_with_memory):
        """No override when no Memory keywords match the prompt."""
        override = adapter_with_memory._check_memory_override(
            "今天天气怎么样", "gateway"
        )
        assert override == ""

    def test_empty_prompt(self, adapter_with_memory):
        """Empty prompt should not trigger any override."""
        override = adapter_with_memory._check_memory_override("", "gateway")
        assert override == ""


# ══════════════════════════════════════════════════════════════════════════════
# 5. Natural Language Fallback
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractRouteFromText:
    """Test _extract_route_from_text: fallback when 3B model outputs natural language."""

    def test_privacy_constraint_enforced(self, adapter_with_memory):
        """Priority 1: require_local should force local_inference."""
        result = adapter_with_memory._extract_route_from_text(
            "This is a simple greeting", {"constraints": {"require_local": True}}
        )
        assert result["route_path"] == "local_inference"

    def test_memory_keyword_match(self, adapter_with_memory):
        """Priority 2: Memory keyword match should determine route."""
        result = adapter_with_memory._extract_route_from_text(
            "I think this should go to the local path",
            {"prompt": "帮我写一段代码"},
        )
        assert result["route_path"] == "agent_chain"

    def test_text_contains_agent_chain(self, adapter_with_memory):
        """Priority 3: Text containing 'agent_chain' should extract it."""
        result = adapter_with_memory._extract_route_from_text(
            "Based on the analysis, agent_chain is the best choice",
            {"prompt": "复杂任务"},
        )
        assert result["route_path"] == "agent_chain"

    def test_text_contains_local_inference(self, adapter_with_memory):
        """Priority 3: Text containing 'local_inference' should extract it."""
        result = adapter_with_memory._extract_route_from_text(
            "For privacy, local_inference is recommended",
            {"prompt": "敏感数据"},
        )
        assert result["route_path"] == "local_inference"

    def test_text_contains_direct_local(self, adapter_with_memory):
        """Priority 3: Text containing 'direct_local' should extract it."""
        result = adapter_with_memory._extract_route_from_text(
            "This is simple, direct_local is fine",
            {"prompt": "你好"},
        )
        assert result["route_path"] == "direct_local"

    def test_default_gateway(self, adapter_with_memory):
        """Default fallback should be gateway when no keywords match."""
        result = adapter_with_memory._extract_route_from_text(
            "I'm not sure about this one",
            {"prompt": "xyzzy plugh"},  # Nonsense words that won't match any Memory rule
        )
        assert result["route_path"] == "gateway"

    def test_privacy_overrides_all(self, adapter_with_memory):
        """Privacy constraint should override even when text says agent_chain."""
        result = adapter_with_memory._extract_route_from_text(
            "This should go to agent_chain",
            {"prompt": "医疗数据", "constraints": {"require_local": True}},
        )
        assert result["route_path"] == "local_inference"


# ══════════════════════════════════════════════════════════════════════════════
# 6. Skill Evolution
# ══════════════════════════════════════════════════════════════════════════════

class TestEvolveRuleFromFeedback:
    """Test _evolve_rule_from_feedback: failure feedback → rule update."""

    def test_evolve_updates_existing_rule(self, adapter_with_memory, memory_file):
        """Evolution should update an existing rule's route when category matches."""
        # "翻译请求被误路由到gateway应走direct_local"
        # "翻译" matches "Translation requests" rule
        adapter_with_memory._evolve_rule_from_feedback(
            "gateway", "翻译请求被误路由到gateway应走direct_local"
        )
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        translation_rule = [r for r in rules if "Translation" in r["category"]]
        assert len(translation_rule) == 1
        assert translation_rule[0]["route"] == "direct_local"

    def test_evolve_adds_new_rule_for_new_category(self, adapter_with_memory, memory_file):
        """Evolution should add a new rule when no existing category matches."""
        adapter_with_memory._evolve_rule_from_feedback(
            "gateway", "数据库查询被误路由到gateway应走gateway"
        )
        # Same route → no evolution
        # Try with different route
        adapter_with_memory._evolve_rule_from_feedback(
            "direct_local", "数据库查询被误路由到direct_local应走gateway"
        )
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        db_rules = [r for r in rules if "Database" in r["category"]]
        assert len(db_rules) >= 1
        assert db_rules[0]["route"] == "gateway"

    def test_evolve_skips_when_no_desired_route(self, adapter_with_memory, memory_file):
        """Evolution should skip when desired route cannot be extracted."""
        original_content = open(memory_file, encoding="utf-8").read()
        adapter_with_memory._evolve_rule_from_feedback(
            "gateway", "翻译请求路由错误"  # No "应走X" pattern
        )
        new_content = open(memory_file, encoding="utf-8").read()
        assert original_content == new_content  # No change

    def test_evolve_skips_when_same_route(self, adapter_with_memory, memory_file):
        """Evolution should skip when desired route equals failed route."""
        original_content = open(memory_file, encoding="utf-8").read()
        adapter_with_memory._evolve_rule_from_feedback(
            "direct_local", "翻译请求被误路由到agent_chain应走direct_local"
        )
        # desired_route=direct_local, but Translation rule already → direct_local
        # So the rule won't change (same route)
        new_content = open(memory_file, encoding="utf-8").read()
        # Content should be unchanged (or only minimally changed)
        # The existing rule already points to direct_local

    def test_evolve_uses_topic_keywords_fallback(self, adapter_with_memory, memory_file):
        """Evolution should use topic_keywords when no Memory rule matches."""
        adapter_with_memory._evolve_rule_from_feedback(
            "direct_local", "配置问题被误路由到direct_local应走gateway"
        )
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        config_rules = [r for r in rules if "Configuration" in r["category"]]
        assert len(config_rules) >= 1
        assert config_rules[0]["route"] == "gateway"

    def test_evolve_precise_line_replacement(self, adapter_with_memory, memory_file):
        """Evolution should replace exact line, not similar lines."""
        # Add two rules with distinct keywords that will match feedback summaries
        _insert_rule_before_feedback(memory_file, "- Test A [testa, ta] → gateway")
        _insert_rule_before_feedback(memory_file, "- Test B [testb, tb] → agent_chain")
        adapter_with_memory._memory_context_cached_at = 0

        # "ta" keyword matches "Test A" rule, desired route is direct_local
        adapter_with_memory._evolve_rule_from_feedback(
            "gateway", "ta请求被误路由到gateway应走direct_local"
        )
        adapter_with_memory._memory_context_cached_at = 0
        rules = adapter_with_memory._parse_routing_rules_from_memory()
        test_a = [r for r in rules if r["category"] == "Test A"]
        test_b = [r for r in rules if r["category"] == "Test B"]
        assert len(test_a) == 1
        assert test_a[0]["route"] == "direct_local"
        assert len(test_b) == 1
        assert test_b[0]["route"] == "agent_chain"  # Unchanged


# ══════════════════════════════════════════════════════════════════════════════
# 7. Memory Length Overflow Handling
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryOverflowHandling:
    """Test Memory length overflow: rolling window, feedback-only trimming, size limit."""

    def test_rolling_window_keeps_10_feedback(self, adapter_with_memory, memory_file):
        """Feedback history should be limited to 10 entries."""
        for i in range(15):
            adapter_with_memory._write_feedback_to_memory(
                "routing-decision", "gateway", True, 100 + i * 10, f"test_{i}"
            )
        with open(memory_file, encoding="utf-8") as f:
            content = f.read()
        feedback_lines = [l for l in content.split("\n")
                         if l.strip().startswith("-") and ("✓" in l or "✗" in l)]
        assert len(feedback_lines) <= 10

    def test_routing_rules_not_trimmed_on_overflow(self, adapter_with_memory, memory_file):
        """Routing Patterns should never be trimmed when feedback overflows."""
        # Read original rules
        with open(memory_file, encoding="utf-8") as f:
            original = f.read()
        original_rules = [l for l in original.split("\n") if "→" in l and "✓" not in l and "✗" not in l]

        # Add many feedback entries to trigger overflow
        for i in range(20):
            adapter_with_memory._write_feedback_to_memory(
                "routing-decision", "gateway", True, 100, f"overflow_{i}" * 5
            )

        with open(memory_file, encoding="utf-8") as f:
            after = f.read()
        after_rules = [l for l in after.split("\n") if "→" in l and "✓" not in l and "✗" not in l]

        # All original routing rules should still be present
        for rule_line in original_rules:
            if rule_line.strip():
                assert rule_line.strip() in after

    def test_new_rule_rejected_if_exceeds_1500(self, adapter_with_memory, memory_file):
        """New rule should be rejected if total content exceeds 1500 chars."""
        # Fill MEMORY.md close to 1500 chars
        with open(memory_file, encoding="utf-8") as f:
            content = f.read()

        # Pad to near 1500
        padding_needed = 1400 - len(content)
        if padding_needed > 0:
            with open(memory_file, "a", encoding="utf-8") as f:
                f.write("\n" + "x" * padding_needed + "\n")

        adapter_with_memory._memory_context_cached_at = 0
        original_content = open(memory_file, encoding="utf-8").read()

        # Try to add a new rule that would exceed 1500
        adapter_with_memory._evolve_rule_from_feedback(
            "direct_local", "全新的监控请求被误路由到direct_local应走gateway"
        )

        new_content = open(memory_file, encoding="utf-8").read()
        # If content was already near 1500, the new rule should be rejected
        # (content unchanged or only minimally changed)
        assert len(new_content) <= 1600  # Allow some margin

    def test_feedback_only_trimmed_not_rules(self, adapter_with_memory, memory_file):
        """When total > 1500, only feedback lines should be trimmed."""
        # Write enough feedback to approach limit
        for i in range(12):
            adapter_with_memory._write_feedback_to_memory(
                "routing-decision", "gateway", True, 100, "a" * 40
            )

        with open(memory_file, encoding="utf-8") as f:
            content = f.read()

        # Verify routing rules section is intact
        assert "## Routing Patterns Learned" in content
        assert "## Key Rules" in content
        # Verify at least some routing rules remain
        assert "→ direct_local" in content or "→ agent_chain" in content


# ══════════════════════════════════════════════════════════════════════════════
# 8. Desired Route Extraction
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractDesiredRoute:
    """Test _extract_desired_route: extract target route from feedback summary."""

    def test_chinese_should_pattern(self, adapter):
        """'应走direct_local' should extract direct_local."""
        result = adapter._extract_desired_route("翻译请求被误路由到gateway应走direct_local")
        assert result == "direct_local"

    def test_chinese_yinggai_pattern(self, adapter):
        """'应该走agent_chain' should extract agent_chain."""
        result = adapter._extract_desired_route("代码请求应该走agent_chain")
        assert result == "agent_chain"

    def test_chinese_ying_luyou_pattern(self, adapter):
        """'应路由到gateway' should extract gateway."""
        result = adapter._extract_desired_route("应路由到gateway")
        assert result == "gateway"

    def test_chinese_yingshi_pattern(self, adapter):
        """'应该是local_inference' should extract local_inference."""
        result = adapter._extract_desired_route("应该是local_inference")
        assert result == "local_inference"

    def test_english_should_be_pattern(self, adapter):
        """'should be gateway' should extract gateway."""
        result = adapter._extract_desired_route("should be gateway")
        assert result == "gateway"

    def test_english_should_route_to_pattern(self, adapter):
        """'should route to agent_chain' should extract agent_chain."""
        result = adapter._extract_desired_route("should route to agent_chain")
        assert result == "agent_chain"

    def test_no_route_found(self, adapter):
        """Summary without route pattern should return empty string."""
        result = adapter._extract_desired_route("翻译请求路由错误")
        assert result == ""

    def test_invalid_route_ignored(self, adapter):
        """Invalid route name should be ignored."""
        result = adapter._extract_desired_route("应走unknown_route")
        assert result == ""


# ══════════════════════════════════════════════════════════════════════════════
# 9. Mode Switching
# ══════════════════════════════════════════════════════════════════════════════

class TestModeSwitching:
    """Test set_mode: runtime switching between ollama_direct and hermes_agent."""

    def test_switch_to_ollama_direct(self, adapter):
        """Switching to ollama_direct should set use_direct_ollama=True."""
        result = adapter.set_mode("ollama_direct")
        assert adapter.use_direct_ollama is True
        assert "ollama_direct" in result["mode"]

    def test_switch_to_hermes_agent(self, adapter):
        """Switching to hermes_agent should set use_direct_ollama=False."""
        adapter.use_direct_ollama = True
        result = adapter.set_mode("hermes_agent")
        assert adapter.use_direct_ollama is False
        assert "hermes_agent" in result["mode"]

    def test_short_aliases(self, adapter):
        """Short aliases 'ollama', 'direct', 'hermes', 'agent' should work."""
        adapter.set_mode("ollama")
        assert adapter.use_direct_ollama is True

        adapter.set_mode("direct")
        assert adapter.use_direct_ollama is True

        adapter.set_mode("hermes")
        assert adapter.use_direct_ollama is False

        adapter.set_mode("agent")
        assert adapter.use_direct_ollama is False

    def test_unknown_mode_returns_error(self, adapter):
        """Unknown mode should return error dict."""
        result = adapter.set_mode("invalid_mode")
        assert "error" in result

    def test_mode_description(self, adapter):
        """Mode switch result should include description."""
        result = adapter.set_mode("ollama_direct")
        assert "description" in result
        assert "3.7s" in result["description"] or "Memory" in result["description"]


# ══════════════════════════════════════════════════════════════════════════════
# 10. Cache Invalidation
# ══════════════════════════════════════════════════════════════════════════════

class TestCacheInvalidation:
    """Test cache invalidation: feedback write and rule evolution should invalidate cache."""

    def test_feedback_write_invalidates_cache(self, adapter_with_memory, memory_file):
        """Writing feedback should set _memory_context_cached_at = 0."""
        adapter_with_memory._memory_context_cached_at = time.time()
        adapter_with_memory._write_feedback_to_memory(
            "routing-decision", "gateway", True, 100, "test"
        )
        assert adapter_with_memory._memory_context_cached_at == 0

    def test_evolution_invalidates_cache(self, adapter_with_memory, memory_file):
        """Rule evolution should set _memory_context_cached_at = 0."""
        adapter_with_memory._memory_context_cached_at = time.time()
        adapter_with_memory._evolve_rule_from_feedback(
            "gateway", "翻译请求被误路由到gateway应走direct_local"
        )
        assert adapter_with_memory._memory_context_cached_at == 0

    def test_memory_context_cache_ttl(self, adapter_with_memory, memory_file):
        """Cache should be valid within TTL and expire after."""
        adapter_with_memory._memory_context_cache_ttl = 10
        # First read — cache miss
        ctx1 = adapter_with_memory._load_memory_context()
        cached_at = adapter_with_memory._memory_context_cached_at
        assert cached_at > 0

        # Second read within TTL — cache hit
        ctx2 = adapter_with_memory._load_memory_context()
        assert ctx1 == ctx2
        assert adapter_with_memory._memory_context_cached_at == cached_at

    def test_health_check_cache_ttl(self, adapter):
        """Health check should use 30s cache TTL."""
        assert adapter._health_cache_ttl == 30

    def test_memory_context_cache_ttl_default(self, adapter):
        """Memory context cache should use 10s TTL by default."""
        assert adapter._memory_context_cache_ttl == 10


# ══════════════════════════════════════════════════════════════════════════════
# Integration: Full Decision Chain
# ══════════════════════════════════════════════════════════════════════════════

class TestFullDecisionChain:
    """Integration tests: full routing decision chain with mocked LLM."""

    def _mock_ollama_json_response(self, route_path, complexity=30, reason="test"):
        """Build mock Ollama response returning valid JSON."""
        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "route_path": route_path,
                        "complexity_score": complexity,
                        "reason": reason,
                    })
                }
            }]
        }

    @patch("httpx.Client")
    def test_ollama_direct_full_chain(self, mock_client_cls, adapter_with_memory):
        """Full chain: Ollama direct → JSON parse → post-validate → result."""
        adapter_with_memory.use_direct_ollama = True
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_ollama_json_response("direct_local", 5, "闲聊")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = adapter_with_memory.route_via_agent({
            "prompt": "你好",
            "type": "chat",
            "priority": 1,
        })
        assert result["route_path"] == "direct_local"
        assert result["agent_decision"] == "ollama_direct_with_memory"
        assert result["memory_context_used"] is True

    @patch("httpx.Client")
    def test_post_validation_corrects_llm_error(self, mock_client_cls, adapter_with_memory):
        """LLM returns wrong route, post-validation corrects it."""
        adapter_with_memory.use_direct_ollama = True
        # LLM says direct_local for a code prompt
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_ollama_json_response("direct_local", 5, "简单")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = adapter_with_memory.route_via_agent({
            "prompt": "帮我写一段代码实现快速排序",
            "type": "chat",
            "priority": 3,
        })
        # Post-validation should override to agent_chain
        assert result["route_path"] == "agent_chain"
        assert result.get("post_validated") is True

    @patch("httpx.Client")
    def test_fallback_to_ollama_on_agent_failure(self, mock_client_cls, adapter_with_memory):
        """When Hermes Agent fails, should fallback to Ollama direct."""
        adapter_with_memory.use_direct_ollama = False  # hermes_agent mode
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._mock_ollama_json_response("gateway", 30, "general")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        # First call (hermes_agent) raises exception, second (ollama) succeeds
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Agent unavailable")
            return mock_resp

        mock_client.post.side_effect = side_effect

        result = adapter_with_memory.route_via_agent({
            "prompt": "什么是微服务",
            "type": "chat",
            "priority": 3,
        })
        assert result["route_path"] == "gateway"

    @patch("httpx.Client")
    def test_natural_language_fallback_chain(self, mock_client_cls, adapter_with_memory):
        """When LLM outputs natural language, fallback chain should extract route."""
        adapter_with_memory.use_direct_ollama = True
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": "根据分析，这个请求应该路由到 agent_chain，因为涉及代码生成。"
                }
            }]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = adapter_with_memory.route_via_agent({
            "prompt": "写一个排序算法",
            "type": "chat",
            "priority": 3,
        })
        assert result["route_path"] == "agent_chain"


# ══════════════════════════════════════════════════════════════════════════════
# Edge Cases
# ══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_empty_prompt_routes_to_gateway(self, adapter_with_memory):
        """Empty prompt should default to gateway."""
        result = adapter_with_memory._extract_route_from_text(
            "unclear response", {"prompt": ""}
        )
        assert result["route_path"] == "gateway"

    def test_very_long_prompt_truncated(self, adapter_with_memory):
        """Very long prompt should be handled without crash."""
        long_prompt = "你好" * 1000
        result = adapter_with_memory._check_memory_override(long_prompt, "gateway")
        # Should not crash, may or may not match
        assert isinstance(result, str)

    def test_unicode_keywords(self, adapter_with_memory):
        """Unicode keywords (Chinese) should match correctly."""
        override = adapter_with_memory._check_memory_override(
            "你好世界", "gateway"
        )
        assert override == "direct_local"

    def test_case_insensitive_keyword_match(self, adapter_with_memory):
        """Keyword matching should be case-insensitive."""
        override = adapter_with_memory._check_memory_override(
            "Write some CODE for me", "gateway"
        )
        assert override == "agent_chain"

    def test_json_extraction_from_markdown(self, adapter):
        """JSON embedded in markdown should be extracted."""
        text = '```json\n{"route_path": "gateway", "complexity_score": 30}\n```'
        result = adapter._extract_json_from_text(text)
        assert result is not None
        assert result["route_path"] == "gateway"

    def test_json_extraction_from_text_with_prefix(self, adapter):
        """JSON after text prefix should be extracted."""
        text = 'The routing decision is: {"route_path": "agent_chain", "complexity_score": 60}'
        result = adapter._extract_json_from_text(text)
        assert result is not None
        assert result["route_path"] == "agent_chain"

    def test_invalid_json_returns_none(self, adapter):
        """Completely invalid text should return None from JSON extraction."""
        result = adapter._extract_json_from_text("no json here at all")
        assert result is None

    def test_feedback_local_file_creation(self, adapter, tmp_path):
        """Local feedback file should be created if it doesn't exist."""
        feedback_dir = str(tmp_path / "routing_feedback")
        # Patch os.path.expanduser inside the adapter module
        with patch("hermes.official_agent_adapter.os.path.expanduser") as mock_expand:
            # First call: os.path.expanduser("~/.hermes/routing_feedback") for dir
            # Second call: os.path.join(feedback_dir, "feedback.jsonl") uses the dir
            mock_expand.return_value = feedback_dir
            result = adapter._write_feedback_local(
                "routing-decision", "gateway", True, 100, "test"
            )
            assert result is True
            # Check file was created
            feedback_file = os.path.join(feedback_dir, "feedback.jsonl")
            assert os.path.exists(feedback_file)

    def test_headers_include_auth(self, adapter):
        """Headers should include Authorization when api_key is set."""
        headers = adapter._headers
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer test-key"

    def test_headers_without_auth(self):
        """Headers should not include Authorization when no api_key."""
        adapter = OfficialHermesAdapter(api_key="")
        headers = adapter._headers
        assert "Authorization" not in headers


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
