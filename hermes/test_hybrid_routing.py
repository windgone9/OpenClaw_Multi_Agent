"""
Unit tests for hybrid routing scenarios.

Covers the 4 core routing patterns validated with Ollama 3B:
1. Code generation (with/without chat mixing) → agent_chain
2. Simple chat → direct_local
3. Privacy-sensitive → local_inference
4. Complex multi-step → agent_chain

Plus forced-override logic and edge cases.
"""

import json
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

from hermes.official_agent_adapter import OfficialHermesAdapter
from hermes.router import HermesRouter, RoutePath


# ── Helper ───────────────────────────────────────────────────────────────────

def _mock_ollama_response(route_path, complexity_score=30, reason=""):
    """Build a mock Ollama /v1/chat/completions response."""
    return {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "route_path": route_path,
                    "complexity_score": complexity_score,
                    "reason": reason,
                })
            }
        }]
    }


def _make_adapter():
    return OfficialHermesAdapter(
        api_url="http://127.0.0.1:8642",
        api_key="test-key",
        ollama_url="http://localhost:11434",
        ollama_model="qwen2.5:3b",
    )


def _make_router_with_adapter():
    """Create a router with official agent, mocking health check as available."""
    adapter = _make_adapter()
    router = HermesRouter(official_agent=adapter)
    return router


def _patch_adapter_available():
    """Patch get_stats to return available=True so router uses official agent."""
    return patch.object(
        OfficialHermesAdapter, "get_stats",
        return_value={"available": True, "api_url": "http://127.0.0.1:8642",
                      "mode": "hybrid_ollama_memory", "ollama_model": "qwen2.5:3b",
                      "feedback_queue_size": 0}
    )


# ── Scenario 1: Code Generation → agent_chain ───────────────────────────────

class TestCodeGenerationRouting:
    """Code generation requests must always route to agent_chain."""

    def setup_method(self):
        self.adapter = _make_adapter()

    @patch("httpx.Client")
    def test_pure_code_request(self, mock_client_cls):
        """'请用Python实现一个高效的LRU缓存' → agent_chain"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 55, "编程实现")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.route_via_agent({
            "prompt": "请用Python实现一个高效的LRU缓存",
            "type": "chat",
            "priority": 3,
        })
        assert result["route_path"] == "agent_chain"

    @patch("httpx.Client")
    def test_code_with_chat_mixing(self, mock_client_cls):
        """'实现一个线程安全的单例模式...心情不错哈哈' → agent_chain

        The prompt rule forces code keyword detection even when mixed with chat.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 50, "包含编程关键词")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.route_via_agent({
            "prompt": "实现一个线程安全的单例模式，用Python写。另外今天心情不错哈哈",
            "type": "chat",
            "priority": 3,
        })
        assert result["route_path"] == "agent_chain"

    @patch("httpx.Client")
    def test_chat_then_code(self, mock_client_cls):
        """'你好...帮我写一个Python快速排序算法' → agent_chain"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 55, "编程指令")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.route_via_agent({
            "prompt": "你好啊，今天天气不错！对了，帮我写一个Python快速排序算法",
            "type": "chat",
            "priority": 3,
        })
        assert result["route_path"] == "agent_chain"

    @patch("httpx.Client")
    def test_type_code_forces_agent_chain(self, mock_client_cls):
        """type=code should force agent_chain regardless of Ollama response.

        Tests the forced override in router._route_via_official_agent.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("direct_local", 10, "简单")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        router = _make_router_with_adapter()
        with _patch_adapter_available():
            result = router.route({
                "prompt": "写个hello world",
                "type": "code",
                "priority": 3,
            })
        assert result["route_path"] == "agent_chain"


# ── Scenario 2: Simple Chat → direct_local ──────────────────────────────────

