import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HERMES_AGENT_LLM_URL = os.getenv("HERMES_AGENT_LLM_URL", "http://localhost:11434/v1/chat/completions")
HERMES_AGENT_LLM_MODEL = os.getenv("HERMES_AGENT_LLM_MODEL", "ollama/qwen2.5:3b")
HERMES_AGENT_LLM_TIMEOUT = int(os.getenv("HERMES_AGENT_LLM_TIMEOUT", "5"))
HERMES_AGENT_ENABLED = os.getenv("HERMES_AGENT_ENABLED", "true").lower() in ("true", "1", "yes")


class SkillStatus(Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEPRECATED = "deprecated"


class AgentDecision(Enum):
    USE_SKILL = "use_skill"
    COMBINE_SKILLS = "combine_skills"
    OVERRIDE = "override"
    FALLBACK = "fallback"
    EXPLORE = "explore"


@dataclass
class SkillCondition:
    pattern_type: str
    pattern_value: str
    weight: float = 1.0

    def matches(self, request: Dict) -> bool:
        if self.pattern_type == "request_type":
            return request.get("type", "chat") == self.pattern_value
        elif self.pattern_type == "keyword":
            return self.pattern_value in request.get("prompt", "")
        elif self.pattern_type == "has_tools":
            return bool(request.get("tools")) == (self.pattern_value == "true")
        elif self.pattern_type == "require_local":
            constraints = request.get("constraints") or {}
            return constraints.get("require_local", False) == (self.pattern_value == "true")
        elif self.pattern_type == "complexity_range":
            parts = self.pattern_value.split("-")
            if len(parts) == 2:
                lo, hi = float(parts[0]), float(parts[1])
                score = request.get("_rule_complexity_score", 0)
                return lo <= score <= hi
        elif self.pattern_type == "priority_range":
            p = request.get("priority", 3)
            return str(p) in self.pattern_value.split(",")
        return False


@dataclass
class SkillAction:
    route_path: str
    model_hint: Optional[str] = None
    parameters: Optional[Dict] = None
    fallback_path: Optional[str] = None


@dataclass
class RoutingSkill:
    name: str
    conditions: List[SkillCondition]
    action: SkillAction
    confidence: float = 0.5
    hit_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    total_latency_ms: int = 0
    status: SkillStatus = SkillStatus.ACTIVE
    created_at: float = 0.0
    updated_at: float = 0.0
    source: str = "rule"
    tags: List[str] = field(default_factory=list)

    def match_score(self, request: Dict) -> float:
        if self.status != SkillStatus.ACTIVE:
            return 0.0
        if not self.conditions:
            return 0.0
        total_weight = 0.0
        matched_weight = 0.0
        for cond in self.conditions:
            total_weight += cond.weight
            if cond.matches(request):
                matched_weight += cond.weight
        if total_weight == 0:
            return 0.0
        return (matched_weight / total_weight) * self.confidence

    def record_result(self, success: bool, latency_ms: int):
        self.hit_count += 1
        self.updated_at = time.time()
        if success:
            self.success_count += 1
            self.total_latency_ms += latency_ms
        else:
            self.fail_count += 1
            self.total_latency_ms += latency_ms
        if self.hit_count > 0:
            self.confidence = self.success_count / self.hit_count
        if self.confidence < 0.2 and self.hit_count >= 5:
            self.status = SkillStatus.SUSPENDED
            logger.info(f"Skill '{self.name}' suspended: confidence={self.confidence:.2f} after {self.hit_count} hits")

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "conditions": [{"type": c.pattern_type, "value": c.pattern_value, "weight": c.weight} for c in self.conditions],
            "action": {"route_path": self.action.route_path, "model_hint": self.action.model_hint,
                       "fallback_path": self.action.fallback_path},
            "confidence": round(self.confidence, 4),
            "hit_count": self.hit_count,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "avg_latency_ms": round(self.total_latency_ms / self.hit_count) if self.hit_count > 0 else 0,
            "status": self.status.value,
            "source": self.source,
            "tags": self.tags,
        }


