import json
import time
from unittest.mock import patch, MagicMock

import pytest

from hermes.llm_enhancer import LLMEnhancer


def _make_request(prompt="test prompt", req_type="chat", rule_score=20, tools=None, constraints=None):
    req = {"prompt": prompt, "type": req_type, "_rule_complexity_score": rule_score}
    if tools:
        req["tools"] = tools
    if constraints:
        req["constraints"] = constraints
    return req


def _mock_llm_response(intent="chat", complexity=5, requires_local=False,
                        requires_tools=False, key_concepts=None,
                        suggested_path="gateway", confidence=0.8):
    return {
        "intent": intent,
        "complexity": complexity,
        "requires_local": requires_local,
        "requires_tools": requires_tools,
        "key_concepts": key_concepts or [],
        "suggested_path": suggested_path,
        "confidence": confidence,
    }


def _mock_httpx_response(content_dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    content = json.dumps(content_dict)
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return resp


class TestClassifyTriggerConditions:
    def test_disabled_returns_none(self):
        enhancer = LLMEnhancer()
        enhancer.enabled = False
        req = _make_request(rule_score=50)
        assert enhancer.classify(req) is None

    def test_low_complexity_skipped(self):
        enhancer = LLMEnhancer()
        enhancer.enabled = True
        enhancer.min_complexity = 10
        req = _make_request(rule_score=5)
        assert enhancer.classify(req) is None

    def test_below_threshold_not_called(self):
        enhancer = LLMEnhancer()
        enhancer.min_complexity = 15
        req = _make_request(rule_score=14)
        with patch.object(enhancer, '_call_llm') as mock_llm:
            result = enhancer.classify(req)
            mock_llm.assert_not_called()
        assert result is None

    def test_at_threshold_triggers(self):
        enhancer = LLMEnhancer()
        enhancer.min_complexity = 10
        req = _make_request(rule_score=10)
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result is not None
        assert result["intent"] == "chat"

    def test_above_threshold_triggers(self):
        enhancer = LLMEnhancer()
        enhancer.min_complexity = 10
        req = _make_request(rule_score=50)
        mock_result = _mock_llm_response(intent="multi_step")
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result is not None
        assert result["intent"] == "multi_step"

    def test_missing_rule_score_treated_as_zero(self):
        enhancer = LLMEnhancer()
        enhancer.min_complexity = 10
        req = {"prompt": "test", "type": "chat"}
        with patch.object(enhancer, '_call_llm') as mock_llm:
            result = enhancer.classify(req)
            mock_llm.assert_not_called()
        assert result is None


class TestClassifyNormalFlow:
    def test_simple_chat(self):
        enhancer = LLMEnhancer()
        req = _make_request(prompt="你好", req_type="chat", rule_score=20)
        mock_result = _mock_llm_response(intent="chat", complexity=2, suggested_path="direct_local")
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result["intent"] == "chat"
        assert result["complexity"] == 2
        assert result["suggested_path"] == "direct_local"

    def test_code_request(self):
        enhancer = LLMEnhancer()
        req = _make_request(prompt="写一个排序算法", req_type="chat", rule_score=25)
        mock_result = _mock_llm_response(intent="code", complexity=5, suggested_path="gateway")
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result["intent"] == "code"

    def test_tool_call_request(self):
        enhancer = LLMEnhancer()
        tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
        req = _make_request(prompt="搜索新闻", req_type="tool_call", rule_score=35, tools=tools)
        mock_result = _mock_llm_response(intent="tool_call", requires_tools=True, suggested_path="gateway")
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result["requires_tools"] is True

    def test_privacy_request(self):
        enhancer = LLMEnhancer()
        req = _make_request(
            prompt="处理敏感数据", rule_score=20,
            constraints={"require_local": True}
        )
        mock_result = _mock_llm_response(intent="code", requires_local=True, suggested_path="direct_local")
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result["requires_local"] is True

    def test_multi_step_request(self):
        enhancer = LLMEnhancer()
        req = _make_request(prompt="设计多步骤自动化方案", rule_score=40)
        mock_result = _mock_llm_response(intent="multi_step", complexity=8, suggested_path="agent_chain", confidence=0.9)
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result["intent"] == "multi_step"
        assert result["complexity"] == 8
        assert result["confidence"] == 0.9

    def test_success_count_incremented(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            enhancer.classify(req)
        assert enhancer._success_count == 1
        assert enhancer._call_count == 1

    def test_llm_latency_recorded(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        mock_result = _mock_llm_response()
        mock_result["_llm_latency_ms"] = 150
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result["_llm_latency_ms"] == 150


class TestDegradation:
    def test_llm_timeout_returns_none(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=None):
            result = enhancer.classify(req)
        assert result is None

    def test_llm_error_increments_fail_count(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=None):
            enhancer.classify(req)
        assert enhancer._fail_count == 1
        assert enhancer._call_count == 1
        assert enhancer._success_count == 0

    def test_consecutive_failures_dont_crash(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        for _ in range(5):
            with patch.object(enhancer, '_call_llm', return_value=None):
                result = enhancer.classify(req)
            assert result is None
        assert enhancer._fail_count == 5
        assert enhancer._call_count == 5

    def test_success_after_failure(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=None):
            enhancer.classify(req)
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            result = enhancer.classify(req)
        assert result is not None
        assert enhancer._fail_count == 1
        assert enhancer._success_count == 1

    def test_call_llm_connection_error(self):
        enhancer = LLMEnhancer()
        with patch('httpx.Client') as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_instance)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_instance.post.side_effect = ConnectionError("Connection refused")
            result = enhancer._call_llm("test message")
        assert result is None
        assert enhancer._last_error is not None

    def test_call_llm_http_error(self):
        enhancer = LLMEnhancer()
        with patch('httpx.Client') as mock_client:
            mock_instance = MagicMock()
            mock_client.return_value.__enter__ = MagicMock(return_value=mock_instance)
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_instance.post.return_value.status_code = 500
            mock_instance.post.return_value.raise_for_status.side_effect = Exception("Internal Server Error")
            result = enhancer._call_llm("test message")
        assert result is None

    def test_stats_reflect_mixed_results(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=None):
            enhancer.classify(req)
            enhancer.classify(req)
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            enhancer.classify(req)
        stats = enhancer.get_stats()
        assert stats["call_count"] == 3
        assert stats["success_count"] == 1
        assert stats["fail_count"] == 2
        assert stats["success_rate"] == round(1 / 3, 4)


class TestResponseParsing:
    def test_valid_json(self):
        enhancer = LLMEnhancer()
        content = json.dumps(_mock_llm_response(intent="code", complexity=7))
        result = enhancer._parse_response(content)
        assert result is not None
        assert result["intent"] == "code"
        assert result["complexity"] == 7

    def test_json_with_markdown_fences(self):
        enhancer = LLMEnhancer()
        inner = json.dumps(_mock_llm_response(intent="analysis"))
        content = f"```json\n{inner}\n```"
        result = enhancer._parse_response(content)
        assert result is not None
        assert result["intent"] == "analysis"

    def test_json_with_plain_fences(self):
        enhancer = LLMEnhancer()
        inner = json.dumps(_mock_llm_response(intent="reasoning"))
        content = f"```\n{inner}\n```"
        result = enhancer._parse_response(content)
        assert result is not None
        assert result["intent"] == "reasoning"

    def test_json_with_extra_whitespace(self):
        enhancer = LLMEnhancer()
        content = "  \n  " + json.dumps(_mock_llm_response()) + "  \n  "
        result = enhancer._parse_response(content)
        assert result is not None

    def test_invalid_json_returns_none(self):
        enhancer = LLMEnhancer()
        result = enhancer._parse_response("not json at all")
        assert result is None

    def test_empty_string_returns_none(self):
        enhancer = LLMEnhancer()
        result = enhancer._parse_response("")
        assert result is None

    def test_partial_json_returns_none(self):
        enhancer = LLMEnhancer()
        result = enhancer._parse_response('{"intent": "code"')
        assert result is None

    def test_invalid_intent_defaulted_to_chat(self):
        enhancer = LLMEnhancer()
        data = _mock_llm_response()
        data["intent"] = "unknown_intent"
        result = enhancer._parse_response(json.dumps(data))
        assert result["intent"] == "chat"

    def test_complexity_clamped_to_range(self):
        enhancer = LLMEnhancer()
        data_high = _mock_llm_response(complexity=99)
        result = enhancer._parse_response(json.dumps(data_high))
        assert result["complexity"] == 10

        data_low = _mock_llm_response(complexity=-5)
        result = enhancer._parse_response(json.dumps(data_low))
        assert result["complexity"] == 1

    def test_confidence_clamped_to_range(self):
        enhancer = LLMEnhancer()
        data_high = _mock_llm_response(confidence=1.5)
        result = enhancer._parse_response(json.dumps(data_high))
        assert result["confidence"] == 1.0

        data_low = _mock_llm_response(confidence=-0.3)
        result = enhancer._parse_response(json.dumps(data_low))
        assert result["confidence"] == 0.0

    def test_missing_fields_get_defaults(self):
        enhancer = LLMEnhancer()
        content = json.dumps({"intent": "chat", "complexity": 3})
        result = enhancer._parse_response(content)
        assert result is not None
        assert result["requires_local"] is False
        assert result["requires_tools"] is False
        assert result["key_concepts"] == []
        assert result["suggested_path"] == "gateway"
        assert result["confidence"] == 0.5


class TestCaching:
    def test_cache_hit_avoids_llm_call(self):
        enhancer = LLMEnhancer()
        req = _make_request(prompt="cache test", rule_score=20)
        mock_result = _mock_llm_response(intent="chat")
        with patch.object(enhancer, '_call_llm', return_value=mock_result) as mock_llm:
            r1 = enhancer.classify(req)
            r2 = enhancer.classify(req)
        assert mock_llm.call_count == 1
        assert r1 == r2

    def test_different_prompts_different_cache_keys(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="hello", rule_score=20)
        req2 = _make_request(prompt="world", rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=_mock_llm_response()) as mock_llm:
            enhancer.classify(req1)
            enhancer.classify(req2)
        assert mock_llm.call_count == 2

    def test_different_types_different_cache_keys(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="test", req_type="chat", rule_score=20)
        req2 = _make_request(prompt="test", req_type="tool_call", rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=_mock_llm_response()) as mock_llm:
            enhancer.classify(req1)
            enhancer.classify(req2)
        assert mock_llm.call_count == 2

    def test_tools_presence_affects_cache(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="test", rule_score=20)
        req2 = _make_request(prompt="test", rule_score=20, tools=[{"type": "function", "function": {"name": "x"}}])
        with patch.object(enhancer, '_call_llm', return_value=_mock_llm_response()) as mock_llm:
            enhancer.classify(req1)
            enhancer.classify(req2)
        assert mock_llm.call_count == 2

    def test_require_local_affects_cache(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="test", rule_score=20)
        req2 = _make_request(prompt="test", rule_score=20, constraints={"require_local": True})
        with patch.object(enhancer, '_call_llm', return_value=_mock_llm_response()) as mock_llm:
            enhancer.classify(req1)
            enhancer.classify(req2)
        assert mock_llm.call_count == 2

    def test_cache_eviction_at_max(self):
        enhancer = LLMEnhancer()
        enhancer._cache_max = 3
        results = []
        for i in range(5):
            req = _make_request(prompt=f"prompt_{i}", rule_score=20)
            mock_result = _mock_llm_response(intent="chat")
            with patch.object(enhancer, '_call_llm', return_value=mock_result):
                r = enhancer.classify(req)
                results.append(r)
        assert len(enhancer._cache) == 3
        assert enhancer._success_count == 5

    def test_failed_call_not_cached(self):
        enhancer = LLMEnhancer()
        req = _make_request(prompt="fail test", rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=None):
            r1 = enhancer.classify(req)
        assert r1 is None
        assert len(enhancer._cache) == 0
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result):
            r2 = enhancer.classify(req)
        assert r2 is not None
        assert len(enhancer._cache) == 1

    def test_cache_stats_reflect_size(self):
        enhancer = LLMEnhancer()
        for i in range(3):
            req = _make_request(prompt=f"prompt_{i}", rule_score=20)
            with patch.object(enhancer, '_call_llm', return_value=_mock_llm_response()):
                enhancer.classify(req)
        stats = enhancer.get_stats()
        assert stats["cache_size"] == 3


class TestMergeScores:
    def test_basic_merge(self):
        enhancer = LLMEnhancer()
        rule_score = {"total": 30, "breakdown": {"request_type": 5, "keywords": 20, "priority": 5}}
        llm_result = _mock_llm_response(intent="code", complexity=6, suggested_path="gateway", confidence=0.8)
        merged = enhancer.merge_scores(rule_score, llm_result)
        assert merged["llm_enhanced"] is True
        assert merged["llm_intent"] == "code"
        assert merged["llm_confidence"] == 0.8
        assert merged["total"] > 0

    def test_requires_local_adds_override(self):
        enhancer = LLMEnhancer()
        rule_score = {"total": 20, "breakdown": {"request_type": 5, "keywords": 10, "priority": 5}}
        llm_result = _mock_llm_response(requires_local=True, suggested_path="direct_local")
        merged = enhancer.merge_scores(rule_score, llm_result)
        assert merged["requires_local_override"] is True
        assert "llm_local_override" in merged["breakdown"]

    def test_requires_tools_adds_override(self):
        enhancer = LLMEnhancer()
        rule_score = {"total": 20, "breakdown": {"request_type": 5, "keywords": 10, "priority": 5}}
        llm_result = _mock_llm_response(requires_tools=True, suggested_path="gateway")
        merged = enhancer.merge_scores(rule_score, llm_result)
        assert merged["requires_tools_override"] is True
        assert "llm_tools_detected" in merged["breakdown"]

    def test_no_override_when_not_needed(self):
        enhancer = LLMEnhancer()
        rule_score = {"total": 20, "breakdown": {"request_type": 5, "keywords": 10, "priority": 5}}
        llm_result = _mock_llm_response(requires_local=False, requires_tools=False)
        merged = enhancer.merge_scores(rule_score, llm_result)
        assert "llm_local_override" not in merged["breakdown"]
        assert "llm_tools_detected" not in merged["breakdown"]

    def test_high_confidence_llm_increases_score(self):
        enhancer = LLMEnhancer()
        rule_score = {"total": 10, "breakdown": {"request_type": 5, "keywords": 0, "priority": 5}}
        llm_low = _mock_llm_response(intent="multi_step", confidence=0.3)
        llm_high = _mock_llm_response(intent="multi_step", confidence=0.9)
        merged_low = enhancer.merge_scores(rule_score, llm_low)
        merged_high = enhancer.merge_scores(rule_score, llm_high)
        assert merged_high["total"] > merged_low["total"]
        assert "llm_intent_boost" in merged_high["breakdown"]
        assert merged_high["breakdown"]["llm_intent_boost"] > merged_low["breakdown"]["llm_intent_boost"]

    def test_intent_complexity_mapping(self):
        enhancer = LLMEnhancer()
        rule_score = {"total": 10, "breakdown": {"request_type": 5, "priority": 5}}
        intents_and_expected = [
            ("chat", "low"),
            ("code", "high"),
            ("multi_step", "high"),
            ("tool_call", "high"),
            ("analysis", "mid"),
        ]
        results = {}
        for intent, _ in intents_and_expected:
            llm_result = _mock_llm_response(intent=intent, confidence=0.9)
            merged = enhancer.merge_scores(rule_score, llm_result)
            results[intent] = merged["total"]

        assert results["multi_step"] > results["code"]
        assert results["code"] > results["analysis"]
        assert results["analysis"] > results["chat"]
        assert results["tool_call"] > results["chat"]

    def test_breakdown_preserves_rule_fields(self):
        enhancer = LLMEnhancer()
        rule_score = {"total": 30, "breakdown": {"request_type": 5, "keywords": 20, "priority": 5}}
        llm_result = _mock_llm_response(intent="code")
        merged = enhancer.merge_scores(rule_score, llm_result)
        assert "request_type" in merged["breakdown"]
        assert "keywords" in merged["breakdown"]
        assert "priority" in merged["breakdown"]
        assert "llm_intent" in merged["breakdown"]
        assert "llm_complexity" in merged["breakdown"]
        assert "llm_confidence" in merged["breakdown"]
        assert "llm_suggested_path" in merged["breakdown"]


class TestCacheKey:
    def test_same_request_same_key(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="hello", req_type="chat", rule_score=20)
        req2 = _make_request(prompt="hello", req_type="chat", rule_score=20)
        assert enhancer._cache_key(req1) == enhancer._cache_key(req2)

    def test_different_prompt_different_key(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="hello", rule_score=20)
        req2 = _make_request(prompt="world", rule_score=20)
        assert enhancer._cache_key(req1) != enhancer._cache_key(req2)

    def test_rule_score_not_in_key(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="hello", rule_score=10)
        req2 = _make_request(prompt="hello", rule_score=50)
        assert enhancer._cache_key(req1) == enhancer._cache_key(req2)

    def test_prompt_truncated_in_key(self):
        enhancer = LLMEnhancer()
        req1 = _make_request(prompt="a" * 200, rule_score=20)
        req2 = _make_request(prompt="a" * 100 + "b" * 100, rule_score=20)
        assert enhancer._cache_key(req1) == enhancer._cache_key(req2)


class TestGetStats:
    def test_initial_stats(self):
        enhancer = LLMEnhancer()
        stats = enhancer.get_stats()
        assert stats["call_count"] == 0
        assert stats["success_count"] == 0
        assert stats["fail_count"] == 0
        assert stats["success_rate"] == 0
        assert stats["avg_latency_ms"] == 0
        assert stats["cache_size"] == 0
        assert stats["last_error"] is None

    def test_stats_after_operations(self):
        enhancer = LLMEnhancer()
        req = _make_request(rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=_mock_llm_response()):
            enhancer.classify(req)
        req2 = _make_request(prompt="different", rule_score=20)
        with patch.object(enhancer, '_call_llm', return_value=None):
            enhancer.classify(req2)
        stats = enhancer.get_stats()
        assert stats["call_count"] == 2
        assert stats["success_count"] == 1
        assert stats["fail_count"] == 1
        assert stats["success_rate"] == 0.5
        assert stats["cache_size"] == 1


class TestCallLlmIntegration:
    def test_successful_httpx_call(self):
        enhancer = LLMEnhancer()
        mock_resp = _mock_httpx_response(_mock_llm_response(intent="code", complexity=7))
        with patch('httpx.Client') as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_instance)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_instance.post.return_value = mock_resp
            result = enhancer._call_llm("test message")
        assert result is not None
        assert result["intent"] == "code"
        assert result["complexity"] == 7
        assert "_llm_latency_ms" in result

    def test_model_name_stripped(self):
        enhancer = LLMEnhancer()
        enhancer.model = "ollama/qwen2.5:3b"
        mock_resp = _mock_httpx_response(_mock_llm_response())
        with patch('httpx.Client') as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_instance)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_instance.post.return_value = mock_resp
            enhancer._call_llm("test")
            call_args = mock_instance.post.call_args
            payload = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
            assert payload["model"] == "qwen2.5:3b"

    def test_timeout_propagated(self):
        enhancer = LLMEnhancer()
        enhancer.timeout = 5
        mock_resp = _mock_httpx_response(_mock_llm_response())
        with patch('httpx.Client') as mock_client_cls:
            mock_instance = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_instance)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_instance.post.return_value = mock_resp
            enhancer._call_llm("test")
            mock_client_cls.assert_called_once()
            call_kwargs = mock_client_cls.call_args[1]
            assert call_kwargs["timeout"] == 5.0