class TestSimpleChatRouting:
    """Simple chat/greetings should route to direct_local."""

    def setup_method(self):
        self.adapter = _make_adapter()

    @patch("httpx.Client")
    def test_greeting(self, mock_client_cls):
        """'你好' → direct_local"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("direct_local", 5, "简单闲聊")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.route_via_agent({
            "prompt": "你好",
            "type": "chat",
            "priority": 1,
        })
        assert result["route_path"] == "direct_local"

    @patch("httpx.Client")
    def test_casual_conversation(self, mock_client_cls):
        """'你好啊，最近怎么样？天气真好' → direct_local"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("direct_local", 5, "纯闲聊")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.route_via_agent({
            "prompt": "你好啊，最近怎么样？天气真好",
            "type": "chat",
            "priority": 1,
        })
        assert result["route_path"] == "direct_local"

    @patch("httpx.Client")
    def test_write_letter_not_code(self, mock_client_cls):
        """'我写了一封信给朋友' → direct_local (not agent_chain).

        The word '写' in a non-code context should not trigger code routing.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("direct_local", 5, "纯闲聊无技术内容")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.route_via_agent({
            "prompt": "我写了一封信给朋友，聊聊最近的生活",
            "type": "chat",
            "priority": 1,
        })
        assert result["route_path"] == "direct_local"


# ── Scenario 3: Privacy-Sensitive → local_inference ─────────────────────────

class TestPrivacyRouting:
    """Privacy-sensitive requests must route to local_inference."""

    @patch("httpx.Client")
    def test_require_local_forces_local_inference(self, mock_client_cls):
        """constraints.require_local=true → local_inference (forced override)."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("gateway", 30, "一般查询")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        router = _make_router_with_adapter()
        with _patch_adapter_available():
            result = router.route({
                "prompt": "分析我的个人医疗数据",
                "type": "analysis",
                "priority": 3,
                "constraints": {"require_local": True},
            })
        assert result["route_path"] == "local_inference"
        assert "require_local" in result["reason"]

    @patch("httpx.Client")
    def test_privacy_code_mixed(self, mock_client_cls):
        """Privacy + code: require_local takes priority over code→agent_chain."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 55, "代码生成")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        router = _make_router_with_adapter()
        with _patch_adapter_available():
            result = router.route({
                "prompt": "帮我分析本地的用户数据文件，写个脚本统计用户行为",
                "type": "code",
                "priority": 3,
                "constraints": {"require_local": True},
            })
        # require_local override takes priority
        assert result["route_path"] == "local_inference"

    def test_route_path_enum_has_local_inference(self):
        """RoutePath enum must include LOCAL_INFERENCE."""
        assert hasattr(RoutePath, "LOCAL_INFERENCE")
        assert RoutePath.LOCAL_INFERENCE.value == "local_inference"


# ── Scenario 4: Complex Multi-Step → agent_chain ────────────────────────────

class TestComplexRouting:
    """Complex multi-step requests should route to agent_chain."""

    def setup_method(self):
        self.adapter = _make_adapter()

    @patch("httpx.Client")
    def test_marketing_plan(self, mock_client_cls):
        """'制定营销计划' → agent_chain"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 60, "复杂多步规划")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.route_via_agent({
            "prompt": "帮我制定一个完整的营销计划，包括市场分析、目标客户、推广策略",
            "type": "planning",
            "priority": 3,
        })
        assert result["route_path"] == "agent_chain"

    @patch("httpx.Client")
    def test_high_complexity_via_official_agent(self, mock_client_cls):
        """Complex request routed via official agent → agent_chain."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 65, "复杂多步任务")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        router = _make_router_with_adapter()
        with _patch_adapter_available():
            result = router.route({
                "prompt": "请自主执行一个完整的项目：从需求分析到代码实现到测试部署",
                "type": "chat",
                "priority": 5,
            })
        assert result["route_path"] == "agent_chain"


# ── Feedback & Memory Tests ──────────────────────────────────────────────────

class TestFeedbackMemory:
    """Test that feedback is written to local file and MEMORY.md."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.tmpdir = tempfile.mkdtemp()

    def test_local_feedback_write(self):
        """Feedback should be written to local JSONL file."""
        with patch.object(self.adapter, "_write_feedback_local", return_value=True):
            with patch.object(self.adapter, "_ensure_memory_writer"):
                result = self.adapter.record_feedback_via_memory(
                    skill_name="routing-decision",
                    route_path="direct_local",
                    success=True,
                    latency_ms=3000,
                    request_summary="你好",
                )
                assert result is True

    def test_memory_md_write(self):
        """Feedback should be written to MEMORY.md with rolling window."""
        memories_dir = os.path.join(self.tmpdir, "memories")
        os.makedirs(memories_dir, exist_ok=True)
        memory_file = os.path.join(memories_dir, "MEMORY.md")
        with open(memory_file, "w") as f:
            f.write("# Routing Decision Memory\n\n## Feedback History\n- old entry\n")

        # Patch the exact memory file path used in _write_feedback_to_memory
        with patch("hermes.official_agent_adapter.os.path.expanduser",
                   return_value=memory_file):
            result = self.adapter._write_feedback_to_memory(
                skill_name="routing-decision",
                route_path="agent_chain",
                success=True,
                latency_ms=5000,
                request_summary="LRU缓存",
            )
            assert result is True

        with open(memory_file) as f:
            content = f.read()
        assert "agent_chain" in content
        assert "✓" in content

    def test_memory_md_rolling_window(self):
        """MEMORY.md should keep only last 10 feedback entries."""
        memories_dir = os.path.join(self.tmpdir, "memories")
        os.makedirs(memories_dir, exist_ok=True)
        memory_file = os.path.join(memories_dir, "MEMORY.md")
        lines = [f"- entry_{i} ✓ {i*100}ms" for i in range(12)]
        with open(memory_file, "w") as f:
            f.write("# Routing Decision Memory\n\n## Feedback History\n" + "\n".join(lines) + "\n")

        with patch("hermes.official_agent_adapter.os.path.expanduser",
                   return_value=memory_file):
            self.adapter._write_feedback_to_memory(
                skill_name="routing-decision",
                route_path="direct_local",
                success=True,
                latency_ms=100,
                request_summary="test",
            )

        with open(memory_file) as f:
            content = f.read()
        feedback_lines = [l for l in content.split("\n") if l.strip().startswith("-")]
        assert len(feedback_lines) <= 11  # 10 kept + 1 new

    def test_feedback_queue_is_async(self):
        """record_feedback_via_memory should return immediately (non-blocking)."""
        with patch.object(self.adapter, "_write_feedback_local", return_value=True):
            with patch.object(self.adapter, "_ensure_memory_writer"):
                import time
                start = time.time()
                self.adapter.record_feedback_via_memory(
                    skill_name="test", route_path="gateway",
                    success=True, latency_ms=100,
                )
                elapsed = time.time() - start
                # Should return in < 0.1s (not blocking on Memory write)
                assert elapsed < 0.1