AGENT_SYSTEM_PROMPT = """你是 Hermes 路由 Agent 的决策大脑。你的任务是基于可用 Skills 和请求特征做出路由决策。

可用 Skills（按置信度排序）:
{skills_text}

请求信息:
- 类型: {request_type}
- 提示词: {prompt_preview}
- 优先级: {priority}
- 含工具: {has_tools}
- 隐私约束: {require_local}
- 规则评分: {rule_score}

请分析请求，选择最佳路由方案。输出JSON格式：
{{
  "decision": "use_skill|combine_skills|override|fallback|explore",
  "selected_skill": "skill_name或null",
  "combined_skills": ["skill1", "skill2"],
  "override_path": "direct_local|gateway|agent_chain|direct_cloud",
  "override_model": "model_name或null",
  "reasoning": "决策理由",
  "confidence": 0.0-1.0
}}

决策规则：
- use_skill: 有高置信度匹配的 Skill 时选择
- combine_skills: 请求匹配多个 Skill 时组合
- override: LLM 判断 Skill 不适合，覆盖推荐路径
- fallback: 无匹配 Skill，使用默认策略
- explore: 探索新路径以收集数据"""


class HermesAgent:
    def __init__(self, llm_url: Optional[str] = None, llm_model: Optional[str] = None,
                 llm_timeout: Optional[int] = None, enabled: Optional[bool] = None):
        self.llm_url = llm_url or HERMES_AGENT_LLM_URL
        self.llm_model = llm_model or HERMES_AGENT_LLM_MODEL
        self.llm_timeout = llm_timeout or HERMES_AGENT_LLM_TIMEOUT
        self.enabled = enabled if enabled is not None else HERMES_AGENT_ENABLED

        self.skills: Dict[str, RoutingSkill] = {}
        self._call_count = 0
        self._success_count = 0
        self._fail_count = 0
        self._total_latency_ms = 0
        self._last_error = None
        self._decision_cache: Dict[str, Dict] = {}
        self._cache_max = 100
        self._exploration_rate = 0.05
        self._min_confidence = 0.4
        self._evolution_log: List[Dict] = []

    def register_skill(self, skill: RoutingSkill):
        self.skills[skill.name] = skill
        logger.info(f"Agent skill registered: {skill.name} (confidence={skill.confidence:.2f}, source={skill.source})")

    def unregister_skill(self, name: str):
        if name in self.skills:
            del self.skills[name]
            logger.info(f"Agent skill unregistered: {name}")

    def decide(self, request: Dict) -> Dict:
        if not self.enabled:
            return self._fallback_decision(request)

        rule_score = request.get("_rule_complexity_score", 0)
        if rule_score < 5:
            return self._fallback_decision(request)

        cache_key = self._cache_key(request)
        if cache_key in self._decision_cache:
            cached = self._decision_cache[cache_key]
            logger.debug(f"Agent decision cache hit: {cache_key[:32]}")
            return cached

        candidate_skills = self._find_candidate_skills(request)

        if not candidate_skills:
            decision = self._fallback_decision(request)
            decision["agent_decision"] = "fallback"
            decision["reasoning"] = "无匹配Skill，使用默认策略"
            return decision

        best_skill_name, best_score = candidate_skills[0]

        if best_score >= 0.7:
            decision = self._use_skill_decision(request, best_skill_name, best_score, candidate_skills)
        elif best_score >= self._min_confidence:
            decision = self._llm_decide(request, candidate_skills)
        else:
            decision = self._fallback_decision(request)
            decision["agent_decision"] = "fallback"
            decision["reasoning"] = f"最佳Skill置信度{best_score:.2f}低于阈值{self._min_confidence}"

        if self._should_explore(request, decision):
            decision = self._explore_decision(request, decision)

        if len(self._decision_cache) >= self._cache_max:
            oldest = next(iter(self._decision_cache))
            del self._decision_cache[oldest]
        self._decision_cache[cache_key] = decision

        return decision

    def _find_candidate_skills(self, request: Dict) -> List[Tuple[str, float]]:
        scores = []
        for name, skill in self.skills.items():
            if skill.status != SkillStatus.ACTIVE:
                continue
            score = skill.match_score(request)
            if score > 0:
                scores.append((name, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    def _use_skill_decision(self, request: Dict, best_skill_name: str,
                             best_score: float, candidates: List[Tuple[str, float]]) -> Dict:
        skill = self.skills[best_skill_name]

        if len(candidates) >= 2 and candidates[1][1] >= best_score * 0.8:
            second_name = candidates[1][1]
            second_skill = self.skills[candidates[1][0]]
            if (skill.action.route_path != second_skill.action.route_path
                    and second_skill.confidence > skill.confidence):
                return self._llm_decide(request, candidates)

        decision = {
            "agent_decision": "use_skill",
            "selected_skill": best_skill_name,
            "route_path": skill.action.route_path,
            "model_hint": skill.action.model_hint,
            "fallback_path": skill.action.fallback_path,
            "confidence": best_score,
            "reasoning": f"Skill '{best_skill_name}' 高置信度匹配 (score={best_score:.2f}, conf={skill.confidence:.2f})",
            "skill_confidence": skill.confidence,
            "skill_hit_count": skill.hit_count,
        }

        return decision

    def _llm_decide(self, request: Dict, candidates: List[Tuple[str, float]]) -> Dict:
        start = time.time()
        try:
            skills_text = self._format_skills_for_prompt(candidates)
            prompt_preview = request.get("prompt", "")[:300]
            req_type = request.get("type", "chat")
            priority = request.get("priority", 3)
            has_tools = bool(request.get("tools"))
            constraints = request.get("constraints") or {}
            require_local = constraints.get("require_local", False)
            rule_score = request.get("_rule_complexity_score", 0)

            system_prompt = AGENT_SYSTEM_PROMPT.format(
                skills_text=skills_text,
                request_type=req_type,
                prompt_preview=prompt_preview,
                priority=priority,
                has_tools=has_tools,
                require_local=require_local,
                rule_score=rule_score,
            )

            user_message = f"请为这个请求做出路由决策。"

            result = self._call_llm(system_prompt, user_message)

            latency_ms = int((time.time() - start) * 1000)
            self._total_latency_ms += latency_ms

            if result:
                self._success_count += 1
                decision = self._parse_llm_decision(result, candidates)
                decision["llm_latency_ms"] = latency_ms
                decision["agent_decision"] = decision.get("agent_decision", "llm_decide")
                return decision
            else:
                self._fail_count += 1
                return self._skill_fallback_decision(candidates)

        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            self._total_latency_ms += latency_ms
            self._fail_count += 1
            self._last_error = str(e)
            logger.warning(f"Agent LLM decide failed: {e}")
            return self._skill_fallback_decision(candidates)

    def _call_llm(self, system_prompt: str, user_message: str) -> Optional[Dict]:
        try:
            import httpx

            model_name = self.llm_model.split("/")[-1] if "/" in self.llm_model else self.llm_model
            payload = {
                "model": model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.1,
                "max_tokens": 300,
                "stream": False,
            }

            with httpx.Client(timeout=float(self.llm_timeout)) as client:
                resp = client.post(self.llm_url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return self._parse_json_response(content)

        except Exception as e:
            self._last_error = str(e)
            logger.warning(f"Agent LLM call failed: {e}")
            return None

    def _parse_json_response(self, content: str) -> Optional[Dict]:
        try:
            json_str = content.strip()
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
            return json.loads(json_str)
        except (json.JSONDecodeError, KeyError, IndexError):
            logger.warning(f"Agent LLM response parse failed: {content[:100]}")
            return None

    def _parse_llm_decision(self, llm_result: Dict, candidates: List[Tuple[str, float]]) -> Dict:
        decision_type = llm_result.get("decision", "fallback")
        selected_skill = llm_result.get("selected_skill")
        combined_skills = llm_result.get("combined_skills", [])
        override_path = llm_result.get("override_path")
        override_model = llm_result.get("override_model")
        reasoning = llm_result.get("reasoning", "")
        confidence = llm_result.get("confidence", 0.5)
        confidence = max(0.0, min(1.0, float(confidence)))

        if decision_type == "use_skill" and selected_skill and selected_skill in self.skills:
            skill = self.skills[selected_skill]
            return {
                "agent_decision": "use_skill",
                "selected_skill": selected_skill,
                "route_path": skill.action.route_path,
                "model_hint": skill.action.model_hint or override_model,
                "fallback_path": skill.action.fallback_path,
                "confidence": confidence,
                "reasoning": reasoning,
                "skill_confidence": skill.confidence,
            }

        elif decision_type == "combine_skills" and combined_skills:
            return self._combine_skills_decision(combined_skills, reasoning, confidence)

        elif decision_type == "override" and override_path:
            return {
                "agent_decision": "override",
                "selected_skill": None,
                "route_path": override_path,
                "model_hint": override_model,
                "fallback_path": None,
                "confidence": confidence,
                "reasoning": reasoning,
            }

        else:
            return self._skill_fallback_decision(candidates)

    def _combine_skills_decision(self, skill_names: List[str], reasoning: str,
                                  confidence: float) -> Dict:
        active_skills = []
        for name in skill_names:
            if name in self.skills and self.skills[name].status == SkillStatus.ACTIVE:
                active_skills.append(self.skills[name])

        if not active_skills:
            return {
                "agent_decision": "fallback",
                "route_path": "gateway",
                "model_hint": None,
                "fallback_path": None,
                "confidence": 0.3,
                "reasoning": "组合Skills均不可用，使用默认路径",
            }

        best = max(active_skills, key=lambda s: s.confidence)
        return {
            "agent_decision": "combine_skills",
            "selected_skill": best.name,
            "combined_skills": [s.name for s in active_skills],
            "route_path": best.action.route_path,
            "model_hint": best.action.model_hint,
            "fallback_path": best.action.fallback_path,
            "confidence": confidence,
            "reasoning": reasoning,
        }

    def _skill_fallback_decision(self, candidates: List[Tuple[str, float]]) -> Dict:
        if candidates:
            best_name, best_score = candidates[0]
            skill = self.skills[best_name]
            return {
                "agent_decision": "use_skill",
                "selected_skill": best_name,
                "route_path": skill.action.route_path,
                "model_hint": skill.action.model_hint,
                "fallback_path": skill.action.fallback_path,
                "confidence": best_score * 0.8,
                "reasoning": f"LLM决策失败，使用最佳匹配Skill '{best_name}' (score={best_score:.2f})",
            }
        return self._fallback_decision(request=None)

    def _fallback_decision(self, request: Optional[Dict]) -> Dict:
        path = "gateway"
        if request:
            constraints = request.get("constraints") or {}
            if constraints.get("require_local"):
                path = "direct_local"
            elif request.get("tools"):
                path = "gateway"
            score = request.get("_rule_complexity_score", 0)
            if score >= 60:
                path = "agent_chain"
            elif score < 15:
                path = "direct_local"
        return {
            "agent_decision": "fallback",
            "selected_skill": None,
            "route_path": path,
            "model_hint": None,
            "fallback_path": None,
            "confidence": 0.3,
            "reasoning": "无可用Skill，使用规则降级策略",
        }

    def _should_explore(self, request: Dict, decision: Dict) -> bool:
        if decision.get("confidence", 1.0) >= 0.8:
            return False
        if request.get("_rule_complexity_score", 0) < 15:
            return False
        import random
        return random.random() < self._exploration_rate

    def _explore_decision(self, request: Dict, current_decision: Dict) -> Dict:
        current_path = current_decision.get("route_path", "gateway")
        all_paths = ["direct_local", "gateway", "agent_chain", "direct_cloud"]
        alternatives = [p for p in all_paths if p != current_path]
        import random
        new_path = random.choice(alternatives)
        return {
            **current_decision,
            "agent_decision": "explore",
            "original_path": current_path,
            "route_path": new_path,
            "reasoning": f"探索路径: {current_path} → {new_path} (exploration_rate={self._exploration_rate:.2f})",
        }

    def record_feedback(self, skill_name: Optional[str], route_path: str,
                        success: bool, latency_ms: int, request: Optional[Dict] = None):
        if skill_name and skill_name in self.skills:
            self.skills[skill_name].record_result(success, latency_ms)

        self._try_evolve(request, success, latency_ms)

        if skill_name and skill_name in self.skills:
            skill = self.skills[skill_name]
            if skill.status == SkillStatus.SUSPENDED and skill.confidence >= 0.5:
                skill.status = SkillStatus.ACTIVE
                logger.info(f"Skill '{skill_name}' reactivated: confidence recovered to {skill.confidence:.2f}")

    def _try_evolve(self, request: Optional[Dict], success: bool, latency_ms: int):
        if not request or not success:
            return

        if self._call_count % 20 != 0:
            return

        if self._exploration_rate > 0.02 and self._success_count / max(self._call_count, 1) > 0.9:
            old = self._exploration_rate
            self._exploration_rate *= 0.9
            self._log_evolution("exploration_decreased", old, self._exploration_rate,
                                f"High success rate, reducing exploration")
        elif self._exploration_rate < 0.15 and self._success_count / max(self._call_count, 1) < 0.7:
            old = self._exploration_rate
            self._exploration_rate = min(0.15, self._exploration_rate * 1.2)
            self._log_evolution("exploration_increased", old, self._exploration_rate,
                                f"Low success rate, increasing exploration")

    def _log_evolution(self, event: str, old_val: float, new_val: float, reason: str):
        entry = {
            "event": event,
            "old_value": round(old_val, 4),
            "new_value": round(new_val, 4),
            "reason": reason,
            "timestamp": time.time(),
        }
        self._evolution_log.append(entry)
        if len(self._evolution_log) > 200:
            self._evolution_log = self._evolution_log[-200:]
        logger.info(f"Agent evolution: {event} {old_val:.4f}→{new_val:.4f} ({reason})")

    def _format_skills_for_prompt(self, candidates: List[Tuple[str, float]]) -> str:
        lines = []
        for name, score in candidates[:8]:
            skill = self.skills[name]
            success_rate = f"{skill.success_count / skill.hit_count:.0%}" if skill.hit_count > 0 else "N/A"
            lines.append(
                f"- {name}: path={skill.action.route_path}, "
                f"confidence={skill.confidence:.2f}, "
                f"match_score={score:.2f}, "
                f"hits={skill.hit_count}, "
                f"success_rate={success_rate}"
            )
        return "\n".join(lines) if lines else "无可用Skills"

    def _cache_key(self, request: Dict) -> str:
        prompt = request.get("prompt", "")[:80]
        req_type = request.get("type", "chat")
        has_tools = bool(request.get("tools"))
        constraints = request.get("constraints") or {}
        require_local = bool(constraints.get("require_local", False))
        return f"{req_type}:{has_tools}:{require_local}:{hash(prompt)}"

    def get_stats(self) -> Dict:
        active_skills = [s for s in self.skills.values() if s.status == SkillStatus.ACTIVE]
        suspended_skills = [s for s in self.skills.values() if s.status == SkillStatus.SUSPENDED]
        return {
            "enabled": self.enabled,
            "llm_model": self.llm_model,
            "llm_timeout": self.llm_timeout,
            "skills": {
                "total": len(self.skills),
                "active": len(active_skills),
                "suspended": len(suspended_skills),
                "items": [s.to_dict() for s in sorted(self.skills.values(), key=lambda s: s.confidence, reverse=True)],
            },
            "decisions": {
                "call_count": self._call_count,
                "success_count": self._success_count,
                "fail_count": self._fail_count,
                "success_rate": round(self._success_count / self._call_count, 4) if self._call_count > 0 else 0,
                "avg_latency_ms": round(self._total_latency_ms / self._call_count) if self._call_count > 0 else 0,
                "cache_size": len(self._decision_cache),
            },
            "exploration_rate": round(self._exploration_rate, 4),
            "min_confidence": self._min_confidence,
            "evolution_log": self._evolution_log[-10:],
            "last_error": self._last_error,
        }

    def bootstrap_from_rules(self):
        from hermes.skill_bootstrap import bootstrap_routing_skills
        skills = bootstrap_routing_skills()
        for skill in skills:
            self.register_skill(skill)
        logger.info(f"Agent bootstrapped with {len(skills)} skills from routing rules")
        return len(skills)
