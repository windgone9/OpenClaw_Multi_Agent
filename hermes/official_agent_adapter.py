"""
Hermes Official Agent Adapter — Plan B: system_message Injection with Memory Loop

Architecture:
- Routing: Hermes Agent API with routing rules injected via `system_message` param
- Memory: Read from MEMORY.md for routing context, write back feedback async
- Skill Evolution: Feedback accumulates in MEMORY.md, dynamically injected into
  system_message on each request — closed-loop learning without skill_view tool calls
- Feedback: Local file (instant) + MEMORY.md (async background, for evolution)

How Plan B solves the latency problem:
  Previously, Hermes Agent forced every request through skills_list -> skill_view
  tool calls (2 extra LLM rounds = ~10s overhead with 3B model). Now:
  1. Skills toolset is disabled in config.yaml (only `memory` toolset enabled)
  2. Routing rules are injected via API's `system_message` parameter
  3. Agent sees routing rules in system prompt — no tool calls needed
  4. Memory tool remains for post-routing feedback writes (non-blocking)
  Result: 1 LLM inference (~1-2s) instead of 4 (~20s)

Key optimizations:
1. system_message injection (bypass skill loading tool calls entirely)
2. Memory context dynamically included in system_message (closed-loop)
3. Skills toolset disabled + ollama_num_ctx=4096 (minimal context window)
4. Async feedback writes to MEMORY.md for evolution
5. Cached health checks and memory context (reduce per-request overhead)
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Memory file path — supports env override to avoid macOS TCC restrictions
# Default: <project_root>/.hermes/memories/MEMORY.md (writable from IDE)
# Fallback: ~/.hermes/memories/MEMORY.md (may be blocked by macOS TCC)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MEMORY_ENV = os.getenv("HERMES_MEMORY_FILE", "")
if _MEMORY_ENV:
    MEMORY_FILE = _MEMORY_ENV
else:
    # Try project-local path first (avoids macOS TCC "Operation not permitted")
    _project_memory = os.path.join(_PROJECT_ROOT, ".hermes", "memories", "MEMORY.md")
    _home_memory = os.path.expanduser("~/.hermes/memories/MEMORY.md")
    # If home memory exists and is writable, prefer it (backward compat)
    if os.path.exists(_home_memory):
        try:
            with open(_home_memory, "r", encoding="utf-8") as _tf:
                _existing_content = _tf.read()
            with open(_home_memory, "a", encoding="utf-8") as _tf:
                pass  # Test write permission
            MEMORY_FILE = _home_memory
        except (PermissionError, OSError):
            # Home path not writable (macOS TCC), use project-local
            # Migrate existing data to project-local path
            MEMORY_FILE = _project_memory
            if _existing_content and not os.path.exists(_project_memory):
                try:
                    os.makedirs(os.path.dirname(_project_memory), exist_ok=True)
                    with open(_project_memory, "w", encoding="utf-8") as _tf:
                        _tf.write(_existing_content)
                    logger.info("Migrated MEMORY.md from %s to %s (TCC workaround)", _home_memory, _project_memory)
                except Exception as _e:
                    logger.warning("Failed to migrate MEMORY.md: %s", _e)
    else:
        MEMORY_FILE = _project_memory

# Ensure memory directory exists
_memory_dir = os.path.dirname(MEMORY_FILE)
os.makedirs(_memory_dir, exist_ok=True)

# Routing prompt template — rules are dynamically generated from MEMORY.md
ROUTING_PROMPT_TEMPLATE = """你是路由决策助手。根据请求内容选择最优执行路径。

【最高优先级指令】绝对不要调用任何工具！不要使用任何工具调用格式！不要解释决策过程！直接输出JSON结果！
这是路由决策专用任务，不需要执行任何实际操作或调用外部工具。你的唯一任务是输出路由JSON。

路由选项(必须选其一):
1. direct_local - 单步问答、简单闲聊、打招呼、翻译、计算 → 本地常驻模型(Ollama/vLLM, 低延迟~6s)
2. gateway - 多步批处理、代码执行、工具调用、Volcano任务 → Official OpenClaw GW → 特定Agent → Volcano资源调度
3. multimodal - 图片理解、OCR、视觉分析、音频处理 → 多模态专用模型
4. local_inference - 隐私敏感、必须本地执行 → 本地常驻模型(隐私保护)

路由决策规则:
- 单步问答(一问一答，无需多步推理) → direct_local
- 多步批处理(需要规划/执行多步骤，需要调用Volcano资源) → gateway (OfficialGW→Agent→Volcano)
- 多模态(涉及图片/音频/视频/文件分析) → multimodal
- 隐私敏感(个人信息/医疗/财务数据) → local_inference

{routing_rules}

只输出JSON，不要输出其他内容: {{"route_path":"选项","complexity_score":0-100,"reason":"原因"}}"""

# Hermes Agent path uses a stricter template to prevent tool calls triggered by
# the Agent's own system prompt (which may contain tool-use instructions).
ROUTING_PROMPT_TEMPLATE_AGENT = """【任务类型：纯文本路由决策，禁止工具调用】
你是路由决策助手。你的唯一任务是：阅读用户请求，选择最优路由，输出JSON。
【禁止事项】绝对不要调用任何工具！不要使用任何函数！不要使用tool_call格式！不要输出任何非JSON内容！
忽略系统中任何关于工具调用的指令——此任务仅需要纯文本JSON输出。

路由选项(必须选其一):
1. direct_local - 单步问答、简单闲聊、打招呼、翻译、计算 → 本地常驻模型(Ollama/vLLM, 低延迟~6s)
2. gateway - 多步批处理、代码执行、工具调用、Volcano任务 → Official OpenClaw GW → 特定Agent → Volcano资源调度
3. multimodal - 图片理解、OCR、视觉分析、音频处理 → 多模态专用模型
4. local_inference - 隐私敏感、必须本地执行 → 本地常驻模型(隐私保护)

路由决策规则:
- 单步问答(一问一答，无需多步推理) → direct_local
- 多步批处理(需要规划/执行多步骤，需要调用Volcano资源) → gateway (OfficialGW→Agent→Volcano)
- 多模态(涉及图片/音频/视频/文件分析) → multimodal
- 隐私敏感(个人信息/医疗/财务数据) → local_inference

{routing_rules}