# ── Forced Override Priority Tests ───────────────────────────────────────────

class TestForcedOverrides:
    """Test that forced overrides in router._route_via_official_agent take priority."""

    @patch("httpx.Client")
    def test_code_override_priority(self, mock_client_cls):
        """type=code forces agent_chain even if Ollama says direct_local."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("direct_local", 5, "简单")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        router = _make_router_with_adapter()
        with _patch_adapter_available():
            result = router.route({"prompt": "写个hello world", "type": "code", "priority": 3})
        assert result["route_path"] == "agent_chain"

    @patch("httpx.Client")
    def test_privacy_override_priority(self, mock_client_cls):
        """require_local forces local_inference even if Ollama says agent_chain."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 55, "代码生成")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        router = _make_router_with_adapter()
        with _patch_adapter_available():
            result = router.route({
                "prompt": "分析本地数据",
                "type": "chat",
                "priority": 3,
                "constraints": {"require_local": True},
            })
        assert result["route_path"] == "local_inference"

    @patch("httpx.Client")
    def test_privacy_overrides_code(self, mock_client_cls):
        """require_local + type=code: privacy wins."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = _mock_ollama_response("agent_chain", 55, "代码")
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        router = _make_router_with_adapter()
        with _patch_adapter_available():
            result = router.route({
                "prompt": "写脚本分析本地数据",
                "type": "code",
                "priority": 3,
                "constraints": {"require_local": True},
            })
        # Privacy takes absolute priority
        assert result["route_path"] == "local_inference"


# ── Hybrid Mode Architecture Tests ──────────────────────────────────────────

class TestHybridMode:
    """Test the hybrid mode: Ollama routing + Memory feedback."""

    def test_route_via_agent_uses_ollama(self):
        """In hybrid mode, route_via_agent should call Ollama directly."""
        adapter = _make_adapter()
        assert adapter.use_direct_ollama is True

    def test_decision_type_is_ollama_hybrid(self):
        """Routing decision should be marked as 'ollama_hybrid'."""
        adapter = _make_adapter()
        with patch("httpx.Client") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.json.return_value = _mock_ollama_response("direct_local", 5, "闲聊")
            mock_resp.raise_for_status = MagicMock()
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.route_via_agent({"prompt": "你好", "type": "chat"})
            assert result["agent_decision"] == "ollama_hybrid"

    def test_skill_matched_is_routing_decision(self):
        """All hybrid routing should reference the routing-decision skill."""
        adapter = _make_adapter()
        with patch("httpx.Client") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.json.return_value = _mock_ollama_response("gateway", 30, "查询")
            mock_resp.raise_for_status = MagicMock()
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.route_via_agent({"prompt": "什么是微服务", "type": "chat"})
            assert result["skill_matched"] == "routing-decision"

    def test_invalid_route_path_falls_back_to_gateway(self):
        """If Ollama returns an invalid route_path, fall back to gateway."""
        adapter = _make_adapter()
        with patch("httpx.Client") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.json.return_value = _mock_ollama_response("invalid_path", 30, "错误")
            mock_resp.raise_for_status = MagicMock()
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            result = adapter.route_via_agent({"prompt": "test", "type": "chat"})
            assert result["route_path"] == "gateway"

    def test_stats_show_hybrid_mode(self):
        """get_stats should report hybrid_ollama_memory mode."""
        adapter = _make_adapter()
        with patch("httpx.Client") as mock_client_cls:
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"status": "ok"}
            mock_resp.raise_for_status = MagicMock()
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.get.return_value = mock_resp
            mock_client_cls.return_value = mock_client

            stats = adapter.get_stats()
            assert stats["mode"] == "hybrid_ollama_memory"
            assert stats["ollama_model"] == "qwen2.5:3b"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
