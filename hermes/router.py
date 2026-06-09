import json
import logging
import os
import random
import sqlite3
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

from hermes.llm_enhancer import LLMEnhancer
from hermes.agent import HermesAgent
from hermes.official_agent_adapter import OfficialHermesAdapter

logger = logging.getLogger(__name__)

HERMES_DATA_DIR = Path(os.getenv("HERMES_DATA_DIR", Path.home() / ".hermes_adapter"))
HERMES_DB_PATH = HERMES_DATA_DIR / "routing_memory.db"


class RoutePath(Enum):
    GATEWAY = "gateway"
    AGENT_CHAIN = "agent_chain"
    DIRECT_LOCAL = "direct_local"
    DIRECT_CLOUD = "direct_cloud"
    LOCAL_INFERENCE = "local_inference"


class ComplexityLevel(Enum):
    SIMPLE = "simple"
    MODERATE = "moderate"
    COMPLEX = "complex"
    HIGHLY_COMPLEX = "highly_complex"


@dataclass
class RoutingScore:
    total: float
    breakdown: Dict[str, float]
    level: ComplexityLevel
    recommended_path: RoutePath


@dataclass
class ModelPerformance:
    name: str
    total_requests: int = 0
    success_requests: int = 0
    failed_requests: int = 0
    total_latency_ms: int = 0
    avg_latency_ms: float = 0.0
    success_rate: float = 1.0
    last_used: float = 0.0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    ema_latency: float = 0.0
    ema_success_rate: float = 1.0
    cost_total: float = 0.0
    tool_call_count: int = 0
    tool_call_success: int = 0


@dataclass
class PathPerformance:
    path: RoutePath
    total_requests: int = 0
    success_requests: int = 0
    total_latency_ms: int = 0
    avg_latency_ms: float = 0.0
    success_rate: float = 1.0
    ema_latency: float = 0.0
    ema_success_rate: float = 1.0


@dataclass
class LearningState:
    alpha: float = 0.1
    beta: float = 0.05
    gamma: float = 0.01
    decay_rate: float = 0.995
    exploration_rate: float = 0.1
    min_exploration: float = 0.02
    max_exploration: float = 0.3
    learning_iterations: int = 0
    last_decay: float = 0.0


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routing_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    request_type TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    prompt_preview TEXT NOT NULL,
    route_path TEXT NOT NULL,
    selected_model TEXT,
    complexity_score REAL NOT NULL,
    complexity_level TEXT NOT NULL,
    success INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    cost REAL NOT NULL DEFAULT 0,
    has_tool_call INTEGER NOT NULL DEFAULT 0,
    tool_names TEXT,
    skill_matched TEXT,
    exploration INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_routing_session ON routing_history(session_id);
CREATE INDEX IF NOT EXISTS idx_routing_path ON routing_history(route_path);
CREATE INDEX IF NOT EXISTS idx_routing_model ON routing_history(selected_model);
CREATE INDEX IF NOT EXISTS idx_routing_success ON routing_history(success);
CREATE INDEX IF NOT EXISTS idx_routing_created ON routing_history(created_at);