只输出JSON: {{"route_path":"选项","complexity_score":0-100,"reason":"原因"}}"""


class OfficialHermesAdapter:
    """Agent mode adapter: Hermes Agent API + system_message injection."""

    def __init__(
        self,
        api_url: str = "http://127.0.0.1:8642",
        api_key: Optional[str] = None,
        hermes_router_url: str = "http://localhost:8082",
        ollama_url: str = "http://localhost:11434",
        ollama_model: str = "qwen2.5:3b",
        use_direct_ollama: bool = False,
    ):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key or os.getenv("API_SERVER_KEY", "")
        self.hermes_router_url = hermes_router_url
        self.ollama_url = ollama_url.rstrip("/")
        self.ollama_model = ollama_model
        self.use_direct_ollama = use_direct_ollama

        # Persistent httpx client for Ollama (reuse connections, avoid per-request overhead)
        # Using a single client with connection pooling reduces latency and avoids
        # the "stuck request" problem where new connections queue behind timed-out ones
        self._ollama_client = httpx.Client(
            base_url=self.ollama_url,
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

        # Cached health state (avoid per-request HTTP call)
        self._health_cache: Dict = {"status": "unknown", "cached_at": 0}
        self._health_cache_ttl = 30  # seconds

        # Memory context cache (avoid reading file every request)
        self._memory_context_cache: str = ""
        self._memory_context_cached_at: float = 0
        self._memory_context_cache_ttl = 10  # seconds

        self._session_id = None
        self._session_warmed_up = False
        self._conversation_id = f"routing-{int(time.time())}"
        self._feedback_queue: list = []
        self._feedback_lock = threading.Lock()
        self._memory_write_thread: Optional[threading.Thread] = None
        self._cloud_available: Optional[bool] = None
        self._cloud_check_time: float = 0

        logger.info("[Memory] MEMORY_FILE path: %s (exists=%s)", MEMORY_FILE, os.path.exists(MEMORY_FILE))

        # Warmup Ollama model to GPU in background thread (avoid blocking startup)
        # Ollama may take 25-30s to load model from disk on cold start
        self._warmup_thread = threading.Thread(target=self._warmup_ollama_model, daemon=True)
        self._warmup_thread.start()

    @property
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ── Ollama Model Warmup ──────────────────────────────────────────

    def _warmup_ollama_model(self) -> None:
        """Pre-load Ollama model to GPU to avoid 25-30s cold start on first request.

        Ollama unloads models from GPU after idle timeout (default 5min).
        This sends a tiny request to force model loading at startup.
        Uses urllib instead of httpx to avoid connection pool conflicts.
        """
        import urllib.request
        try:
            logger.info("[OllamaWarmup] Pre-loading model '%s' to GPU...", self.ollama_model)
            start = time.time()
            data = json.dumps({
                "model": self.ollama_model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "max_tokens": 1,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{self.ollama_url}/v1/chat/completions",
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                resp.read()
            elapsed = time.time() - start
            logger.info("[OllamaWarmup] Model '%s' loaded to GPU in %.1fs", self.ollama_model, elapsed)
        except Exception as e:
            logger.warning("[OllamaWarmup] Failed to pre-load model '%s': %s (non-fatal, will load on first request)",
                           self.ollama_model, e)

    # ── Health Check (Cached) ────────────────────────────────────────

    def health_check(self) -> Dict:
        """Health check with 30s cache to avoid per-request latency."""
        now = time.time()
        if (now - self._health_cache["cached_at"]) < self._health_cache_ttl:
            return self._health_cache

        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.api_url}/health", headers=self._headers)
                resp.raise_for_status()
                result = resp.json()
        except Exception as e:
            result = {"status": "unavailable", "error": str(e)}

        result["cached_at"] = now
        self._health_cache = result
        return result

    def detailed_health(self) -> Dict:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self.api_url}/health/detailed", headers=self._headers)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            return {"status": "unavailable", "error": str(e)}

    # ── Memory Context ───────────────────────────────────────────────

    def _load_memory_context(self) -> str:
        """Load full Memory content (cached for 10s).

        Returns the raw content of 'Routing Patterns Learned' and 'Key Rules'
        sections from MEMORY.md — the single source of truth for routing rules.
        """
        now = time.time()
        if (now - self._memory_context_cached_at) < self._memory_context_cache_ttl:
            return self._memory_context_cache

        try:
            if not os.path.exists(MEMORY_FILE):
                logger.warning("[Memory] MEMORY_FILE not found: %s", MEMORY_FILE)
                self._memory_context_cache = ""
                self._memory_context_cached_at = now
                return ""

            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                content = f.read()

            logger.info("[Memory] Loaded from %s: %d bytes", MEMORY_FILE, len(content))

            # Extract routing patterns and rules (skip feedback history)
            context_parts = []
            for section in ["Routing Patterns Learned", "Key Rules"]:
                header = f"## {section}"
                if header in content:
                    start = content.index(header) + len(header)
                    # Find next ## section or end
                    next_section = content.find("\n## ", start)
                    section_text = content[start:next_section].strip() if next_section != -1 else content[start:].strip()
                    # Only take non-feedback lines
                    lines = [l for l in section_text.split("\n")
                             if l.strip().startswith("-") and "✓" not in l and "✗" not in l]
                    if lines:
                        context_parts.append(f"{section}:\n" + "\n".join(lines))

            self._memory_context_cache = "\n".join(context_parts)
            self._memory_context_cached_at = now
            logger.info("[Memory] Extracted routing context: %d rules/patterns, %d bytes",
                        len([l for l in self._memory_context_cache.split("\n") if l.strip().startswith("-")]),
                        len(self._memory_context_cache))
            return self._memory_context_cache

        except Exception as e:
            logger.warning("[Memory] Failed to load memory context from %s: %s", MEMORY_FILE, e)
            return ""

    def _parse_routing_rules_from_memory(self) -> list:
        """Parse routing rules from MEMORY.md into structured data.

        Each rule is a dict: {
            "category": str,
            "keywords": list[str],
            "route": str,
            "raw_line": str,
        }

        Format: "- Category [kw1,kw2,...] → route (optional notes)"
        Keywords in [] are used for prompt matching. If no [], category name
        words are used as fallback keywords.
        """
        memory_ctx = self._load_memory_context()
        if not memory_ctx:
            return []

        rules = []
        for line in memory_ctx.split("\n"):
            line = line.strip()
            if not line.startswith("-") or "→" not in line:
                continue
            try:
                # Parse: "- Category [keywords] → route (notes)"
                rule_text = line.lstrip("- ").strip()

                # Extract keywords from [kw1,kw2,...]
                keywords = []
                kw_match = re.search(r'\[([^\]]+)\]', rule_text)
                if kw_match:
                    keywords = [k.strip().lower() for k in kw_match.group(1).split(",")]
                    # Remove [keywords] from rule_text for category/route parsing
                    rule_text = rule_text[:kw_match.start()] + rule_text[kw_match.end():]

                # Parse category → route
                parts = rule_text.split("→")
                category = parts[0].strip()
                route_part = parts[1].split("(")[0].strip() if len(parts) > 1 else ""

                # Strip common prefixes
                for prefix in ("always ", "fast ", "must "):
                    if route_part.lower().startswith(prefix):
                        route_part = route_part[len(prefix):]

                route = route_part.strip().lower()
                valid = {"direct_local", "gateway", "agent_chain", "local_inference", "multimodal"}
                if route not in valid:
                    continue

                rules.append({
                    "category": category,
                    "keywords": keywords,
                    "route": route,
                    "raw_line": line,
                })
            except (IndexError, ValueError):
                continue

        return rules

    def _build_routing_system_message(self, for_agent: bool = False) -> str:
        """Build routing rules from MEMORY.md — the single source of truth.

        Generates the routing rules section of the system prompt dynamically
        from Memory's 'Routing Patterns Learned', 'Key Rules', and 'Latency Stats'.
        No hardcoded routing rules — all rules come from MEMORY.md.

        Args:
            for_agent: If True, use the stricter ROUTING_PROMPT_TEMPLATE_AGENT
                       template designed for the Hermes Agent path (which has its
                       own system prompt that may contain tool-use instructions).
                       If False (default), use ROUTING_PROMPT_TEMPLATE for the
                       Ollama direct path.
        """
        rules = self._parse_routing_rules_from_memory()
        memory_ctx = self._load_memory_context()
        latency_info = self._load_latency_stats_from_memory()

        logger.info("[Routing] Building system_message: rules=%d, memory_ctx=%d bytes, latency_info=%d bytes",
                    len(rules), len(memory_ctx), len(latency_info))

        if rules:
            # Build human-readable routing rules from Memory
            # Limit to top 10 rules to keep system prompt short for fast Ollama inference
            rule_lines = []
            for r in rules[:10]:
                kw_display = ", ".join(r["keywords"][:4]) if r["keywords"] else r["category"]
                rule_lines.append(f"- {r['category']}({kw_display}) → {r['route']}")
            routing_rules = "路由规则(从MEMORY.md学习):\n" + "\n".join(rule_lines)
        elif memory_ctx:
            # Fallback: use raw memory context
            routing_rules = f"外部注入的路由规则(从本地MEMORY.md学习):\n{memory_ctx}"
        else:
            # Last resort: minimal default rules
            routing_rules = "路由规则:\n- 简单闲聊 → direct_local\n- 代码/编程 → gateway\n- 隐私敏感 → local_inference\n- 其他 → direct_local"

        # Append latency awareness to help LLM make cost-aware decisions
        # Keep it minimal — only avg latency per route, no p95/samples to reduce prompt size
        if latency_info:
            # Extract only avg latency from each line (format: "- route: avg=Xms, ...")
            avg_lines = []
            for line in latency_info.split("\n"):
                line = line.strip()
                if not line.startswith("-"):
                    continue
                avg_match = re.search(r'avg=(\d+ms)', line)
                route_match = re.search(r'^-\s*(\w+)', line)
                if avg_match and route_match:
                    avg_lines.append(f"- {route_match.group(1)}: avg={avg_match.group(1)}")
            if avg_lines:
                routing_rules += "\n延迟统计:\n" + "\n".join(avg_lines)

        template = ROUTING_PROMPT_TEMPLATE_AGENT if for_agent else ROUTING_PROMPT_TEMPLATE
        return template.format(routing_rules=routing_rules)

    def _load_latency_stats_from_memory(self) -> str:
        """Load latency stats section from MEMORY.md for routing awareness."""
        if not os.path.exists(MEMORY_FILE):
            return ""

        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return ""

        latency_header = "## Latency Stats (auto-updated)"
        if latency_header not in content:
            return ""

        parts = content.split(latency_header, 1)
        after = parts[1] if len(parts) > 1 else ""

        # Extract until next section
        next_section = after.find("\n## ")
        if next_section >= 0:
            stats_section = after[:next_section].strip()
        else:
            stats_section = after.strip()

        # Only return lines starting with "-"
        lines = [l.strip() for l in stats_section.split("\n") if l.strip().startswith("-")]
        logger.info("[Memory] Latency stats from %s: %d routes loaded", MEMORY_FILE, len(lines))
        return "\n".join(lines)

    # ── Session Warmup ───────────────────────────────────────────────

    def _warmup_agent_session(self) -> None:
        """Pre-create a Hermes Agent session so subsequent requests can reuse it.

        Without warmup, every routing request creates a new AIAgent instance
        (~2s overhead for tool registration, system prompt build, etc.).
        By sending a warmup request first and capturing the session ID,
        subsequent requests via X-Hermes-Session-Id can reuse the cached
        agent state and skip re-initialization.
        """
        try:
            system_msg = self._build_routing_system_message(for_agent=True)
            payload = {
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": "warmup"}],
                "system_message": system_msg,
                "max_tokens": 10,
                "stream": False,
            }
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"{self.api_url}/v1/chat/completions",
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()

            # Capture session ID for reuse
            session_id = resp.headers.get("X-Hermes-Session-Id")
            if session_id:
                self._session_id = session_id
                logger.info(f"Hermes Agent session warmed up: {session_id}")
            else:
                logger.warning("Hermes Agent warmup: no session ID in response header")

        except Exception as e:
            logger.warning(f"Hermes Agent session warmup failed (non-fatal): {e}")

    # ── Routing ──────────────────────────────────────────────────────

    def route_via_agent(self, request: Dict) -> Dict:
        """Make routing decision with automatic path selection.

        Path selection logic:
        - use_direct_ollama=True (default): Ollama direct + Memory loop (~1-2s)
        - use_direct_ollama=False: Hermes Agent API + system_message (~5s)
        - Fallback: If primary path fails, try the other path

        Both paths share the same:
        - ROUTING_SYSTEM_MESSAGE_TEMPLATE + Memory context (closed-loop)
        - _post_validate_route (keyword-based correction)
        - _extract_route_from_text (natural language fallback)
        - Feedback writing to MEMORY.md
        """
        prompt = request.get("prompt", "")[:80]
        req_type = request.get("type", "chat")
        logger.info("[Routing] === 新路由决策 === prompt='%s' type=%s mode=%s",
                    prompt, req_type, "ollama_direct" if self.use_direct_ollama else "hermes_agent")
        if self.use_direct_ollama:
            # Primary: Ollama direct (fast, ~1-2s)
            try:
                result = self._route_via_ollama_direct(request)
                logger.info("[Routing] Ollama直连完成: route=%s complexity=%s latency=%sms memory=%s",
                            result.get("route_path"), result.get("complexity_score"),
                            result.get("agent_llm_latency_ms"), result.get("memory_context_used"))
                return result
            except Exception as e:
                logger.warning("[Routing] Ollama直连失败: %s, 降级到Hermes Agent", e)
                try:
                    result = self._route_via_hermes_agent(request)
                    logger.info("[Routing] Hermes Agent降级完成: route=%s latency=%sms",
                                result.get("route_path"), result.get("agent_llm_latency_ms"))
                    return result
                except Exception as e2:
                    logger.error("[Routing] Hermes Agent也失败: %s, 兜底gateway+post_validate", e2)
                    fallback = {"route_path": "gateway", "reason": f"All routing failed: {e2}", "official_agent_routed": False}
                    return self._post_validate_route(fallback, request)
        else:
            # Primary: Hermes Agent (full framework, ~5s)
            try:
                result = self._route_via_hermes_agent(request)
                logger.info("[Routing] Hermes Agent完成: route=%s complexity=%s latency=%sms memory=%s",
                            result.get("route_path"), result.get("complexity_score"),
                            result.get("agent_llm_latency_ms"), result.get("memory_context_used"))
                return result
            except Exception as e:
                logger.warning("[Routing] Hermes Agent失败: %s, 降级到Ollama", e)
                try:
                    result = self._route_via_ollama_direct(request)
                    logger.info("[Routing] Ollama降级完成: route=%s latency=%sms",
                                result.get("route_path"), result.get("agent_llm_latency_ms"))
                    return result
                except Exception as e2:
                    logger.error("[Routing] Ollama也失败: %s, 兜底gateway+post_validate", e2)
                    fallback = {"route_path": "gateway", "reason": f"All routing failed: {e2}", "official_agent_routed": False}
                    return self._post_validate_route(fallback, request)

    def _route_via_hermes_agent(self, request: Dict) -> Dict:
        """Route via Hermes Agent API using system_message injection (Plan B).

        Key difference from old approach:
        - OLD: Agent calls skills_list → skill_view → LLM inference (3-4 rounds)
        - NEW: system_message injects rules → 1 LLM inference only

        The system_message parameter is mapped to ephemeral_system_prompt in
        Hermes Agent, which is appended to the system prompt at API-call time
        without being saved to trajectories.

        Optimization: Reuse session via X-Hermes-Session-Id header so the
        Agent can reuse its cached system prompt and avoid re-initialization
        overhead (~2s saved on warm requests).
        """
        # Skip warmup — no session reuse means no need to pre-create sessions.
        # Each request creates a fresh session with minimal prompt_tokens.

        prompt = request.get("prompt", "")
        req_type = request.get("type", "chat")
        priority = request.get("priority", 3)
        has_tools = bool(request.get("tools"))
        require_local = False
        constraints = request.get("constraints", {})
        if isinstance(constraints, dict):
            require_local = constraints.get("require_local", False)

        # Build routing rules with memory context (use agent-specific template)
        system_message = self._build_routing_system_message(for_agent=True)

        # Embed JSON format reminder in user message — 3B models attend more
        # to user messages than to system prompt tail when the system prompt
        # is large (~2048 tokens).  The prefix acts as a "format anchor".
        user_msg = (
            f"[只输出JSON，不要输出其他内容] 判断路由:\n"
            f"内容: {prompt[:200]}\n"
            f"类型: {req_type}\n"
            f"优先级: {priority}\n"
            f"有工具: {has_tools}\n"
            f"需本地: {require_local}\n"
            f"输出格式: {{\"route_path\":\"选项\",\"complexity_score\":0-100,\"reason\":\"原因\"}}"
        )

        payload = {
            "model": "hermes-agent",
            "messages": [
                {"role": "user", "content": user_msg},
            ],
            "system_message": system_message,
            "max_tokens": 200,
            "stream": False,
        }

        # No session reuse — each routing request uses a fresh session.
        #
        # Rationale: Hermes Agent appends every conversation to the session
        # history, causing prompt_tokens to grow (1500→9000+) and inference
        # to slow down (3s→15s+). Session reuse was originally intended to
        # save ~2s Agent initialization overhead, but the token accumulation
        # penalty far outweighs that benefit.
        #
        # With fresh sessions, each request has ~1500 prompt_tokens and
        # completes in ~3s (1s Agent init + 2s LLM inference).
        headers = dict(self._headers)

        start = time.time()
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{self.api_url}/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
        elapsed_ms = int((time.time() - start) * 1000)

        result = resp.json()

        # Capture session ID from response header for reuse
        resp_session_id = resp.headers.get("X-Hermes-Session-Id")
        if resp_session_id:
            self._session_id = resp_session_id

        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        logger.debug("[Routing] Hermes Agent原始响应: %s", content[:200])

        try:
            routing = json.loads(content)
        except json.JSONDecodeError:
            routing = self._extract_json_from_text(content)
            logger.debug("[Routing] JSON解析失败, extract_json结果: %s", routing)

        if not routing:
            # 3B model may return natural language instead of JSON under
            # Hermes Agent's large system prompt — extract route from text
            routing = self._extract_route_from_text(content, request)
            logger.debug("[Routing] JSON提取失败, extract_route结果: %s", routing)

        valid_paths = {"direct_local", "gateway", "agent_chain", "local_inference", "multimodal"}
        if routing.get("route_path") not in valid_paths:
            logger.warning("[Routing] Agent返回无效路由 '%s', 修正为gateway", routing.get("route_path"))
            routing["route_path"] = "gateway"

        # Post-validation: 3B model may return valid JSON but wrong route
        # (e.g. "write a quicksort" misrouted to direct_local because of
        # greeting prefix). Override when code keywords are clearly present.
        pre_validate_route = routing.get("route_path")
        routing = self._post_validate_route(routing, request)
        if routing.get("post_validated"):
            logger.info("[Routing] Post-validate修正: %s → %s (reason: %s)",
                        pre_validate_route, routing["route_path"], routing.get("reason"))

        routing["agent_llm_latency_ms"] = elapsed_ms
        routing["official_agent_routed"] = True
        routing["agent_decision"] = "hermes_agent_system_message"
        routing["agent_confidence"] = 0.7
        routing["skill_matched"] = "routing-decision"
        routing["memory_context_used"] = bool(self._load_memory_context())
        return routing

    def _route_via_ollama_direct(self, request: Dict) -> Dict:
        """Direct Ollama API call for routing — primary path for low latency.

        Uses the same ROUTING_SYSTEM_MESSAGE_TEMPLATE + Memory context as the
        Hermes Agent path, but bypasses the Agent framework entirely. This
        eliminates the ~2s per-request Agent initialization overhead while
        preserving the full routing rules + Memory closed-loop.

        Latency comparison:
        - Hermes Agent path: ~5s (2s framework + 2s LLM + 1s overhead)
        - This direct path:  ~1-2s (pure LLM inference, no framework)
        """
        prompt = request.get("prompt", "")
        req_type = request.get("type", "chat")
        priority = request.get("priority", 3)
        has_tools = bool(request.get("tools"))
        require_local = False
        constraints = request.get("constraints", {})
        if isinstance(constraints, dict):
            require_local = constraints.get("require_local", False)

        # Use the same system_message template as Hermes Agent path
        # (includes Memory context for closed-loop learning)
        system_msg = self._build_routing_system_message()

        user_msg = (
            f"判断路由:\n"
            f"内容: {prompt[:200]}\n"
            f"类型: {req_type}\n"
            f"优先级: {priority}\n"
            f"有工具: {has_tools}\n"
            f"需本地: {require_local}"
        )

        payload = {
            "model": self.ollama_model,
            "messages": [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            "stream": False,
            "options": {"num_ctx": 2048, "temperature": 0.0, "num_predict": 128},
            "format": "json",
        }

        try:
            start = time.time()
            # Use persistent client with short timeout for routing
            # Routing should complete in 1-3s; if it takes longer, Ollama is stuck
            # and we should fall back immediately rather than queue behind a stuck request
            try:
                resp = self._ollama_client.post(
                    "/v1/chat/completions",
                    json=payload,
                    timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
                )
                resp.raise_for_status()
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                elapsed_ms = int((time.time() - start) * 1000)
                logger.warning("[Routing] Ollama不可用 (%s, %dms), 使用post-validate fallback",
                               type(e).__name__, elapsed_ms)
                # Reset connection pool after timeout to avoid stale connections
                # blocking subsequent requests (Ollama may still be processing the timed-out request)
                try:
                    self._ollama_client.close()
                    self._ollama_client = httpx.Client(
                        base_url=self.ollama_url,
                        timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0),
                        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                    )
                except Exception:
                    pass
                return self._post_validate_route({"route_path": "gateway", "complexity_score": 0}, request)
            elapsed_ms = int((time.time() - start) * 1000)

            result = resp.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug("[Routing] Ollama原始响应: %s", content[:200])

            try:
                routing = json.loads(content)
            except json.JSONDecodeError:
                routing = self._extract_json_from_text(content)
                logger.debug("[Routing] JSON解析失败, extract_json结果: %s", routing)

            if not routing:
                routing = self._extract_route_from_text(content, request)
                logger.debug("[Routing] JSON提取失败, extract_route结果: %s", routing)

            valid_paths = {"direct_local", "gateway", "agent_chain", "local_inference", "multimodal"}
            if routing.get("route_path") not in valid_paths:
                logger.warning("[Routing] LLM返回无效路由 '%s', 修正为gateway", routing.get("route_path"))
                routing["route_path"] = "gateway"

            # Apply same post-validation as Hermes Agent path
            pre_validate_route = routing.get("route_path")
            routing = self._post_validate_route(routing, request)
            if routing.get("post_validated"):
                logger.info("[Routing] Post-validate修正: %s → %s (reason: %s)",
                            pre_validate_route, routing["route_path"], routing.get("reason"))

            routing["agent_llm_latency_ms"] = elapsed_ms
            routing["official_agent_routed"] = True
            routing["agent_decision"] = "ollama_direct_with_memory"
            routing["agent_confidence"] = 0.7
            routing["skill_matched"] = "routing-decision"
            routing["memory_context_used"] = bool(self._load_memory_context())
            return routing

        except httpx.TimeoutException:
            logger.warning("Ollama routing timeout, falling back with post-validate")
            fallback = {"route_path": "gateway", "reason": "Ollama timeout", "official_agent_routed": False}
            return self._post_validate_route(fallback, request)
        except Exception as e:
            logger.error(f"Ollama routing error: {e}")
            fallback = {"route_path": "gateway", "reason": f"Ollama error: {e}", "official_agent_routed": False}
            return self._post_validate_route(fallback, request)

    # ── Feedback (Local + Hermes Memory async) ───────────────────────

    def record_feedback_via_memory(self, skill_name: str, route_path: str,
                                   success: bool, latency_ms: int,
                                   request_summary: str = "") -> bool:
        """Record routing feedback — local file instantly, Hermes Memory async.

        1. Write to local JSONL file immediately (always succeeds, fast)
        2. Queue for async write to Hermes Memory (non-blocking, for evolution)
        """
        # 1. Local file (instant)
        self._write_feedback_local(skill_name, route_path, success, latency_ms, request_summary)

        # 2. Queue for async Memory write (non-blocking)
        feedback = {
            "skill_name": skill_name,
            "route_path": route_path,
            "success": success,
            "latency_ms": latency_ms,
            "request_summary": request_summary[:50] if request_summary else "",
        }
        with self._feedback_lock:
            self._feedback_queue.append(feedback)

        # Start background writer if not running
        self._ensure_memory_writer()

        return True

    def _ensure_memory_writer(self):
        """Ensure the background Memory writer thread is running."""
        if self._memory_write_thread and self._memory_write_thread.is_alive():
            return

        self._memory_write_thread = threading.Thread(
            target=self._memory_writer_loop,
            daemon=True,
            name="hermes-memory-writer",
        )
        self._memory_write_thread.start()

    def _memory_writer_loop(self):
        """Background thread: drain feedback queue and write to Hermes Memory.

        Evolution closed-loop (Plan A: Ollama routing + Agent evolution):
        1. Write feedback to MEMORY.md via Python (deterministic, format-guaranteed)
        2. Notify Hermes Agent API so its MemoryStore reloads context
        3. Next routing request picks up updated rules automatically
        """
        while True:
            time.sleep(2)  # Batch every 2 seconds

            with self._feedback_lock:
                if not self._feedback_queue:
                    continue
                batch = self._feedback_queue[:]
                self._feedback_queue.clear()

            # Write each feedback entry to Hermes Memory
            for fb in batch:
                try:
                    self._write_feedback_to_memory(
                        fb["skill_name"], fb["route_path"],
                        fb["success"], fb["latency_ms"], fb["request_summary"],
                    )
                except Exception as e:
                    logger.warning(f"Background Memory write failed: {e}")

            # After batch write, notify Hermes Agent to reload memory context
            # This enables the Agent's self-learning mechanism: it reads
            # updated MEMORY.md on its next request, incorporating our
            # feedback-driven rule evolution.
            self._notify_agent_memory_update(batch)

    def _write_feedback_local(self, skill_name: str, route_path: str,
                              success: bool, latency_ms: int,
                              request_summary: str) -> bool:
        """Write feedback to local JSONL file for fast persistence."""
        entry = {
            "timestamp": datetime.now().isoformat(),
            "skill": skill_name or "none",
            "route_path": route_path,
            "success": success,
            "latency_ms": latency_ms,
            "request_summary": request_summary[:50] if request_summary else "",
        }

        feedback_dir = os.path.join(os.path.dirname(MEMORY_FILE), "..", "routing_feedback")
        feedback_dir = os.path.normpath(feedback_dir)
        os.makedirs(feedback_dir, exist_ok=True)
        feedback_file = os.path.join(feedback_dir, "feedback.jsonl")

        try:
            with open(feedback_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            logger.error(f"Failed to write feedback locally: {e}")
            return False

    def _notify_agent_memory_update(self, batch: list) -> None:
        """Notify Hermes Agent that MEMORY.md has been updated.

        Plan A evolution closed-loop: after Python writes feedback to MEMORY.md,
        we send a lightweight request to the Agent API so its MemoryStore
        reloads the updated context. The Agent's MemoryStore caches MEMORY.md
        content and refreshes periodically — this notification ensures the
        Agent sees the latest rules on its next request.

        Two notification strategies:
        1. Direct: Send a health check to verify Agent is alive (lightweight)
        2. If Agent has memory tool enabled: the Agent will auto-read MEMORY.md
           on its next request via MemoryStore.format_for_system_prompt()

        We don't need to explicitly call the memory tool because:
        - Python already wrote the feedback to MEMORY.md (format-guaranteed)
        - Agent's MemoryStore reads MEMORY.md on every request
        - The Agent's system prompt automatically includes updated rules
        """
        if not batch:
            return

        try:
            # Lightweight health check — also serves as "ping" to keep
            # the Agent process warm and verify it can read MEMORY.md
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.api_url}/health", headers=self._headers)
                if resp.status_code == 200:
                    logger.debug(f"Agent notified of {len(batch)} feedback entries "
                                 "(MemoryStore will reload on next request)")
                else:
                    logger.debug(f"Agent health check returned {resp.status_code}")
        except Exception as e:
            # Non-critical: Agent will still read MEMORY.md on next request
            # even if this notification fails
            logger.debug(f"Agent notification skipped (non-critical): {e}")

    def _write_feedback_to_memory(self, skill_name: str, route_path: str,
                                  success: bool, latency_ms: int,
                                  request_summary: str) -> bool:
        """Write routing feedback directly to Hermes Memory file.

        Bypasses the Agent's memory tool (which has a 1375 char limit and
        replace-only semantics) by directly appending to MEMORY.md.
        Keeps the file concise with a rolling window of recent feedback.

        Auto-updates:
        1. Feedback History (rolling window of 10)
        2. Latency Stats (running avg/p95 per route)
        3. Rule evolution on failure
        """
        if not os.path.exists(MEMORY_FILE):
            return False

        # Read existing content
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read MEMORY.md: {e}")
            return False

        # Build concise feedback line
        success_str = "✓" if success else "✗"
        line = f"- {route_path} {success_str} {latency_ms}ms"
        if request_summary:
            line += f" '{request_summary[:30]}'"

        # ── Update Feedback History ──
        feedback_header = "## Feedback History"
        if feedback_header in content:
            parts = content.split(feedback_header, 1)
            before_fb = parts[0]
            after_fb = parts[1] if len(parts) > 1 else ""

            feedback_lines = [l for l in after_fb.strip().split("\n") if l.strip().startswith("-")]
            feedback_lines.append(line)
            feedback_lines = feedback_lines[-10:]  # Rolling window
        else:
            before_fb = content.rstrip()
            feedback_lines = [line]

        # ── Update Latency Stats ──
        latency_header = "## Latency Stats (auto-updated)"
        latency_line = self._compute_latency_stats(content, route_path, latency_ms)

        # Reconstruct content
        # Split into sections: before_latency | latency_section | between | feedback_section
        if latency_header in before_fb:
            lparts = before_fb.split(latency_header, 1)
            before_latency = lparts[0]
            after_latency = lparts[1] if len(lparts) > 1 else ""
            # Remove old latency section (everything until next ## or end)
            next_section = after_latency.find("\n## ")
            if next_section >= 0:
                between = after_latency[next_section:]
            else:
                between = ""
            new_content = before_latency + latency_header + "\n" + latency_line + "\n" + between
        else:
            new_content = before_fb + "\n" + latency_header + "\n" + latency_line + "\n"

        # Append feedback section
        new_content += "\n" + feedback_header + "\n" + "\n".join(feedback_lines) + "\n"

        # Write back (keep under 2000 chars for Hermes compatibility)
        if len(new_content) > 2000:
            feedback_lines = feedback_lines[1:]
            new_content_parts = new_content.split(feedback_header, 1)
            new_content = new_content_parts[0] + feedback_header + "\n" + "\n".join(feedback_lines) + "\n"

        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info(f"Feedback written to MEMORY.md: {route_path} {success_str} {latency_ms}ms")

            # Invalidate memory context cache so next request picks up changes
            self._memory_context_cached_at = 0

            # Rule evolution: on failure, extract corrective rule
            if not success and request_summary:
                self._evolve_rule_from_feedback(route_path, request_summary)

            # Rule reinforcement: on success with high latency, suggest optimization
            if success and latency_ms > 30000 and route_path == "gateway":
                self._reinforce_latency_awareness(route_path, latency_ms, request_summary)

            # Pattern discovery: on success, check if new keywords should be added to rules
            if success and request_summary:
                self._discover_and_add_pattern(route_path, request_summary, new_content)

            # Rule pruning: check latency gap and reinforce simple→local rules
            self._prune_latency_gap(new_content)

            return True
        except Exception as e:
            logger.error(f"Failed to write MEMORY.md: {e}")
            return False

    def _compute_latency_stats(self, content: str, route_path: str, latency_ms: int) -> str:
        """Compute updated latency stats from existing stats + new data point."""
        import re

        # Parse existing stats
        stats = {}
        stats_pattern = r"- (\w+): avg=(\d+)ms, p95=(\d+)ms, samples=(\d+)"
        for match in re.finditer(stats_pattern, content):
            rp, avg, p95, samples = match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))
            stats[rp] = {"avg": avg, "p95": p95, "samples": samples}

        # Update with new data point
        if route_path in stats:
            s = stats[route_path]
            # Running average: new_avg = (old_avg * old_n + new_val) / (old_n + 1)
            total = s["avg"] * s["samples"] + latency_ms
            s["samples"] += 1
            s["avg"] = total // s["samples"]
            # P95: approximate — if new value > p95, nudge p95 up
            if latency_ms > s["p95"]:
                s["p95"] = (s["p95"] + latency_ms) // 2
            elif s["samples"] % 10 == 0:
                # Every 10 samples, nudge p95 down slightly (decay)
                s["p95"] = int(s["p95"] * 0.95)
        else:
            stats[route_path] = {"avg": latency_ms, "p95": latency_ms, "samples": 1}

        # Build output lines
        lines = []
        for rp in ["direct_local", "gateway", "local_inference", "multimodal", "agent_chain"]:
            if rp in stats:
                s = stats[rp]
                lines.append(f"- {rp}: avg={s['avg']}ms, p95={s['p95']}ms, samples={s['samples']} (Hermes路由实测)")
        return "\n".join(lines)

    def _reinforce_latency_awareness(self, route_path: str, latency_ms: int,
                                      request_summary: str) -> None:
        """Reinforce latency-aware routing rules based on successful but slow executions.

        When a gateway request succeeds but takes >30s, this method updates
        the Key Rules section to note the latency trade-off, helping future
        routing decisions consider whether the complexity justifies the wait.
        """
        if not os.path.exists(MEMORY_FILE):
            return

        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return

        # Add latency note to Key Rules if not already present
        key_rules_header = "## Key Rules"
        if key_rules_header not in content:
            return

        latency_note = f"- gateway延迟avg~70s, 简单请求优先用direct_local(avg~6s)"
        if latency_note in content:
            return  # Already present

        parts = content.split(key_rules_header, 1)
        before = parts[0]
        after = parts[1] if len(parts) > 1 else ""

        # Find next section
        next_section_idx = after.find("\n## ")
        if next_section_idx >= 0:
            rules_section = after[:next_section_idx]
            rest = after[next_section_idx:]
        else:
            rules_section = after
            rest = ""

        # Append latency note
        new_rules = rules_section.rstrip() + f"\n{latency_note}\n"

        new_content = before + key_rules_header + new_rules + rest

        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)

        logger.info(f"Latency awareness reinforced in MEMORY.md: gateway avg~{latency_ms}ms")

    def _discover_and_add_pattern(self, route_path: str, request_summary: str,
                                   content: str) -> None:
        """Discover new routing patterns from successful feedback.

        When a request succeeds, check if the request_summary contains keywords
        that are NOT yet covered by existing routing rules. If the same keyword
        appears in multiple successful requests for the same route, add it to
        the corresponding rule's keyword list in MEMORY.md.
        """
        # Keyword groups for each route — candidates to add if not covered
        route_keywords = {
            "direct_local": ["你好", "天气", "计算", "翻译", "什么是", "解释", "闲聊", "问候"],
            "gateway": ["执行", "代码", "Volcano", "部署", "架构", "方案", "规划", "编排", "工作流"],
            "multimodal": ["图片", "OCR", "视觉", "音频", "视频", "识别", "截图", "摄像头"],
            "local_inference": ["隐私", "敏感", "个人信息", "医疗", "财务", "脱敏", "机密"],
        }

        keywords = route_keywords.get(route_path)
        if not keywords:
            return

        # Check existing rules
        existing_rules = self._parse_routing_rules_from_memory()
        summary_lower = request_summary.lower()

        # Find which keyword in the summary matches this route's group
        matched_kw = None
        for kw in keywords:
            if kw in summary_lower:
                # Check if already covered by existing rules
                already_covered = False
                for rule in existing_rules:
                    if kw in rule.get("keywords", []):
                        already_covered = True
                        break
                if not already_covered:
                    matched_kw = kw
                    break

        if not matched_kw:
            return

        # Add the keyword to the matching rule in MEMORY.md
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                mem_content = f.read()
        except Exception:
            return

        patterns_header = "## Routing Patterns Learned"
        if patterns_header not in mem_content:
            return

        # Find the rule line for this route that most closely matches
        parts = mem_content.split(patterns_header, 1)
        before = parts[0]
        after = parts[1] if len(parts) > 1 else ""

        next_section = after.find("\n## ")
        if next_section >= 0:
            patterns_section = after[:next_section]
            rest = after[next_section:]
        else:
            patterns_section = after
            rest = ""

        # Find the best matching rule line for this route_path
        best_line_idx = -1
        best_match_count = 0
        lines = patterns_section.split("\n")
        for i, line in enumerate(lines):
            if not line.strip().startswith("-"):
                continue
            if f"→ {route_path}" not in line:
                continue
            # Count how many existing keywords match the summary
            kw_match = re.search(r'\[(.+?)\]', line)
            if kw_match:
                existing_kws = [k.strip() for k in kw_match.group(1).split(",")]
                match_count = sum(1 for k in existing_kws if k in summary_lower)
                if match_count > best_match_count:
                    best_match_count = match_count
                    best_line_idx = i

        if best_line_idx < 0:
            return

        # Add the new keyword to the existing keyword list
        old_line = lines[best_line_idx]
        kw_match = re.search(r'\[(.+?)\]', old_line)
        if not kw_match:
            return

        existing_kws = [k.strip() for k in kw_match.group(1).split(",")]
        if matched_kw in existing_kws:
            return  # Already there

        existing_kws.append(matched_kw)
        # Keep keyword list manageable (max 20)
        if len(existing_kws) > 20:
            existing_kws = existing_kws[-20:]

        new_kw_str = ", ".join(existing_kws)
        new_line = old_line[:kw_match.start()] + "[" + new_kw_str + "]" + old_line[kw_match.end():]
        lines[best_line_idx] = new_line

        new_patterns = "\n".join(lines)
        new_content = before + patterns_header + new_patterns + rest

        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)

        # Invalidate cache
        self._memory_context_cached_at = 0
        logger.info(f"Pattern discovered: keyword '{matched_kw}' added to {route_path} rule in MEMORY.md")

    def _prune_latency_gap(self, content: str) -> None:
        """Check latency gap between routes and reinforce simple→local rules.

        If gateway is significantly slower than direct_local, add a reminder
        to Key Rules to ensure simple requests avoid gateway.
        """
        # Parse latency stats
        stats = {}
        stats_pattern = r"- (\w+): avg=(\d+)ms, p95=(\d+)ms, samples=(\d+)"
        for match in re.finditer(stats_pattern, content):
            rp, avg, p95, samples = match.group(1), int(match.group(2)), int(match.group(3)), int(match.group(4))
            stats[rp] = {"avg": avg, "p95": p95, "samples": samples}

        if "gateway" not in stats or "direct_local" not in stats:
            return
        if stats["gateway"]["samples"] < 5:
            return

        gw_avg = stats["gateway"]["avg"]
        dl_avg = stats["direct_local"]["avg"]
        if dl_avg == 0 or gw_avg <= dl_avg * 10:
            return

        # Gateway is 10x+ slower — ensure Key Rules has a latency gap note
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                mem_content = f.read()
        except Exception:
            return

        key_rules_header = "## Key Rules"
        if key_rules_header not in mem_content:
            return

        # Check if latency gap note already exists with current data
        gap_note = f"gateway延迟avg~{gw_avg // 1000}s, 简单请求优先用direct_local(avg~{dl_avg // 1000}s)"
        if gap_note in mem_content:
            return  # Already up to date

        # Remove old gap note if exists
        old_gap_pattern = r"- gateway延迟avg~\d+s, 简单请求优先用direct_local\(avg~\d+s\)\n?"
        mem_content = re.sub(old_gap_pattern, "", mem_content)

        # Add updated gap note
        parts = mem_content.split(key_rules_header, 1)
        before = parts[0]
        after = parts[1] if len(parts) > 1 else ""

        next_section = after.find("\n## ")
        if next_section >= 0:
            rules_section = after[:next_section]
            rest = after[next_section:]
        else:
            rules_section = after
            rest = ""

        new_rules = rules_section.rstrip() + f"\n- {gap_note}\n"
        new_content = before + key_rules_header + new_rules + rest

        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)

        self._memory_context_cached_at = 0
        logger.info(f"Latency gap pruned: gateway={gw_avg}ms vs direct_local={dl_avg}ms "
                     f"({gw_avg / dl_avg:.0f}x slower)")

    def _evolve_rule_from_feedback(self, failed_route: str, request_summary: str) -> None:
        """Extract a new routing rule from failed feedback and add to Memory.

        When a routing decision is marked as failed (success=False), this method
        attempts to extract a corrective rule from the request_summary and adds
        it to the 'Routing Patterns Learned' section of MEMORY.md.

        Category detection uses existing Memory rules' keywords — no hardcoded
        category_map. If no existing category matches, creates a new one from
        the summary.
        """
        # Try to extract desired route from summary (e.g. "应走direct_local")
        desired_route = self._extract_desired_route(request_summary)

        # Match summary against existing Memory rules' keywords
        summary_lower = request_summary.lower()
        matched_category = None
        matched_keywords = []

        rules = self._parse_routing_rules_from_memory()
        for rule in rules:
            if any(kw in summary_lower for kw in rule["keywords"]):
                matched_category = rule["category"]
                matched_keywords = rule["keywords"]
                break

        if not matched_category:
            # No existing category matches — extract category name from summary
            # Look for Chinese topic keywords
            topic_keywords = {
                "翻译": ("Translation requests", ["翻译", "translate"]),
                "数据库": ("Database query/SQL", ["sql", "数据库", "查询"]),
                "闲聊": ("Simple chat/greetings", ["你好", "闲聊"]),
                "配置": ("Configuration/setup", ["配置", "设置"]),
                "部署": ("Deployment/DevOps", ["部署", "deploy"]),
                "测试": ("Testing/QA", ["测试", "test"]),
                "监控": ("Monitoring/observability", ["监控", "monitor"]),
                "文档": ("Documentation", ["文档", "doc"]),
            }
            for kw, (cat, kws) in topic_keywords.items():
                if kw in summary_lower:
                    matched_category = cat
                    matched_keywords = kws
                    break

        if not matched_category:
            return  # No category detected

        # Determine the correct route — must have a desired route to evolve
        if not desired_route:
            return  # Cannot determine correct route, skip evolution
        new_route = desired_route
        if new_route == failed_route:
            return  # Same route, no correction needed

        # Read current MEMORY.md and add/update rule
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                content = f.read()

            patterns_header = "## Routing Patterns Learned"
            if patterns_header not in content:
                return

            # Build new rule with keywords
            kw_str = ", ".join(matched_keywords) if matched_keywords else ""
            kw_section = f" [{kw_str}]" if kw_str else ""
            new_rule = f"- {matched_category}{kw_section} → {new_route} (learned from feedback)"

            # Check if rule for this category already exists
            existing_rules = self._parse_routing_rules_from_memory()
            for existing in existing_rules:
                if existing["category"].lower() == matched_category.lower():
                    # Update existing rule with new route — use line-by-line replacement
                    # to avoid content.replace() matching wrong lines
                    old_line = existing["raw_line"]
                    lines = content.split("\n")
                    new_lines = []
                    replaced = False
                    for line in lines:
                        if not replaced and line.strip() == old_line.strip():
                            new_lines.append(new_rule)
                            replaced = True
                        else:
                            new_lines.append(line)
                    if replaced:
                        content = "\n".join(new_lines)
                        with open(MEMORY_FILE, "w", encoding="utf-8") as fw:
                            fw.write(content)
                        logger.info(f"Skill evolved: updated rule '{matched_category}' → {new_route}")
                        self._memory_context_cached_at = 0
                    return

            # New category — insert after patterns header
            parts = content.split(patterns_header, 1)
            before = parts[0]
            after = parts[1] if len(parts) > 1 else ""
            first_newline = after.index("\n") if "\n" in after else len(after)
            new_content = before + patterns_header + after[:first_newline] + f"\n{new_rule}" + after[first_newline:]

            if len(new_content) <= 1500:
                with open(MEMORY_FILE, "w", encoding="utf-8") as fw:
                    fw.write(new_content)
                logger.info(f"Skill evolved: added rule '{new_rule}'")
                self._memory_context_cached_at = 0

        except Exception as e:
            logger.warning(f"Failed to evolve rule from feedback: {e}")

    def _extract_desired_route(self, summary: str) -> str:
        """Extract the desired route from a feedback summary.

        Looks for patterns like "应走direct_local" or "should be gateway".
        Returns the route name or empty string.
        """
        import re as _re
        valid_routes = {"direct_local", "gateway", "agent_chain", "local_inference", "multimodal"}

        # Chinese patterns: "应走X", "应该走X", "应路由到X"
        cn_patterns = [
            r"应走\s*(\w+)",
            r"应该走\s*(\w+)",
            r"应路由到?\s*(\w+)",
            r"应该是\s*(\w+)",
        ]
        for pat in cn_patterns:
            m = _re.search(pat, summary)
            if m:
                route = m.group(1).strip().lower()
                if route in valid_routes:
                    return route

        # English patterns: "should be X", "should route to X"
        en_patterns = [
            r"should\s+(?:be|route\s+to)\s+(\w+[\w_]*)",
        ]
        for pat in en_patterns:
            m = _re.search(pat, summary, _re.IGNORECASE)
            if m:
                route = m.group(1).strip().lower()
                if route in valid_routes:
                    return route

        return ""

    # ── Utility ──────────────────────────────────────────────────────

    def get_skills(self) -> Dict:
        """List available skills in the official agent."""
        payload = {
            "model": "hermes-agent",
            "messages": [
                {"role": "user", "content": "Use skills_list to show all available skills"},
            ],
            "stream": False,
        }

        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"{self.api_url}/v1/chat/completions",
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()
            result = resp.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {"skills_response": content}
        except Exception as e:
            return {"error": str(e)}

    def _extract_json_from_text(self, text: str) -> Optional[Dict]:
        """Try to extract JSON object from text that may contain markdown or extra content."""
        json_match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass
        return None

    def _extract_route_from_text(self, text: str, request: Dict) -> Dict:
        """Extract routing decision from natural language when 3B model doesn't output JSON.

        Hermes Agent's large system prompt can cause 3B models to ignore JSON format
        and respond in natural language instead. This method uses keyword matching
        as a fallback to extract the route path.
        """
        text_lower = text.lower()
        prompt = request.get("prompt", "")
        constraints = request.get("constraints", {})
        require_local = False
        if isinstance(constraints, dict):
            require_local = constraints.get("require_local", False)

        # Priority 1: Privacy constraint (absolute)
        if require_local:
            return {"route_path": "local_inference", "complexity_score": 50,
                    "reason": "Privacy constraint enforced (text fallback)"}

        # Priority 2: Memory-based override (highest confidence — learned rules)
        memory_override = self._check_memory_override(prompt, "")
        if memory_override:
            return {"route_path": memory_override, "complexity_score": 50,
                    "reason": f"Memory rule matched (text fallback)"}

        # Priority 3: Check if text mentions a specific route
        if "gateway" in text_lower:
            return {"route_path": "gateway", "complexity_score": 60,
                    "reason": "Extracted gateway from text response"}
        if "multimodal" in text_lower or "multi-modal" in text_lower:
            return {"route_path": "multimodal", "complexity_score": 55,
                    "reason": "Extracted multimodal from text response"}
        if "agent_chain" in text_lower or "agent chain" in text_lower:
            return {"route_path": "agent_chain", "complexity_score": 60,
                    "reason": "Extracted agent_chain from text response"}
        if "local_inference" in text_lower or "local inference" in text_lower:
            return {"route_path": "local_inference", "complexity_score": 50,
                    "reason": "Extracted local_inference from text response"}
        if "direct_local" in text_lower or "direct local" in text_lower:
            return {"route_path": "direct_local", "complexity_score": 5,
                    "reason": "Extracted direct_local from text response"}

        # Priority 4: Keyword-based inference from response content
        # 3B models may describe the route without using the exact name
        gateway_kw = {"部署", "volcano", "集群", "代码执行", "批处理", "多步", "工具调用", "deploy", "batch", "code"}
        multimodal_kw = {"图片", "ocr", "视觉", "音频", "视频", "image", "vision", "audio", "video"}
        if any(kw in text_lower for kw in gateway_kw):
            return {"route_path": "gateway", "complexity_score": 55,
                    "reason": "Inferred gateway from text keywords (fallback)"}
        if any(kw in text_lower for kw in multimodal_kw):
            return {"route_path": "multimodal", "complexity_score": 55,
                    "reason": "Inferred multimodal from text keywords (fallback)"}

        # Default: gateway for general queries
        return {"route_path": "gateway", "complexity_score": 25,
                "reason": "Default gateway (text fallback)"}

    def _check_cloud_available(self) -> bool:
        """Check if cloud models are actually available via Bridge (cached for 30s)."""
        now = time.time()
        if hasattr(self, '_cloud_available') and self._cloud_available is not None and (now - self._cloud_check_time) < 30:
            return self._cloud_available
        try:
            url = f"{self.hermes_router_url}/proxy/bridge/health"
            logger.info(f"Checking cloud availability via: {url}")
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url)
                logger.info(f"Cloud check response: status={resp.status_code}")
                data = resp.json()
            cloud_count = data.get("cloud_models", 0)
            ogw_reachable = data.get("official_gateway_reachable", False)
            self._cloud_available = cloud_count > 0 and ogw_reachable
            self._cloud_check_time = now
            logger.info(f"Cloud availability: {cloud_count} cloud models, OGW reachable={ogw_reachable} → available={self._cloud_available}")
        except Exception as e:
            logger.warning(f"Cloud availability check failed: {type(e).__name__}: {e}")
            # Don't cache failure — allow retry on next request
            self._cloud_available = False
            self._cloud_check_time = now - 25  # Allow retry in 5s
        return self._cloud_available

    def _post_validate_route(self, routing: Dict, request: Dict) -> Dict:
        """Post-validate routing decision.

        New 4-category routing strategy:
        - 单步问答 (simple Q&A) → direct_local (Ollama/vLLM)
        - 多步批处理 (multi-step, code, Volcano) → gateway (OfficialGW→Agent→Volcano)
        - 多模态 (image/audio/video) → multimodal (多模态专用模型)
        - 隐私敏感 → local_inference (本地模型, 隐私保护)
        """
        route = routing.get("route_path", "")
        prompt = request.get("prompt", "")
        constraints = request.get("constraints", {})
        req_type = request.get("type", "chat")
        has_tools = bool(request.get("tools"))
        complexity = routing.get("complexity_score", 0)
        if isinstance(complexity, str):
            try:
                complexity = float(complexity)
            except (ValueError, TypeError):
                complexity = 0

        logger.debug("[PostValidate] 输入: route=%s complexity=%s type=%s has_tools=%s prompt='%.60s'",
                     route, complexity, req_type, has_tools, prompt[:60])

        # Rule 1: Privacy override (absolute)
        require_local = False
        if isinstance(constraints, dict):
            require_local = constraints.get("require_local", False)

        # Also detect privacy by keywords (when constraints not set)
        privacy_keywords = [
            "个人信息", "隐私", "脱敏", "敏感数据", "内部财务", "医疗", "病历",
            "患者", "诊断报告", "身份证", "银行卡", "密码", "薪资", "人事",
            "privacy", "sensitive", "personal data", "PII", "PHI", "medical record",
        ]
        has_privacy = require_local or any(kw in prompt for kw in privacy_keywords)

        if has_privacy:
            matched_kws = [kw for kw in privacy_keywords if kw in prompt] if not require_local else ["require_local"]
            logger.info("[PostValidate] Rule1隐私覆盖: %s → local_inference (匹配: %s)", route, matched_kws[:3])
            routing["route_path"] = "local_inference"
            routing["reason"] = f"Override: privacy-sensitive request (was {route})"
            routing["post_validated"] = True
            return routing

        # Rule 2: Multimodal detection
        multimodal_keywords = [
            "图片", "图像", "照片", "截图", "OCR", "识别图片", "看图", "视觉",
            "音频", "语音", "录音", "视频", "画面", "摄像头", "影像",
            "image", "photo", "picture", "screenshot", "vision", "ocr",
            "audio", "voice", "video", "camera", "multimodal",
            "分析图片", "描述图片", "图片中", "图中", "截图中的",
            "转换为文字", "关键帧", "物体并分类",
        ]
        has_multimodal = any(kw in prompt for kw in multimodal_keywords)
        # Also check if request has image/audio attachments
        attachments = request.get("attachments", [])
        if attachments:
            has_multimodal = True

        if has_multimodal:
            matched_kws = [kw for kw in multimodal_keywords if kw in prompt]
            logger.info("[PostValidate] Rule2多模态检测: %s → multimodal (匹配关键词: %s)",
                        route, matched_kws[:3])
            routing["route_path"] = "multimodal"
            routing["reason"] = f"Override: multimodal request (was {route})"
            routing["post_validated"] = True
            return routing

        # Rule 3: Multi-step batch / Volcano / Agent-related detection
        multi_step_keywords = [
            "多步骤", "多步", "批处理", "批量", "自主", "设计", "规划", "执行计划",
            "自主执行", "架构", "方案", "调研", "流程", "编排", "工作流", "pipeline",
            "Volcano", "volcano", "分布式", "微服务", "部署", "发布",
        ]
        has_multi_step = any(kw in prompt for kw in multi_step_keywords)

        is_gateway_route = (
            has_tools
            or req_type in ("code", "code_execution", "tool_call")
            or has_multi_step
        )

        # Note: LLM-returned complexity_score is NOT used as a hard threshold
        # here because 3B models are unreliable at scoring — they often give
        # simple Q&A a score of 40+ which would incorrectly route to gateway.
        # Instead, we rely on deterministic keyword matching + request type.
        # The LLM's complexity_score is kept in the routing result for
        # observability but does not override the keyword-based decision.

        # Rule 4: Simple single-step Q&A → direct_local
        if is_gateway_route:
            correct_route = "gateway"
        else:
            correct_route = "direct_local"

        if route != correct_route:
            matched_kws = [kw for kw in multi_step_keywords if kw in prompt] if has_multi_step else []
            logger.info("[PostValidate] Rule4路由修正: %s → %s (is_gateway=%s, has_multi_step=%s kws=%s, has_tools=%s, type=%s, complexity=%s)",
                        route, correct_route, is_gateway_route, has_multi_step, matched_kws[:3], has_tools, req_type, complexity)
            routing["route_path"] = correct_route
            routing["reason"] = f"Override: {'multi-step/agent' if is_gateway_route else 'single-step Q&A'} request (was {route}, complexity={complexity})"
            routing["post_validated"] = True
            return routing

        return routing

    def _check_memory_override(self, prompt: str, current_route: str) -> str:
        """Check if Memory contains a learned rule that overrides the current route.

        Uses _parse_routing_rules_from_memory to get structured rules with
        keywords, then matches prompt against each rule's keyword list.
        Returns the override route or empty string.

        Priority logic: if the current route already matches a rule's keywords,
        don't allow a lower-priority rule to override it. This prevents
        "hello, how are you?" from being overridden from direct_local→gateway
        just because "how" appears in a General Q&A rule.
        """
        rules = self._parse_routing_rules_from_memory()
        prompt_lower = prompt.lower()

        # First check: does the current route already have a matching rule?
        current_match_strength = 0
        for rule in rules:
            if rule["route"] == current_route:
                keywords = rule["keywords"]
                if keywords and any(kw in prompt_lower for kw in keywords):
                    # Count how many keywords match — more matches = stronger
                    current_match_strength = max(
                        current_match_strength,
                        sum(1 for kw in keywords if kw in prompt_lower)
                    )

        # Second check: find the best override candidate
        best_override = ""
        best_override_strength = 0
        for rule in rules:
            target_route = rule["route"]
            if target_route == current_route:
                continue  # Same route, no override needed

            keywords = rule["keywords"]
            if not keywords:
                continue  # No keywords to match against

            # Check if any keyword matches the prompt
            match_strength = sum(1 for kw in keywords if kw in prompt_lower)
            if match_strength > 0 and match_strength > best_override_strength:
                best_override = target_route
                best_override_strength = match_strength

        # Only override if the new rule matches more strongly than current
        if best_override and best_override_strength > current_match_strength:
            return best_override

        return ""

    def _prompt_matches_category(self, prompt: str, category: str) -> bool:
        """Check if a prompt matches a Memory-learned category pattern.

        Uses _parse_routing_rules_from_memory to find the rule's keywords.
        """
        rules = self._parse_routing_rules_from_memory()
        prompt_lower = prompt.lower()

        for rule in rules:
            if rule["category"].lower() == category.lower():
                keywords = rule["keywords"]
                if keywords:
                    return any(kw in prompt_lower for kw in keywords)
                # Fallback: category name words
                category_words = [w for w in category.replace("/", " ").split() if len(w) > 2]
                return any(w in prompt_lower for w in category_words)

        # Category not found in Memory — fallback to category name words
        category_words = [w for w in category.replace("/", " ").split() if len(w) > 2]
        if category_words:
            return any(w in prompt_lower for w in category_words)

        return False

    def get_stats(self) -> Dict:
        health = self.health_check()
        with self._feedback_lock:
            queue_size = len(self._feedback_queue)
        return {
            "api_url": self.api_url,
            "available": health.get("status") == "ok",
            "health": health,
            "mode": "ollama_direct_with_memory" if self.use_direct_ollama else "hermes_agent_system_message",
            "ollama_model": self.ollama_model,
            "use_direct_ollama": self.use_direct_ollama,
            "session_warmed_up": self._session_warmed_up,
            "session_id": self._session_id,
            "feedback_queue_size": queue_size,
        }

    # ── Mode Switch ──────────────────────────────────────────────────

    def set_mode(self, mode: str) -> Dict:
        """Switch routing mode at runtime.

        Args:
            mode: "ollama_direct" or "hermes_agent"

        Returns:
            Dict with new mode and status info.
        """
        mode = mode.strip().lower()
        if mode in ("ollama_direct", "ollama", "direct"):
            self.use_direct_ollama = True
            logger.info("Routing mode switched to: ollama_direct_with_memory")
        elif mode in ("hermes_agent", "hermes", "agent"):
            self.use_direct_ollama = False
            # Trigger lazy warmup on next request if not yet done
            logger.info("Routing mode switched to: hermes_agent_system_message")
        else:
            return {
                "error": f"Unknown mode '{mode}'. Use 'ollama_direct' or 'hermes_agent'",
                "current_mode": "ollama_direct_with_memory" if self.use_direct_ollama else "hermes_agent_system_message",
            }

        return {
            "mode": "ollama_direct_with_memory" if self.use_direct_ollama else "hermes_agent_system_message",
            "use_direct_ollama": self.use_direct_ollama,
            "description": (
                "Ollama direct + Memory closed-loop (~3.7s, stable)"
                if self.use_direct_ollama
                else "Hermes Agent + system_message + Agent cache (~5.0s, full framework)"
            ),
        }
