import httpx
import json
import logging
import os
import time
from typing import Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

LLM_ENHANCER_ENABLED = os.getenv("HERMES_LLM_ENHANCER", "true").lower() in ("true", "1", "yes")
LLM_CLASSIFY_URL = os.getenv("HERMES_LLM_CLASSIFY_URL", "http://localhost:11434/v1/chat/completions")
LLM_CLASSIFY_MODEL = os.getenv("HERMES_LLM_CLASSIFY_MODEL", "ollama/qwen2.5:3b")
LLM_CLASSIFY_TIMEOUT = int(os.getenv("HERMES_LLM_CLASSIFY_TIMEOUT", "3"))
LLM_MIN_COMPLEXITY_FOR_ENHANCE = int(os.getenv("HERMES_LLM_MIN_COMPLEXITY", "10"))

CLASSIFY_SYSTEM_PROMPT = """你是一个请求路由分类器。分析用户的请求，输出JSON格式的分类结果。

输出格式（只输出JSON，不要解释）：
{
  "intent": "chat|code|analysis|tool_call|multi_step|creative|reasoning",
  "complexity": 1-10,
  "requires_local": true/false,
  "requires_tools": true/false,
  "key_concepts": ["概念1", "概念2"],
  "suggested_path": "direct_local|gateway|agent_chain|direct_cloud",
  "confidence": 0.0-1.0
}

分类规则：
- intent:
  - chat: 简单对话、问候、常识问答
  - code: 编写、修改、调试代码
  - analysis: 分析、比较、评估数据或信息
  - tool_call: 需要调用外部工具或API
  - multi_step: 需要多步骤规划与执行
  - creative: 创意写作、头脑风暴
  - reasoning: 逻辑推理、数学计算
- complexity: 1=最简单, 10=最复杂
- requires_local: 涉及隐私、敏感数据、本地文件时为true
- requires_tools: 需要调用外部工具/API时为true
- suggested_path: 基于分析推荐的路由路径
- confidence: 分类置信度"""