class TestUserMessageConstruction:
    def test_basic_message(self):
        enhancer = LLMEnhancer()
        req = _make_request(prompt="hello world", req_type="chat", rule_score=20)
        cache_key = enhancer._cache_key(req)
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result) as mock_llm:
            enhancer.classify(req)
            call_args = mock_llm.call_args[0][0]
        assert "请求类型: chat" in call_args
        assert "提示词: hello world" in call_args

    def test_tools_included_in_message(self):
        enhancer = LLMEnhancer()
        tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
        req = _make_request(prompt="search news", req_type="tool_call", rule_score=35, tools=tools)
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result) as mock_llm:
            enhancer.classify(req)
            call_args = mock_llm.call_args[0][0]
        assert "可用工具: search" in call_args

    def test_require_local_in_message(self):
        enhancer = LLMEnhancer()
        req = _make_request(prompt="private data", rule_score=20, constraints={"require_local": True})
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result) as mock_llm:
            enhancer.classify(req)
            call_args = mock_llm.call_args[0][0]
        assert "约束: 需要本地执行" in call_args

    def test_long_prompt_truncated(self):
        enhancer = LLMEnhancer()
        long_prompt = "x" * 1000
        req = _make_request(prompt=long_prompt, rule_score=20)
        mock_result = _mock_llm_response()
        with patch.object(enhancer, '_call_llm', return_value=mock_result) as mock_llm:
            enhancer.classify(req)
            call_args = mock_llm.call_args[0][0]
        assert len(call_args) < len(long_prompt) + 50
