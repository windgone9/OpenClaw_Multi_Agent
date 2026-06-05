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

# Memory file path
MEMORY_FILE = os.path.expanduser("~/.hermes/memories/MEMORY.md")

# Routing prompt template — rules are dynamically generated from MEMORY.md
ROUTING_PROMPT_TEMPLATE = """你是路由决策助手。根据请求内容选择最优执行路径。

重要：不要调用任何工具！直接输出JSON结果。

路由选项(必须选其一):
1. direct_local - 简单闲聊、打招呼、翻译 → Gateway→本地常驻模型(Ollama/vLLM)
2. gateway - 一般对话、信息查询、分析比较 → Gateway→云端模型
3. agent_chain - 复杂多步规划、代码生成、编程实现、自主执行 → Gateway→Official OpenClaw→Volcano资源调度
4. local_inference - 隐私敏感、必须本地执行 → Gateway→本地常驻模型(Ollama/vLLM,隐私保护)

注意：所有路由均通过Gateway统一管理，区别在于：
- direct_local/local_inference → Gateway选择已注册的本地常驻模型(Ollama/vLLM)
- gateway → Gateway选择云端模型(Moonshot/DeepSeek等)
- agent_chain → Gateway转发到Official OpenClaw服务，可监控，支持Volcano资源调度

{routing_rules}

只输出JSON，不要输出其他内容: {{"route_path":"选项","complexity_score":0-100,"reason":"原因"}}"""


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

    @property
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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
                self._memory_context_cache = ""
                self._memory_context_cached_at = now
                return ""

            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                content = f.read()

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
            return self._memory_context_cache

        except Exception as e:
            logger.warning(f"Failed to load memory context: {e}")
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
                valid = {"direct_local", "gateway", "agent_chain", "local_inference"}
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

    def _build_routing_system_message(self) -> str:
        """Build routing rules from MEMORY.md — the single source of truth.

        Generates the routing rules section of the system prompt dynamically
        from Memory's 'Routing Patterns Learned' and 'Key Rules' sections.
        No hardcoded routing rules — all rules come from MEMORY.md.
        """
        rules = self._parse_routing_rules_from_memory()
        memory_ctx = self._load_memory_context()

        if rules:
            # Build human-readable routing rules from Memory
            rule_lines = []
            for r in rules:
                kw_display = ", ".join(r["keywords"][:8]) if r["keywords"] else r["category"]
                rule_lines.append(f"- {r['category']}({kw_display}) → {r['route']}")
            routing_rules = "路由规则(从Memory学习，按优先级):\n" + "\n".join(rule_lines)
        elif memory_ctx:
            # Fallback: use raw memory context
            routing_rules = f"路由规则(从Memory学习):\n{memory_ctx}"
        else:
            # Last resort: minimal default rules
            routing_rules = "路由规则:\n- 简单闲聊 → direct_local\n- 代码/编程 → agent_chain\n- 隐私敏感 → local_inference\n- 其他 → gateway"

        return ROUTING_PROMPT_TEMPLATE.format(routing_rules=routing_rules)

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
            system_msg = self._build_routing_system_message()
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
        if self.use_direct_ollama:
            # Primary: Ollama direct (fast, ~1-2s)
            try:
                return self._route_via_ollama_direct(request)
            except Exception as e:
                logger.warning(f"Ollama direct routing failed, falling back to Hermes Agent: {e}")
                try:
                    return self._route_via_hermes_agent(request)
                except Exception as e2:
                    logger.warning(f"Hermes Agent routing also failed: {e2}")
                    return {"route_path": "gateway", "reason": f"All routing failed: {e2}", "official_agent_routed": False}
        else:
            # Primary: Hermes Agent (full framework, ~5s)
            try:
                return self._route_via_hermes_agent(request)
            except Exception as e:
                logger.warning(f"Hermes Agent routing failed, falling back to Ollama: {e}")
                try:
                    return self._route_via_ollama_direct(request)
                except Exception as e2:
                    logger.warning(f"Ollama routing also failed: {e2}")
                    return {"route_path": "gateway", "reason": f"All routing failed: {e2}", "official_agent_routed": False}

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

        # Build routing rules with memory context
        system_message = self._build_routing_system_message()

        user_msg = (
            f"判断路由:\n"
            f"内容: {prompt[:200]}\n"
            f"类型: {req_type}\n"
            f"优先级: {priority}\n"
            f"有工具: {has_tools}\n"
            f"需本地: {require_local}"
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

        try:
            routing = json.loads(content)
        except json.JSONDecodeError:
            routing = self._extract_json_from_text(content)

        if not routing:
            # 3B model may return natural language instead of JSON under
            # Hermes Agent's large system prompt — extract route from text
            routing = self._extract_route_from_text(content, request)

        valid_paths = {"direct_local", "gateway", "agent_chain", "local_inference"}
        if routing.get("route_path") not in valid_paths:
            routing["route_path"] = "gateway"

        # Post-validation: 3B model may return valid JSON but wrong route
        # (e.g. "write a quicksort" misrouted to direct_local because of
        # greeting prefix). Override when code keywords are clearly present.
        routing = self._post_validate_route(routing, request)

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
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(
                    f"{self.ollama_url}/v1/chat/completions",
                    json=payload,
                )
                resp.raise_for_status()
            elapsed_ms = int((time.time() - start) * 1000)

            result = resp.json()
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")

            try:
                routing = json.loads(content)
            except json.JSONDecodeError:
                routing = self._extract_json_from_text(content)

            if not routing:
                routing = self._extract_route_from_text(content, request)

            valid_paths = {"direct_local", "gateway", "agent_chain", "local_inference"}
            if routing.get("route_path") not in valid_paths:
                routing["route_path"] = "gateway"

            # Apply same post-validation as Hermes Agent path
            routing = self._post_validate_route(routing, request)

            routing["agent_llm_latency_ms"] = elapsed_ms
            routing["official_agent_routed"] = True
            routing["agent_decision"] = "ollama_direct_with_memory"
            routing["agent_confidence"] = 0.7
            routing["skill_matched"] = "routing-decision"
            routing["memory_context_used"] = bool(self._load_memory_context())
            return routing

        except httpx.TimeoutException:
            logger.warning("Ollama routing timeout, falling back")
            return {"route_path": "gateway", "reason": "Ollama timeout", "official_agent_routed": False}
        except Exception as e:
            logger.error(f"Ollama routing error: {e}")
            return {"route_path": "gateway", "reason": f"Ollama error: {e}", "official_agent_routed": False}

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
        """Background thread: drain feedback queue and write to Hermes Memory."""
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

        feedback_dir = os.path.expanduser("~/.hermes/routing_feedback")
        os.makedirs(feedback_dir, exist_ok=True)
        feedback_file = os.path.join(feedback_dir, "feedback.jsonl")

        try:
            with open(feedback_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return True
        except Exception as e:
            logger.error(f"Failed to write feedback locally: {e}")
            return False

    def _write_feedback_to_memory(self, skill_name: str, route_path: str,
                                  success: bool, latency_ms: int,
                                  request_summary: str) -> bool:
        """Write routing feedback directly to Hermes Memory file.

        Bypasses the Agent's memory tool (which has a 1375 char limit and
        replace-only semantics) by directly appending to MEMORY.md.
        Keeps the file concise with a rolling window of recent feedback.
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

        # Update feedback section
        feedback_header = "## Feedback History"
        if feedback_header in content:
            # Append to existing feedback section
            parts = content.split(feedback_header, 1)
            before = parts[0]
            after = parts[1] if len(parts) > 1 else ""

            # Keep only last 10 feedback lines
            feedback_lines = [l for l in after.strip().split("\n") if l.strip().startswith("-")]
            feedback_lines.append(line)
            feedback_lines = feedback_lines[-10:]  # Rolling window

            new_content = before + feedback_header + "\n" + "\n".join(feedback_lines) + "\n"
        else:
            # Add feedback section
            new_content = content.rstrip() + f"\n\n{feedback_header}\n{line}\n"

        # Write back (keep under 1500 chars for Hermes compatibility)
        if len(new_content) > 1500:
            # Trim oldest feedback lines only (not routing rules!)
            feedback_lines = feedback_lines[1:]  # Remove oldest feedback
            new_content = before + feedback_header + "\n" + "\n".join(feedback_lines) + "\n"

        try:
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                f.write(new_content)
            logger.info(f"Feedback written to MEMORY.md: {route_path} {success_str} {latency_ms}ms")

            # Invalidate memory context cache so next request picks up changes
            self._memory_context_cached_at = 0

            # Skill evolution: extract new rule from failed feedback
            if not success and request_summary:
                self._evolve_rule_from_feedback(route_path, request_summary)

            return True
        except Exception as e:
            logger.error(f"Failed to write MEMORY.md: {e}")
            return False

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
        valid_routes = {"direct_local", "gateway", "agent_chain", "local_inference"}

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
        if "agent_chain" in text_lower or "agent chain" in text_lower:
            return {"route_path": "agent_chain", "complexity_score": 60,
                    "reason": "Extracted agent_chain from text response"}
        if "local_inference" in text_lower or "local inference" in text_lower:
            return {"route_path": "local_inference", "complexity_score": 50,
                    "reason": "Extracted local_inference from text response"}
        if "direct_local" in text_lower or "direct local" in text_lower:
            return {"route_path": "direct_local", "complexity_score": 5,
                    "reason": "Extracted direct_local from text response"}

        # Default: gateway for general queries
        return {"route_path": "gateway", "complexity_score": 25,
                "reason": "Default gateway (text fallback)"}

    def _post_validate_route(self, routing: Dict, request: Dict) -> Dict:
        """Post-validate routing decision using Memory rules.

        All routing rules come from MEMORY.md — no hardcoded keyword lists.
        Priority order (highest first):
        1. Privacy override (absolute, from constraints)
        2. Memory-learned override (from MEMORY.md Routing Patterns)
        3. Type-based override (from MEMORY.md Key Rules)
        """
        route = routing.get("route_path", "")
        prompt = request.get("prompt", "")
        constraints = request.get("constraints", {})

        # Rule 1: Privacy override (absolute, regardless of model output)
        require_local = False
        if isinstance(constraints, dict):
            require_local = constraints.get("require_local", False)
        if require_local and route != "local_inference":
            routing["route_path"] = "local_inference"
            routing["reason"] = f"Override: require_local (was {route})"
            routing["post_validated"] = True
            return routing

        # Rule 2: Memory-learned override — scan all Memory rules for prompt match
        memory_override = self._check_memory_override(prompt, route)
        if memory_override:
            routing["route_path"] = memory_override
            routing["reason"] = f"Override: memory-learned rule (was {route})"
            routing["post_validated"] = True
            return routing

        # Rule 3: Type-based override (from Key Rules)
        req_type = request.get("type", "chat")
        if req_type in ("code", "code_execution") and route != "agent_chain":
            routing["route_path"] = "agent_chain"
            routing["reason"] = f"Override: type={req_type} (was {route})"
            routing["post_validated"] = True
            return routing

        return routing

    def _check_memory_override(self, prompt: str, current_route: str) -> str:
        """Check if Memory contains a learned rule that overrides the current route.

        Uses _parse_routing_rules_from_memory to get structured rules with
        keywords, then matches prompt against each rule's keyword list.
        Returns the override route or empty string.
        """
        rules = self._parse_routing_rules_from_memory()
        prompt_lower = prompt.lower()

        for rule in rules:
            target_route = rule["route"]
            if target_route == current_route:
                continue  # Same route, no override needed

            keywords = rule["keywords"]
            if not keywords:
                continue  # No keywords to match against

            # Check if any keyword matches the prompt
            if any(kw in prompt_lower for kw in keywords):
                return target_route

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