class LLMEnhancer:
    def __init__(self):
        self.enabled = LLM_ENHANCER_ENABLED
        self.classify_url = LLM_CLASSIFY_URL
        self.model = LLM_CLASSIFY_MODEL
        self.timeout = LLM_CLASSIFY_TIMEOUT
        self.min_complexity = LLM_MIN_COMPLEXITY_FOR_ENHANCE
        self._call_count = 0
        self._success_count = 0
        self._fail_count = 0
        self._total_latency_ms = 0
        self._last_error = None
        self._cache: Dict[str, dict] = {}
        self._cache_max = 200

        # 实例级共享 httpx.Client (连接池复用, 避免per-request创建开销)
        self._classify_client = httpx.Client(
            base_url=self._extract_base_url(self.classify_url),
            timeout=httpx.Timeout(connect=5.0, read=3.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )
        self._classify_url_path = self._extract_url_path(self.classify_url)

    @staticmethod
    def _extract_base_url(url: str) -> str:
        """从完整URL提取base_url (scheme+host+port), 如 http://localhost:11434/v1/chat/completions → http://localhost:11434"""
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base

    @staticmethod
    def _extract_url_path(url: str) -> str:
        """从完整URL提取路径部分, 如 http://localhost:11434/v1/chat/completions → /v1/chat/completions"""
        parsed = urlparse(url)
        return parsed.path or "/"

    def classify(self, request: Dict) -> Optional[Dict]:
        if not self.enabled:
            return None

        rule_score = request.get("_rule_complexity_score", 0)
        if rule_score < self.min_complexity:
            logger.debug(f"LLM enhancer skipped: rule_score={rule_score} < min={self.min_complexity}")
            return None

        cache_key = self._cache_key(request)
        if cache_key in self._cache:
            logger.debug(f"LLM enhancer cache hit: {cache_key[:32]}")
            return self._cache[cache_key]

        prompt = request.get("prompt", "")
        req_type = request.get("type", "chat")
        tools = request.get("tools", [])
        constraints = request.get("constraints") or {}

        user_message = f"请求类型: {req_type}\n提示词: {prompt[:500]}"
        if tools:
            tool_names = [t.get("function", {}).get("name", t.get("name", "?")) for t in tools]
            user_message += f"\n可用工具: {', '.join(tool_names)}"
        if constraints.get("require_local"):
            user_message += "\n约束: 需要本地执行"

        result = self._call_llm(user_message)

        if result:
            self._success_count += 1
            if len(self._cache) >= self._cache_max:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
            self._cache[cache_key] = result
        else:
            self._fail_count += 1

        self._call_count += 1
        return result

    def _call_llm(self, user_message: str) -> Optional[Dict]:
        start = time.time()
        try:
            payload = {
                "model": self.model.split("/")[-1] if "/" in self.model else self.model,
                "messages": [
                    {"role": "system", "content": CLASSIFY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.1,
                "max_tokens": 200,
                "stream": False,
            }

            resp = self._classify_client.post(self._classify_url_path, json=payload)
            resp.raise_for_status()
            data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            latency_ms = int((time.time() - start) * 1000)
            self._total_latency_ms += latency_ms

            result = self._parse_response(content)
            if result:
                result["_llm_latency_ms"] = latency_ms
                logger.info(f"LLM enhancer classified: intent={result.get('intent')}, "
                            f"complexity={result.get('complexity')}, "
                            f"path={result.get('suggested_path')}, "
                            f"confidence={result.get('confidence')}, "
                            f"latency={latency_ms}ms")
            return result

        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            self._total_latency_ms += latency_ms
            self._last_error = str(e)
            logger.warning(f"LLM enhancer failed (fallback to rules): {e}, latency={latency_ms}ms")
            return None

    def _parse_response(self, content: str) -> Optional[Dict]:
        try:
            json_str = content.strip()
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            result = json.loads(json_str)

            valid_intents = {"chat", "code", "analysis", "tool_call", "multi_step", "creative", "reasoning"}
            if result.get("intent") not in valid_intents:
                result["intent"] = "chat"

            complexity = result.get("complexity", 5)
            result["complexity"] = max(1, min(10, int(complexity)))

            result.setdefault("requires_local", False)
            result.setdefault("requires_tools", False)
            result.setdefault("key_concepts", [])
            result.setdefault("suggested_path", "gateway")
            result.setdefault("confidence", 0.5)

            confidence = result["confidence"]
            result["confidence"] = max(0.0, min(1.0, float(confidence)))

            return result

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"LLM enhancer parse failed: {e}, content={content[:100]}")
            return None

    def _cache_key(self, request: Dict) -> str:
        prompt = request.get("prompt", "")[:100]
        req_type = request.get("type", "chat")
        has_tools = bool(request.get("tools"))
        require_local = bool((request.get("constraints") or {}).get("require_local", False))
        return f"{req_type}:{has_tools}:{require_local}:{hash(prompt)}"

    def merge_scores(self, rule_score: Dict, llm_result: Dict) -> Dict:
        llm_complexity = llm_result.get("complexity", 5)
        llm_confidence = llm_result.get("confidence", 0.5)
        llm_intent = llm_result.get("intent", "chat")
        llm_suggested_path = llm_result.get("suggested_path", "gateway")

        rule_total = rule_score.get("total", 0)

        intent_complexity_map = {
            "chat": 5, "creative": 10, "reasoning": 15,
            "code": 25, "analysis": 20, "tool_call": 35, "multi_step": 45,
        }
        llm_score = intent_complexity_map.get(llm_intent, 10)

        rule_weight = 0.6
        llm_weight = 0.4 * llm_confidence
        total_weight = rule_weight + llm_weight

        merged_total = (rule_total * rule_weight + llm_score * llm_weight) / total_weight

        merged_breakdown = dict(rule_score.get("breakdown", {}))
        merged_breakdown["llm_intent"] = llm_intent
        merged_breakdown["llm_complexity"] = llm_complexity
        merged_breakdown["llm_confidence"] = round(llm_confidence, 3)
        merged_breakdown["llm_suggested_path"] = llm_suggested_path

        requires_local = llm_result.get("requires_local", False)
        requires_tools = llm_result.get("requires_tools", False)

        if requires_local and "constraints" not in merged_breakdown:
            merged_breakdown["llm_local_override"] = 10

        if requires_tools and "tool_calls" not in merged_breakdown:
            merged_breakdown["llm_tools_detected"] = 20

        llm_intent_score = round(llm_score * llm_confidence * 0.4, 1)
        if llm_intent_score > 0:
            merged_breakdown["llm_intent_boost"] = llm_intent_score

        merged_total = sum(v for k, v in merged_breakdown.items()
                           if k not in ("llm_intent", "llm_complexity", "llm_confidence", "llm_suggested_path"))

        return {
            "total": merged_total,
            "breakdown": merged_breakdown,
            "llm_enhanced": True,
            "llm_intent": llm_intent,
            "llm_suggested_path": llm_suggested_path,
            "llm_confidence": llm_confidence,
            "requires_local_override": requires_local,
            "requires_tools_override": requires_tools,
        }

    def get_stats(self) -> Dict:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "classify_url": self.classify_url,
            "timeout_seconds": self.timeout,
            "min_complexity_threshold": self.min_complexity,
            "call_count": self._call_count,
            "success_count": self._success_count,
            "fail_count": self._fail_count,
            "success_rate": round(self._success_count / self._call_count, 4) if self._call_count > 0 else 0,
            "avg_latency_ms": round(self._total_latency_ms / self._call_count) if self._call_count > 0 else 0,
            "cache_size": len(self._cache),
            "last_error": self._last_error,
        }
