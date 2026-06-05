"""
Unit tests for Official Hermes Agent Adapter integration.
"""

import json
import pytest
from unittest.mock import patch, MagicMock

from hermes.official_agent_adapter import OfficialHermesAdapter
from hermes.router import HermesRouter, RoutePath


# ── OfficialHermesAdapter Tests ──────────────────────────────────────────────

class TestOfficialHermesAdapter:

    def setup_method(self):
        self.adapter = OfficialHermesAdapter(
            api_url="http://127.0.0.1:8642",
            api_key="test-key",
            hermes_router_url="http://localhost:8082",
        )

    def test_init(self):
        assert self.adapter.api_url == "http://127.0.0.1:8642"
        assert self.adapter.api_key == "test-key"
        assert self.adapter.hermes_router_url == "http://localhost:8082"

    def test_headers_with_key(self):
        headers = self.adapter._headers
        assert headers["Authorization"] == "Bearer test-key"
        assert headers["Content-Type"] == "application/json"

    def test_headers_without_key(self):
        adapter = OfficialHermesAdapter(api_url="http://localhost:8642")
        assert "Authorization" not in adapter._headers

    @patch("httpx.Client")
    def test_health_check_ok(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.health_check()
        assert result["status"] == "ok"

    @patch("httpx.Client")
    def test_health_check_unavailable(self, mock_client_cls):
        mock_client_cls.side_effect = Exception("Connection refused")
        result = self.adapter.health_check()
        assert result["status"] == "unavailable"

    @patch("httpx.Client")
    def test_route_via_agent_success(self, mock_client_cls):
        agent_response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "route_path": "agent_chain",
                        "complexity_score": 55,
                        "selected_model": "moonshot/kimi-k2.6",
                        "skill_matched": "complex_task_route",
                        "agent_decision": "use_skill",
                        "agent_confidence": 0.9,
                        "reason": "Complex multi-step task",
                    })
                }
            }]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = agent_response
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        request = {"prompt": "请帮我制定一个完整的营销计划", "type": "chat", "priority": 3}
        result = self.adapter.route_via_agent(request)

        assert result["route_path"] == "agent_chain"
        assert result["official_agent_routed"] is True
        assert result["agent_confidence"] == 0.9
        assert "agent_llm_latency_ms" in result

    @patch("httpx.Client")
    def test_route_via_agent_timeout(self, mock_client_cls):
        import httpx
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = httpx.TimeoutException("timeout")
        mock_client_cls.return_value = mock_client

        request = {"prompt": "test", "type": "chat"}
        result = self.adapter.route_via_agent(request)

        assert result["route_path"] == "gateway"
        assert result["official_agent_routed"] is False
        assert "timeout" in result["reason"].lower()

    @patch("httpx.Client")
    def test_route_via_agent_unparseable_response(self, mock_client_cls):
        agent_response = {
            "choices": [{
                "message": {
                    "content": "The route should be direct_local based on simplicity."
                }
            }]
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = agent_response
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        request = {"prompt": "hello", "type": "chat"}
        result = self.adapter.route_via_agent(request)

        # Should fallback to gateway when response is not parseable
        assert result["route_path"] == "gateway"

    @patch("httpx.Client")
    def test_record_feedback_via_memory_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = self.adapter.record_feedback_via_memory(
            skill_name="privacy_local_route",
            route_path="direct_local",
            success=True,
            latency_ms=200,
        )
        assert result is True

    @patch("httpx.Client")
    def test_record_feedback_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = Exception("Connection refused")
        mock_client_cls.return_value = mock_client

        result = self.adapter.record_feedback_via_memory(
            skill_name="test",
            route_path="gateway",
            success=False,
            latency_ms=5000,
        )
        assert result is False

    def test_extract_json_from_text(self):
        text = 'Here is the result: {"route_path": "gateway", "score": 30} done.'
        result = self.adapter._extract_json_from_text(text)
        assert result is not None
        assert result["route_path"] == "gateway"

    def test_extract_json_from_text_no_json(self):
        result = self.adapter._extract_json_from_text("No JSON here")
        assert result is None

    @patch("httpx.Client")
    def test_get_stats_available(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        stats = self.adapter.get_stats()
        assert stats["available"] is True
        assert stats["api_url"] == "http://127.0.0.1:8642"

    @patch("httpx.Client")
    def test_get_stats_unavailable(self, mock_client_cls):
        mock_client_cls.side_effect = Exception("Connection refused")
        stats = self.adapter.get_stats()
        assert stats["available"] is False


# ── Router Integration Tests ─────────────────────────────────────────────────

class TestRouterWithOfficialAgent:

    def setup_method(self):
        self.router = HermesRouter()

    def test_router_without_official_agent(self):
        assert self.router.official_agent is None

    def test_router_with_official_agent(self):
        adapter = OfficialHermesAdapter(api_url="http://localhost:8642")
        router = HermesRouter(official_agent=adapter)
        assert router.official_agent is not None
        assert router.official_agent.api_url == "http://localhost:8642"

    @patch.object(OfficialHermesAdapter, "get_stats")
    @patch.object(OfficialHermesAdapter, "route_via_agent")
    def test_route_prefers_official_agent(self, mock_route, mock_stats):
        mock_stats.return_value = {"available": True, "api_url": "http://localhost:8642"}
        mock_route.return_value = {
            "route_path": "agent_chain",
            "selected_model": "moonshot/kimi-k2.6",
            "skill_matched": "complex_task_route",
            "agent_decision": "use_skill",
            "agent_confidence": 0.85,
            "reason": "Complex task requiring multi-step execution",
            "official_agent_routed": True,
        }

        adapter = OfficialHermesAdapter(api_url="http://localhost:8642")
        router = HermesRouter(official_agent=adapter)

        request = {
            "type": "chat",
            "priority": 3,
            "prompt": "请帮我制定一个完整的营销计划并执行",
        }
        result = router.route(request)

        assert result["route_path"] == "agent_chain"
        assert result["official_agent_routed"] is True
        assert result["agent_routed"] is True
        assert "[OfficialAgent]" in result["reason"]

    @patch.object(OfficialHermesAdapter, "get_stats")
    def test_route_falls_back_when_official_unavailable(self, mock_stats):
        mock_stats.return_value = {"available": False}

        adapter = OfficialHermesAdapter(api_url="http://localhost:8642")
        router = HermesRouter(official_agent=adapter)

        request = {
            "type": "chat",
            "priority": 3,
            "prompt": "hello",
        }
        result = router.route(request)

        # Should fall back to normal routing (not official agent)
        assert result.get("official_agent_routed") is not True

    @patch.object(OfficialHermesAdapter, "record_feedback_via_memory")
    def test_record_result_propagates_to_official_agent(self, mock_feedback):
        mock_feedback.return_value = True

        adapter = OfficialHermesAdapter(api_url="http://localhost:8642")
        router = HermesRouter(official_agent=adapter)

        routing = {
            "route_path": "gateway",
            "official_agent_routed": True,
            "skill_matched": "moderate_task_route",
        }
        router.record_result(
            model_name="moonshot/kimi-k2.6",
            path="gateway",
            success=True,
            latency_ms=1500,
            request={"prompt": "分析一下市场趋势"},
            routing=routing,
        )

        mock_feedback.assert_called_once()

    def test_record_result_no_official_agent(self):
        router = HermesRouter()
        # Should not raise
        router.record_result(
            model_name="ollama/qwen2.5:3b",
            path="direct_local",
            success=True,
            latency_ms=200,
            request={"prompt": "hello"},
            routing={"route_path": "direct_local"},
        )


# ── Plugin Schema Tests ──────────────────────────────────────────────────────

class TestPluginSchemas:
    """Test that the plugin tool schemas are valid."""

    def test_route_request_schema(self):
        import sys
        sys.path.insert(0, "/Users/yangxu/.hermes/plugins/intelligent-routing")
        from schemas import ROUTE_REQUEST

        assert ROUTE_REQUEST["name"] == "route_request"
        assert "prompt" in ROUTE_REQUEST["parameters"]["properties"]
        assert "request_type" in ROUTE_REQUEST["parameters"]["properties"]
        assert ROUTE_REQUEST["parameters"]["required"] == ["prompt"]

    def test_record_routing_feedback_schema(self):
        import sys
        sys.path.insert(0, "/Users/yangxu/.hermes/plugins/intelligent-routing")
        from schemas import RECORD_ROUTING_FEEDBACK

        assert RECORD_ROUTING_FEEDBACK["name"] == "record_routing_feedback"
        assert "route_path" in RECORD_ROUTING_FEEDBACK["parameters"]["properties"]
        assert "success" in RECORD_ROUTING_FEEDBACK["parameters"]["properties"]
        assert "route_path" in RECORD_ROUTING_FEEDBACK["parameters"]["required"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