CREATE VIRTUAL TABLE IF NOT EXISTS routing_history_fts USING fts5(
    prompt_preview,
    request_type,
    route_path,
    selected_model,
    content=routing_history,
    content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS routing_history_ai AFTER INSERT ON routing_history BEGIN
    INSERT INTO routing_history_fts(rowid, prompt_preview, request_type, route_path, selected_model)
    VALUES (new.id, new.prompt_preview, new.request_type, new.route_path, new.selected_model);
END;

CREATE TABLE IF NOT EXISTS routing_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name TEXT NOT NULL UNIQUE,
    pattern_type TEXT NOT NULL,
    pattern_detail TEXT NOT NULL,
    recommended_path TEXT NOT NULL,
    recommended_model TEXT,
    confidence REAL NOT NULL DEFAULT 0.5,
    hit_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_pattern ON routing_skills(pattern_type);
CREATE INDEX IF NOT EXISTS idx_skills_confidence ON routing_skills(confidence DESC);

CREATE TABLE IF NOT EXISTS learning_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    reason TEXT,
    iteration INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evolution_created ON evolution_log(created_at);
"""


class RoutingMemory:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or HERMES_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA synchronous=NORMAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.commit()

    def record_routing(self, session_id: str, request_type: str,
                       prompt_hash: str, prompt_preview: str,
                       route_path: str, selected_model: Optional[str],
                       complexity_score: float, complexity_level: str,
                       success: bool, latency_ms: int, cost: float = 0.0,
                       has_tool_call: bool = False, tool_names: Optional[str] = None,
                       skill_matched: Optional[str] = None,
                       exploration: bool = False) -> int:
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO routing_history
               (session_id, request_type, prompt_hash, prompt_preview,
                route_path, selected_model, complexity_score, complexity_level,
                success, latency_ms, cost, has_tool_call, tool_names,
                skill_matched, exploration, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, request_type, prompt_hash, prompt_preview[:200],
             route_path, selected_model, complexity_score, complexity_level,
             1 if success else 0, latency_ms, cost,
             1 if has_tool_call else 0, tool_names, skill_matched,
             1 if exploration else 0, time.time()),
        )
        conn.commit()
        return cur.lastrowid

    def search_similar(self, prompt_preview: str, limit: int = 5) -> List[Dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT rh.* FROM routing_history rh
                   JOIN routing_history_fts fts ON rh.id = fts.rowid
                   WHERE routing_history_fts MATCH ?
                   ORDER BY rh.created_at DESC LIMIT ?""",
                (prompt_preview[:100], limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                """SELECT * FROM routing_history
                   WHERE prompt_preview LIKE ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"%{prompt_preview[:50]}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_recent(self, limit: int = 50) -> List[Dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM routing_history ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_path_stats(self, path: str, since: float = 0) -> Dict:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as successes,
                      AVG(latency_ms) as avg_latency,
                      SUM(cost) as total_cost
               FROM routing_history
               WHERE route_path=? AND created_at>?
               """,
            (path, since),
        ).fetchone()
        if row and row["total"] > 0:
            return {
                "total": row["total"],
                "success_rate": row["successes"] / row["total"],
                "avg_latency_ms": round(row["avg_latency"] or 0, 1),
                "total_cost": round(row["total_cost"] or 0, 4),
            }
        return {"total": 0, "success_rate": 0, "avg_latency_ms": 0, "total_cost": 0}

    def get_model_stats(self, model: str, since: float = 0) -> Dict:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as successes,
                      AVG(latency_ms) as avg_latency,
                      SUM(cost) as total_cost,
                      SUM(has_tool_call) as tool_calls,
                      SUM(CASE WHEN success=1 AND has_tool_call=1 THEN 1 ELSE 0 END) as tool_successes
               FROM routing_history
               WHERE selected_model=? AND created_at>?
               """,
            (model, since),
        ).fetchone()
        if row and row["total"] > 0:
            return {
                "total": row["total"],
                "success_rate": row["successes"] / row["total"],
                "avg_latency_ms": round(row["avg_latency"] or 0, 1),
                "total_cost": round(row["total_cost"] or 0, 4),
                "tool_call_rate": row["tool_calls"] / row["total"],
                "tool_call_success_rate": row["tool_successes"] / max(row["tool_calls"], 1),
            }
        return {"total": 0, "success_rate": 0, "avg_latency_ms": 0, "total_cost": 0, "tool_call_rate": 0, "tool_call_success_rate": 0}

    def save_skill(self, skill_name: str, pattern_type: str,
                   pattern_detail: str, recommended_path: str,
                   recommended_model: Optional[str] = None,
                   confidence: float = 0.5) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO routing_skills
               (skill_name, pattern_type, pattern_detail, recommended_path,
                recommended_model, confidence, hit_count, success_count,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
               ON CONFLICT(skill_name) DO UPDATE SET
                 confidence=excluded.confidence,
                 updated_at=excluded.updated_at""",
            (skill_name, pattern_type, pattern_detail, recommended_path,
             recommended_model, confidence, time.time(), time.time()),
        )
        conn.commit()

    def update_skill(self, skill_name: str, success: bool) -> None:
        conn = self._get_conn()
        conn.execute(
            """UPDATE routing_skills
               SET hit_count=hit_count+1,
                   success_count=success_count+?,
                   confidence=CAST(success_count+? AS REAL)/CAST(hit_count+1 AS REAL),
                   updated_at=?
               WHERE skill_name=?""",
            (1 if success else 0, 1 if success else 0, time.time(), skill_name),
        )
        conn.commit()

    def find_skill(self, pattern_type: str, pattern_detail: str,
                   min_confidence: float = 0.6) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM routing_skills
               WHERE pattern_type=? AND pattern_detail=? AND confidence>=?
               ORDER BY confidence DESC LIMIT 1""",
            (pattern_type, pattern_detail, min_confidence),
        ).fetchone()
        return dict(row) if row else None

    def find_skill_by_request_type(self, request_type: str,
                                    min_confidence: float = 0.6) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM routing_skills
               WHERE pattern_type='request_type' AND pattern_detail=? AND confidence>=?
               ORDER BY confidence DESC LIMIT 1""",
            (request_type, min_confidence),
        ).fetchone()
        return dict(row) if row else None

    def find_skill_by_keyword(self, keyword: str,
                               min_confidence: float = 0.6) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM routing_skills
               WHERE pattern_type='keyword' AND pattern_detail=? AND confidence>=?
               ORDER BY confidence DESC LIMIT 1""",
            (keyword, min_confidence),
        ).fetchone()
        return dict(row) if row else None

    def get_all_skills(self) -> List[Dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM routing_skills ORDER BY confidence DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def save_learning_state(self, key: str, value: str) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO learning_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, time.time()),
        )
        conn.commit()

    def load_learning_state(self, key: str, default: str = "") -> str:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM learning_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def log_evolution(self, event: str, old_val: Optional[float],
                      new_val: Optional[float], reason: str,
                      iteration: int) -> None:
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO evolution_log (event, old_value, new_value, reason, iteration, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event, old_val, new_val, reason, iteration, time.time()),
        )
        conn.commit()

    def get_evolution_log(self, limit: int = 20) -> List[Dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM evolution_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_total_count(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) as cnt FROM routing_history").fetchone()
        return row["cnt"] if row else 0


class HermesRouter:
    def __init__(self, db_path: Optional[Path] = None, llm_enhancer: Optional[LLMEnhancer] = None,
                 hermes_agent: Optional[HermesAgent] = None,
                 official_agent: Optional[OfficialHermesAdapter] = None):
        self.memory = RoutingMemory(db_path)
        self.model_performance: Dict[str, ModelPerformance] = {}
        self.path_performance: Dict[RoutePath, PathPerformance] = {
            RoutePath.GATEWAY: PathPerformance(path=RoutePath.GATEWAY),
            RoutePath.AGENT_CHAIN: PathPerformance(path=RoutePath.AGENT_CHAIN),
            RoutePath.DIRECT_LOCAL: PathPerformance(path=RoutePath.DIRECT_LOCAL),
            RoutePath.DIRECT_CLOUD: PathPerformance(path=RoutePath.DIRECT_CLOUD),
            RoutePath.LOCAL_INFERENCE: PathPerformance(path=RoutePath.LOCAL_INFERENCE),
        }
        self.learning = LearningState()
        self.complexity_threshold: float = 40.0
        self._evolution_log: List[Dict] = []
        self._max_evolution_log: int = 200
        self._session_id = f"hermes-{int(time.time())}"
        self.llm_enhancer = llm_enhancer
        self.hermes_agent = hermes_agent
        self.official_agent = official_agent
        self._cloud_available: Optional[bool] = None
        self._cloud_check_time: float = 0
        self._load_state()

    def _load_state(self):
        threshold_str = self.memory.load_learning_state("complexity_threshold", "40.0")
        try:
            self.complexity_threshold = float(threshold_str)
        except ValueError:
            self.complexity_threshold = 40.0

        alpha_str = self.memory.load_learning_state("alpha", "0.1")
        try:
            self.learning.alpha = float(alpha_str)
        except ValueError:
            pass

        exploration_str = self.memory.load_learning_state("exploration_rate", "0.1")
        try:
            self.learning.exploration_rate = float(exploration_str)
        except ValueError:
            pass

        iterations_str = self.memory.load_learning_state("learning_iterations", "0")
        try:
            self.learning.learning_iterations = int(iterations_str)
        except ValueError:
            pass

        logger.info(f"Hermes state loaded: threshold={self.complexity_threshold}, "
                     f"alpha={self.learning.alpha}, exploration={self.learning.exploration_rate}, "
                     f"iterations={self.learning.learning_iterations}")

    def _persist_state(self):
        self.memory.save_learning_state("complexity_threshold", str(self.complexity_threshold))
        self.memory.save_learning_state("alpha", str(self.learning.alpha))
        self.memory.save_learning_state("exploration_rate", str(self.learning.exploration_rate))
        self.memory.save_learning_state("learning_iterations", str(self.learning.learning_iterations))

    def score_complexity(self, request: Dict) -> RoutingScore:
        breakdown = {}

        req_type = request.get("type", "chat")
        type_scores = {
            "chat": 5, "completion": 5, "tool_call": 35,
            "code_execution": 40, "code": 45,
            "embedding": 10, "image": 30, "audio": 30,
        }
        breakdown["request_type"] = type_scores.get(req_type, 10)

        tools = request.get("tools", [])
        tool_score = 0
        if tools:
            tool_score = 30 + (len(tools) - 1) * 10
        breakdown["tool_calls"] = tool_score

        prompt = request.get("prompt", "")
        prompt_len = len(prompt)
        if prompt_len <= 50:
            len_score = 0
        elif prompt_len <= 200:
            len_score = 5
        elif prompt_len <= 500:
            len_score = 10
        elif prompt_len <= 1000:
            len_score = 15
        else:
            len_score = 18
        breakdown["prompt_length"] = len_score

        high_keywords = ["多步骤", "自主", "设计", "规划", "执行计划", "自主执行"]
        moderate_keywords = ["分析", "比较", "评估", "优化", "搜索", "查询"]
        keyword_score = 0
        matched_keyword = None
        for kw in high_keywords:
            if kw in prompt:
                keyword_score = 20
                matched_keyword = kw
                break
        if keyword_score == 0:
            for kw in moderate_keywords:
                if kw in prompt:
                    keyword_score = 10
                    matched_keyword = kw
                    break
        breakdown["keywords"] = keyword_score

        context = request.get("context", [])
        context_score = min(len(context) * 5, 15) if context else 0
        breakdown["context"] = context_score

        priority = request.get("priority", 3)
        priority_score = 0
        if priority == 1:
            priority_score = 10
        elif priority == 2:
            priority_score = 5
        elif priority == 4:
            priority_score = -3
        elif priority >= 5:
            priority_score = -5
        breakdown["priority"] = priority_score

        total = sum(breakdown.values())

        if total < 20:
            level = ComplexityLevel.SIMPLE
        elif total < 40:
            level = ComplexityLevel.MODERATE
        elif total < 60:
            level = ComplexityLevel.COMPLEX
        else:
            level = ComplexityLevel.HIGHLY_COMPLEX

        recommended_path = self._decide_path(total, request)

        return RoutingScore(
            total=total,
            breakdown=breakdown,
            level=level,
            recommended_path=recommended_path,
        )

    def _check_cloud_available(self) -> bool:
        """Check if cloud models are actually available via Bridge (cached for 30s)."""
        now = time.time()
        if self._cloud_available is not None and (now - self._cloud_check_time) < 30:
            return self._cloud_available
        try:
            import httpx
            resp = httpx.get("http://localhost:3001/health", timeout=5.0)
            data = resp.json()
            cloud_count = data.get("cloud_models", 0)
            ogw_reachable = data.get("official_gateway_reachable", False)
            self._cloud_available = cloud_count > 0 and ogw_reachable
            self._cloud_check_time = now
            logger.info(f"Cloud availability check: {cloud_count} cloud models, OGW reachable={ogw_reachable} → available={self._cloud_available}")
        except Exception as e:
            logger.warning(f"Cloud availability check failed: {e}")
            self._cloud_available = False
            self._cloud_check_time = now - 25  # Allow retry in 5s
        return self._cloud_available

    def _decide_path(self, score: float, request: Dict) -> RoutePath:
        has_tools = bool(request.get("tools"))
        constraints = request.get("constraints") or {}
        require_local = constraints.get("require_local", False)

        if require_local:
            return RoutePath.LOCAL_INFERENCE

        # Agent-related requests: tools, code, multi-step → OfficialGW (cloud)
        if has_tools:
            return RoutePath.GATEWAY

        req_type = request.get("type", "chat")
        if req_type in ("code", "code_execution", "tool_call"):
            return RoutePath.GATEWAY

        if score >= self.complexity_threshold:
            return RoutePath.GATEWAY

        # High-complexity keywords indicate agent-related tasks regardless of score
        prompt = request.get("prompt", "")
        high_complexity_keywords = ["多步骤", "自主", "设计", "规划", "执行计划", "自主执行", "架构", "方案", "调研"]
        if any(kw in prompt for kw in high_complexity_keywords):
            return RoutePath.GATEWAY

        # Everything else: local model (Ollama/vLLM)
        return RoutePath.DIRECT_LOCAL

    def _recalc_level(self, total: float) -> ComplexityLevel:
        if total < 20:
            return ComplexityLevel.SIMPLE
        elif total < 40:
            return ComplexityLevel.MODERATE
        elif total < 60:
            return ComplexityLevel.COMPLEX
        else:
            return ComplexityLevel.HIGHLY_COMPLEX

    def _match_skill(self, request: Dict) -> Optional[Dict]:
        req_type = request.get("type", "chat")
        skill = self.memory.find_skill_by_request_type(req_type, min_confidence=0.65)
        if skill:
            return skill

        prompt = request.get("prompt", "")
        high_keywords = ["多步骤", "自主", "设计", "规划", "执行计划", "自主执行"]
        moderate_keywords = ["分析", "比较", "评估", "优化", "搜索", "查询"]
        for kw in high_keywords + moderate_keywords:
            if kw in prompt:
                skill = self.memory.find_skill_by_keyword(kw, min_confidence=0.65)
                if skill:
                    return skill

        return None

    def _learn_from_memory(self, request: Dict) -> Optional[Dict]:
        prompt = request.get("prompt", "")
        similar = self.memory.search_similar(prompt[:100], limit=3)
        if not similar:
            return None

        best = None
        best_score = 0
        for record in similar:
            if record["success"] == 1:
                score = 1.0 / max(record["latency_ms"], 1)
                if score > best_score:
                    best_score = score
                    best = record

        return best

    def route(self, request: Dict) -> Dict:
        score = self.score_complexity(request)

        if self.official_agent and self.official_agent.get_stats().get("available"):
            return self._route_via_official_agent(request, score)

        if self.hermes_agent and self.hermes_agent.enabled:
            return self._route_via_agent(request, score)

        llm_result = None
        llm_enhanced = False
        if self.llm_enhancer and self.llm_enhancer.enabled:
            request_with_score = dict(request)
            request_with_score["_rule_complexity_score"] = score.total
            llm_result = self.llm_enhancer.classify(request_with_score)
            if llm_result:
                llm_enhanced = True
                merged = self.llm_enhancer.merge_scores(
                    {"total": score.total, "breakdown": score.breakdown},
                    llm_result,
                )
                score = RoutingScore(
                    total=merged["total"],
                    breakdown=merged["breakdown"],
                    level=self._recalc_level(merged["total"]),
                    recommended_path=score.recommended_path,
                )
                logger.info(f"Hermes LLM enhanced: intent={llm_result.get('intent')}, "
                            f"score {score.total:.1f}, suggested_path={llm_result.get('suggested_path')}")

        path = score.recommended_path
        skill_matched = None
        memory_match = None

        if llm_enhanced and llm_result:
            requires_local = llm_result.get("requires_local", False)
            requires_tools = llm_result.get("requires_tools", False)
            llm_suggested = llm_result.get("suggested_path")
            llm_confidence = llm_result.get("confidence", 0.5)

            if requires_local:
                path = RoutePath.DIRECT_LOCAL
                logger.info(f"Hermes LLM override: requires_local=True → direct_local")
            elif requires_tools and llm_confidence >= 0.7:
                try:
                    path = RoutePath(llm_suggested) if llm_suggested else path
                    logger.info(f"Hermes LLM override: requires_tools=True → {path.value}")
                except ValueError:
                    pass
            elif llm_confidence >= 0.8:
                try:
                    llm_path = RoutePath(llm_suggested)
                    if llm_path != path:
                        path = llm_path
                        logger.info(f"Hermes LLM override: high confidence ({llm_confidence:.2f}) "
                                    f"→ {path.value} (rule suggested {score.recommended_path.value})")
                except ValueError:
                    pass

        skill_matched = self._match_skill(request)
        if skill_matched:
            try:
                skill_path = RoutePath(skill_matched["recommended_path"])
                skill_confidence = skill_matched.get("confidence", 0.5)
                if llm_enhanced and llm_result and llm_result.get("confidence", 0) >= 0.8 and skill_confidence < 0.8:
                    logger.info(f"Hermes LLM wins over skill: llm_conf={llm_result.get('confidence'):.2f} "
                                f"> skill_conf={skill_confidence:.2f}, keeping path={path.value}")
                else:
                    path = skill_path
                    logger.info(f"Hermes skill matched: {skill_matched['skill_name']} "
                                f"→ path={skill_path.value}, confidence={skill_confidence:.2f}")
            except ValueError:
                pass
        else:
            memory_match = self._learn_from_memory(request)
            if memory_match:
                try:
                    mem_path = RoutePath(memory_match["route_path"])
                    if memory_match["success"] == 1:
                        path = mem_path
                        logger.info(f"Hermes memory match: similar request succeeded via "
                                    f"{mem_path.value}, latency={memory_match['latency_ms']}ms")
                except ValueError:
                    pass

        if (self.learning.exploration_rate > 0
                and self.learning.learning_iterations > 10
                and not skill_matched
                and not llm_enhanced):
            if random.random() < self.learning.exploration_rate:
                alternatives = [p for p in RoutePath if p != path]
                path = random.choice(alternatives)
                logger.info(f"Hermes exploration: trying alternative path {path.value} "
                            f"instead of {score.recommended_path.value}")

        # Apply simplified routing override as final guard
        # This ensures consistent routing regardless of LLM/skill/memory decisions
        constraints_final = request.get("constraints") or {}
        require_local_final = constraints_final.get("require_local", False)

        if require_local_final:
            path = RoutePath.LOCAL_INFERENCE
        else:
            req_type_final = request.get("type", "chat")
            has_tools_final = bool(request.get("tools"))
            prompt_final = request.get("prompt", "")
            high_complexity_keywords_final = ["多步骤", "自主", "设计", "规划", "执行计划", "自主执行", "架构", "方案", "调研"]
            has_hck_final = any(kw in prompt_final for kw in high_complexity_keywords_final)

            is_agent_related_final = (
                has_tools_final
                or req_type_final in ("code", "code_execution", "tool_call")
                or score.total >= self.complexity_threshold
                or has_hck_final
            )
            correct_path_final = RoutePath.GATEWAY if is_agent_related_final else RoutePath.DIRECT_LOCAL

            if path != correct_path_final:
                logger.info(f"Simplified routing override: {path.value} → {correct_path_final.value} "
                            f"({'agent-related' if is_agent_related_final else 'simple'}, score={score.total})")
                path = correct_path_final

        selected_model = self._select_model(request, path)

        result = {
            "route_path": path.value,
            "complexity_score": score.total,
            "complexity_level": score.level.value,
            "score_breakdown": score.breakdown,
            "selected_model": selected_model,
            "threshold": self.complexity_threshold,
            "exploration": path != score.recommended_path,
            "skill_matched": skill_matched["skill_name"] if skill_matched else None,
            "memory_match": bool(memory_match),
            "reason": self._generate_reason(score, path, selected_model, skill_matched, memory_match),
        }

        if llm_enhanced and llm_result:
            result["llm_enhanced"] = True
            result["llm_intent"] = llm_result.get("intent")
            result["llm_confidence"] = llm_result.get("confidence")
            result["llm_suggested_path"] = llm_result.get("suggested_path")
            result["llm_latency_ms"] = llm_result.get("_llm_latency_ms")

        return result

    def _route_via_official_agent(self, request: Dict, score: RoutingScore) -> Dict:
        agent_result = self.official_agent.route_via_agent(request)

        try:
            path = RoutePath(agent_result.get("route_path", score.recommended_path.value))
        except ValueError:
            path = score.recommended_path

        # Apply simplified routing override (same logic as _post_validate_route)
        constraints = request.get("constraints") or {}
        require_local = constraints.get("require_local", False)

        if require_local:
            path = RoutePath.LOCAL_INFERENCE
            agent_result["reason"] = f"Override: require_local (was {path.value})"
        else:
            req_type = request.get("type", "chat")
            has_tools = bool(request.get("tools"))
            prompt = request.get("prompt", "")
            high_complexity_keywords = ["多步骤", "自主", "设计", "规划", "执行计划", "自主执行", "架构", "方案", "调研"]
            has_high_complexity_keywords = any(kw in prompt for kw in high_complexity_keywords)

            is_agent_related = (
                has_tools
                or req_type in ("code", "code_execution", "tool_call")
                or score.total >= self.complexity_threshold
                or has_high_complexity_keywords
            )
            correct_path = RoutePath.GATEWAY if is_agent_related else RoutePath.DIRECT_LOCAL

            if path != correct_path:
                original = path.value
                path = correct_path
                agent_result["reason"] = f"Override: {'agent-related' if is_agent_related else 'simple'} (was {original}, score={score.total})"

        selected_model = agent_result.get("selected_model") or self._select_model(request, path)

        result = {
            "route_path": path.value,
            "complexity_score": score.total,
            "complexity_level": score.level.value,
            "score_breakdown": score.breakdown,
            "selected_model": selected_model,
            "threshold": self.complexity_threshold,
            "skill_matched": agent_result.get("skill_matched"),
            "memory_match": False,
            "reason": f"[OfficialAgent] {agent_result.get('reason', '')}",
            "official_agent_routed": agent_result.get("official_agent_routed", True),
            "agent_routed": True,
            "agent_decision": agent_result.get("agent_decision", "official_agent"),
            "agent_confidence": agent_result.get("agent_confidence", 0.5),
        }

        if agent_result.get("agent_llm_latency_ms"):
            result["agent_llm_latency_ms"] = agent_result["agent_llm_latency_ms"]

        if agent_result.get("memory_context_used") is not None:
            result["memory_context_used"] = agent_result["memory_context_used"]

        logger.info(f"Official Hermes Agent routed: path={path.value}, "
                     f"confidence={agent_result.get('agent_confidence', 0):.2f}, "
                     f"reason={agent_result.get('reason', '')[:80]}")

        return result

    def _route_via_agent(self, request: Dict, score: RoutingScore) -> Dict:
        request_with_score = dict(request)
        request_with_score["_rule_complexity_score"] = score.total

        agent_decision = self.hermes_agent.decide(request_with_score)

        try:
            path = RoutePath(agent_decision.get("route_path", score.recommended_path.value))
        except ValueError:
            path = score.recommended_path

        # Apply simplified routing override (same logic as _route_via_official_agent)
        constraints = request.get("constraints") or {}
        require_local = constraints.get("require_local", False)

        if require_local:
            path = RoutePath.LOCAL_INFERENCE
            agent_decision["reasoning"] = f"Override: require_local"
        else:
            req_type = request.get("type", "chat")
            has_tools = bool(request.get("tools"))
            prompt = request.get("prompt", "")
            high_complexity_keywords = ["多步骤", "自主", "设计", "规划", "执行计划", "自主执行", "架构", "方案", "调研"]
            has_high_complexity_keywords = any(kw in prompt for kw in high_complexity_keywords)

            is_agent_related = (
                has_tools
                or req_type in ("code", "code_execution", "tool_call")
                or score.total >= self.complexity_threshold
                or has_high_complexity_keywords
            )
            correct_path = RoutePath.GATEWAY if is_agent_related else RoutePath.DIRECT_LOCAL

            if path != correct_path:
                original = path.value
                path = correct_path
                agent_decision["reasoning"] = f"Override: {'agent-related' if is_agent_related else 'simple'} (was {original}, score={score.total})"

        selected_model = agent_decision.get("model_hint") or self._select_model(request, path)

        selected_skill = agent_decision.get("selected_skill")
        agent_decision_type = agent_decision.get("agent_decision", "unknown")
        reasoning = agent_decision.get("reasoning", "")
        confidence = agent_decision.get("confidence", 0.5)

        result = {
            "route_path": path.value,
            "complexity_score": score.total,
            "complexity_level": score.level.value,
            "score_breakdown": score.breakdown,
            "selected_model": selected_model,
            "threshold": self.complexity_threshold,
            "exploration": agent_decision_type == "explore",
            "skill_matched": selected_skill,
            "memory_match": False,
            "reason": f"[Agent:{agent_decision_type}] {reasoning}",
            "agent_routed": True,
            "agent_decision": agent_decision_type,
            "agent_confidence": confidence,
        }

        if agent_decision.get("llm_latency_ms"):
            result["agent_llm_latency_ms"] = agent_decision["llm_latency_ms"]

        if agent_decision.get("combined_skills"):
            result["agent_combined_skills"] = agent_decision["combined_skills"]

        if agent_decision.get("original_path"):
            result["agent_original_path"] = agent_decision["original_path"]

        logger.info(f"Hermes Agent routed: decision={agent_decision_type}, "
                     f"skill={selected_skill}, path={path.value}, "
                     f"confidence={confidence:.2f}, reason={reasoning[:80]}")

        return result

    def _select_model(self, request: Dict, path: RoutePath) -> Optional[str]:
        has_tools = bool(request.get("tools"))

        candidates = []
        for name, perf in self.model_performance.items():
            if perf.consecutive_failures >= 3:
                continue
            if has_tools and perf.tool_call_count > 0 and perf.tool_call_success == 0:
                continue
            candidates.append(perf)

        if not candidates:
            return None

        if path == RoutePath.DIRECT_LOCAL:
            locals_ = [c for c in candidates if c.name and ("local" in c.name.lower() or "ollama" in c.name.lower())]
            if locals_:
                candidates = locals_
        elif path == RoutePath.LOCAL_INFERENCE:
            locals_ = [c for c in candidates if c.name and ("local" in c.name.lower() or "ollama" in c.name.lower())]
            if locals_:
                candidates = locals_
        elif path in (RoutePath.DIRECT_CLOUD, RoutePath.GATEWAY):
            if has_tools:
                tool_capable = [c for c in candidates if c.tool_call_count > 0 and c.tool_call_success > 0]
                if tool_capable:
                    candidates = tool_capable

        scored = []
        for c in candidates:
            s = self._compute_model_score(c, request)
            scored.append((c.name, s))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0] if scored else None

    def _compute_model_score(self, perf: ModelPerformance, request: Dict) -> float:
        success_weight = 0.4
        latency_weight = 0.3
        cost_weight = 0.2
        freshness_weight = 0.1

        success_score = perf.ema_success_rate

        latency_score = 1.0
        if perf.ema_latency > 0:
            latency_score = max(0.0, 1.0 - (perf.ema_latency / 60000.0))

        cost_score = 1.0
        if perf.cost_total > 0 and perf.total_requests > 0:
            avg_cost = perf.cost_total / perf.total_requests
            cost_score = max(0.0, 1.0 - avg_cost * 100)

        freshness_score = 1.0
        if perf.last_used > 0:
            age = time.time() - perf.last_used
            freshness_score = max(0.0, 1.0 - age / 3600.0)

        total = (success_weight * success_score
                 + latency_weight * latency_score
                 + cost_weight * cost_score
                 + freshness_weight * freshness_score)

        if perf.consecutive_successes > 3:
            total += 0.05 * min(perf.consecutive_successes - 3, 5)

        return total

    def record_result(self, model_name: str, path: str, success: bool,
                      latency_ms: int, cost: float = 0.0,
                      has_tool_call: bool = False,
                      request: Optional[Dict] = None,
                      routing: Optional[Dict] = None) -> None:
        if model_name not in self.model_performance:
            self.model_performance[model_name] = ModelPerformance(name=model_name)

        perf = self.model_performance[model_name]
        perf.total_requests += 1
        perf.last_used = time.time()
        perf.cost_total += cost

        if success:
            perf.success_requests += 1
            perf.consecutive_failures = 0
            perf.consecutive_successes += 1
        else:
            perf.failed_requests += 1
            perf.consecutive_failures += 1
            perf.consecutive_successes = 0

        if has_tool_call:
            perf.tool_call_count += 1
            if success:
                perf.tool_call_success += 1

        alpha = self.learning.alpha
        perf.ema_latency = alpha * latency_ms + (1 - alpha) * perf.ema_latency if perf.ema_latency > 0 else latency_ms
        current_rate = 1.0 if success else 0.0
        perf.ema_success_rate = alpha * current_rate + (1 - alpha) * perf.ema_success_rate
        perf.avg_latency_ms = perf.total_latency_ms / perf.total_requests if perf.total_requests > 0 else 0
        perf.total_latency_ms += latency_ms

        try:
            route_path = RoutePath(path)
        except ValueError:
            route_path = RoutePath.GATEWAY

        if route_path in self.path_performance:
            pp = self.path_performance[route_path]
            pp.total_requests += 1
            pp.total_latency_ms += latency_ms
            if success:
                pp.success_requests += 1
            pp.ema_latency = alpha * latency_ms + (1 - alpha) * pp.ema_latency if pp.ema_latency > 0 else latency_ms
            current_rate = 1.0 if success else 0.0
            pp.ema_success_rate = alpha * current_rate + (1 - alpha) * pp.ema_success_rate
            pp.avg_latency_ms = pp.total_latency_ms / pp.total_requests if pp.total_requests > 0 else 0

        if request and routing:
            prompt = request.get("prompt", "")
            tool_names = ""
            tools = request.get("tools", [])
            if tools:
                tool_names = ",".join(
                    t.get("function", {}).get("name", "") if isinstance(t, dict) else str(t)
                    for t in tools
                )
            self.memory.record_routing(
                session_id=self._session_id,
                request_type=request.get("type", "chat"),
                prompt_hash=str(hash(prompt)),
                prompt_preview=prompt,
                route_path=path,
                selected_model=model_name,
                complexity_score=routing.get("complexity_score", 0),
                complexity_level=routing.get("complexity_level", "unknown"),
                success=success,
                latency_ms=latency_ms,
                cost=cost,
                has_tool_call=has_tool_call,
                tool_names=tool_names,
                skill_matched=routing.get("skill_matched"),
                exploration=routing.get("exploration", False),
            )

        if routing and routing.get("skill_matched"):
            self.memory.update_skill(routing["skill_matched"], success)

        self._try_generate_skill(request, routing, success, path, model_name)

        if self.hermes_agent and self.hermes_agent.enabled and routing:
            skill_name = routing.get("skill_matched")
            self.hermes_agent.record_feedback(
                skill_name=skill_name,
                route_path=path,
                success=success,
                latency_ms=latency_ms,
                request=request,
            )

        if self.official_agent and routing and routing.get("official_agent_routed"):
            try:
                # For Memory feedback, judge routing decision quality by LLM latency
                # (not total latency which includes downstream service timeouts)
                llm_latency = routing.get("agent_llm_latency_ms", latency_ms)
                routing_success = llm_latency < 5000  # LLM decision < 5s = good routing
                self.official_agent.record_feedback_via_memory(
                    skill_name=routing.get("skill_matched"),
                    route_path=path,
                    success=routing_success,
                    latency_ms=llm_latency,
                    request_summary=request.get("prompt", "")[:50] if request else "",
                )
            except Exception as e:
                logger.warning(f"Failed to record feedback to official agent: {e}")

        self._evolve()
        self._persist_state()

    def _try_generate_skill(self, request: Optional[Dict], routing: Optional[Dict],
                            success: bool, path: str, model: str):
        if not request or not routing or not success:
            return

        req_type = request.get("type", "chat")
        existing = self.memory.find_skill_by_request_type(req_type, min_confidence=0.0)
        if existing:
            return

        recent = self.memory.get_recent(limit=20)
        type_records = [r for r in recent if r["request_type"] == req_type and r["success"] == 1]
        if len(type_records) < 3:
            return

        path_counts: Dict[str, int] = {}
        for r in type_records:
            p = r["route_path"]
            path_counts[p] = path_counts.get(p, 0) + 1

        best_path = max(path_counts, key=path_counts.get)
        confidence = path_counts[best_path] / len(type_records)

        if confidence >= 0.6:
            skill_name = f"auto_{req_type}_to_{best_path}"
            self.memory.save_skill(
                skill_name=skill_name,
                pattern_type="request_type",
                pattern_detail=req_type,
                recommended_path=best_path,
                recommended_model=model,
                confidence=confidence,
            )
            logger.info(f"Hermes skill generated: {skill_name} "
                        f"(confidence={confidence:.2f}, samples={len(type_records)})")

        prompt = request.get("prompt", "")
        high_keywords = ["多步骤", "自主", "设计", "规划", "执行计划", "自主执行"]
        moderate_keywords = ["分析", "比较", "评估", "优化", "搜索", "查询"]
        for kw in high_keywords + moderate_keywords:
            if kw in prompt:
                existing_kw = self.memory.find_skill_by_keyword(kw, min_confidence=0.0)
                if existing_kw:
                    continue
                kw_records = [r for r in recent if kw in (r.get("prompt_preview") or "") and r["success"] == 1]
                if len(kw_records) < 3:
                    continue
                kw_path_counts: Dict[str, int] = {}
                for r in kw_records:
                    p = r["route_path"]
                    kw_path_counts[p] = kw_path_counts.get(p, 0) + 1
                kw_best_path = max(kw_path_counts, key=kw_path_counts.get)
                kw_confidence = kw_path_counts[kw_best_path] / len(kw_records)
                if kw_confidence >= 0.6:
                    kw_skill_name = f"auto_keyword_{kw}_to_{kw_best_path}"
                    self.memory.save_skill(
                        skill_name=kw_skill_name,
                        pattern_type="keyword",
                        pattern_detail=kw,
                        recommended_path=kw_best_path,
                        recommended_model=model,
                        confidence=kw_confidence,
                    )
                    logger.info(f"Hermes keyword skill generated: {kw_skill_name} "
                                f"(confidence={kw_confidence:.2f})")

    def _evolve(self) -> None:
        self.learning.learning_iterations += 1

        if self.learning.learning_iterations % 50 == 0:
            self._adjust_threshold()

        if self.learning.learning_iterations % 100 == 0:
            self._adjust_exploration()

        if self.learning.learning_iterations % 200 == 0:
            self._decay_weights()

    def _adjust_threshold(self) -> None:
        since = time.time() - 3600
        gw_stats = self.memory.get_path_stats("gateway", since)
        agent_stats = self.memory.get_path_stats("agent_chain", since)

        if gw_stats["total"] < 5 or agent_stats["total"] < 5:
            return

        gw_rate = gw_stats["success_rate"]
        agent_rate = agent_stats["success_rate"]

        if agent_rate > gw_rate + 0.1:
            old = self.complexity_threshold
            self.complexity_threshold = max(20, self.complexity_threshold - 2)
            if old != self.complexity_threshold:
                self._log_evolution("threshold_lowered", old, self.complexity_threshold,
                                    f"Agent path outperforming (agent={agent_rate:.2f} > gateway={gw_rate:.2f})")
        elif gw_rate > agent_rate + 0.1:
            old = self.complexity_threshold
            self.complexity_threshold = min(60, self.complexity_threshold + 2)
            if old != self.complexity_threshold:
                self._log_evolution("threshold_raised", old, self.complexity_threshold,
                                    f"Gateway path outperforming (gateway={gw_rate:.2f} > agent={agent_rate:.2f})")

    def _adjust_exploration(self) -> None:
        since = time.time() - 3600
        total = 0
        successes = 0
        for p in RoutePath:
            stats = self.memory.get_path_stats(p.value, since)
            total += stats["total"]
            successes += int(stats["total"] * stats["success_rate"])

        if total < 20:
            return

        success_rate = successes / total

        if success_rate > 0.95:
            self.learning.exploration_rate = max(
                self.learning.min_exploration,
                self.learning.exploration_rate * 0.9
            )
        elif success_rate < 0.8:
            self.learning.exploration_rate = min(
                self.learning.max_exploration,
                self.learning.exploration_rate * 1.2
            )

    def _decay_weights(self) -> None:
        for perf in self.model_performance.values():
            perf.ema_latency *= self.learning.decay_rate
            perf.ema_success_rate = (1 - self.learning.gamma) * perf.ema_success_rate + self.learning.gamma

    def _log_evolution(self, event: str, old_val: float, new_val: float, reason: str) -> None:
        entry = {
            "event": event,
            "old_value": old_val,
            "new_value": new_val,
            "reason": reason,
            "iteration": self.learning.learning_iterations,
            "timestamp": time.time(),
        }
        self._evolution_log.append(entry)
        if len(self._evolution_log) > self._max_evolution_log:
            self._evolution_log = self._evolution_log[-self._max_evolution_log:]
        self.memory.log_evolution(event, old_val, new_val, reason, self.learning.learning_iterations)
        logger.info(f"Hermes evolution: {event} {old_val}→{new_val} ({reason})")

    def _generate_reason(self, score: RoutingScore, path: RoutePath,
                         model: Optional[str],
                         skill: Optional[Dict] = None,
                         memory: Optional[Dict] = None) -> str:
        reasons = []
        if skill:
            reasons.append(f"技能匹配({skill['skill_name']}, conf={skill['confidence']:.2f})")
        elif memory:
            reasons.append("记忆匹配(历史成功经验)")
        else:
            if score.level == ComplexityLevel.SIMPLE:
                reasons.append("简单请求")
            elif score.level == ComplexityLevel.MODERATE:
                reasons.append("中等复杂度")
            elif score.level == ComplexityLevel.COMPLEX:
                reasons.append("复杂请求")
            else:
                reasons.append("高复杂度请求")

        if score.breakdown.get("tool_calls", 0) > 0:
            reasons.append(f"含工具调用(+{score.breakdown['tool_calls']})")
        if score.breakdown.get("keywords", 0) > 0:
            reasons.append(f"含复杂关键词(+{score.breakdown['keywords']})")

        path_names = {
            RoutePath.GATEWAY: "Gateway→云端模型",
            RoutePath.AGENT_CHAIN: "Gateway→Official OpenClaw→Volcano",
            RoutePath.DIRECT_LOCAL: "Gateway→本地常驻模型",
            RoutePath.DIRECT_CLOUD: "Gateway→云端模型",
            RoutePath.LOCAL_INFERENCE: "Gateway→本地常驻模型(隐私)",
        }
        reasons.append(f"→{path_names.get(path, path.value)}")

        if model:
            reasons.append(f"model={model}")

        return ", ".join(reasons)

    def get_state(self) -> Dict:
        skills = self.memory.get_all_skills()
        recent_count = self.memory.get_total_count()
        return {
            "complexity_threshold": self.complexity_threshold,
            "learning": {
                "alpha": self.learning.alpha,
                "beta": self.learning.beta,
                "gamma": self.learning.gamma,
                "decay_rate": self.learning.decay_rate,
                "exploration_rate": self.learning.exploration_rate,
                "learning_iterations": self.learning.learning_iterations,
            },
            "model_performance": {
                name: {
                    "total_requests": p.total_requests,
                    "success_rate": round(p.ema_success_rate, 4),
                    "avg_latency_ms": round(p.ema_latency, 1),
                    "consecutive_failures": p.consecutive_failures,
                    "consecutive_successes": p.consecutive_successes,
                    "cost_total": round(p.cost_total, 4),
                    "tool_call_success_rate": round(
                        p.tool_call_success / p.tool_call_count, 4
                    ) if p.tool_call_count > 0 else None,
                }
                for name, p in self.model_performance.items()
            },
            "path_performance": {
                path.value: {
                    "total_requests": p.total_requests,
                    "success_rate": round(p.ema_success_rate, 4),
                    "avg_latency_ms": round(p.ema_latency, 1),
                }
                for path, p in self.path_performance.items()
            },
            "skills": {
                "total": len(skills),
                "items": [
                    {
                        "name": s["skill_name"],
                        "pattern": f"{s['pattern_type']}={s['pattern_detail']}",
                        "recommended_path": s["recommended_path"],
                        "recommended_model": s["recommended_model"],
                        "confidence": round(s["confidence"], 3),
                        "hit_count": s["hit_count"],
                        "success_count": s["success_count"],
                    }
                    for s in skills[:20]
                ],
            },
            "memory": {
                "total_records": recent_count,
                "db_path": str(self.memory.db_path),
            },
            "evolution_log": self._evolution_log[-10:],
            "session_id": self._session_id,
            "llm_enhancer": self.llm_enhancer.get_stats() if self.llm_enhancer else {"enabled": False},
            "hermes_agent": self.hermes_agent.get_stats() if self.hermes_agent else {"enabled": False},
        }

    def register_model(self, name: str) -> None:
        if name not in self.model_performance:
            self.model_performance[name] = ModelPerformance(name=name)

    def reset(self) -> None:
        self.model_performance.clear()
        for pp in self.path_performance.values():
            pp.total_requests = 0
            pp.success_requests = 0
            pp.total_latency_ms = 0
            pp.avg_latency_ms = 0.0
            pp.success_rate = 1.0
            pp.ema_latency = 0.0
            pp.ema_success_rate = 1.0
        self.learning = LearningState()
        self.complexity_threshold = 40.0
        self._evolution_log.clear()
        self._persist_state()
