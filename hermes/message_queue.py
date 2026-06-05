"""
Message Queue Layer for Hermes Dispatch System.

Supports two backends:
1. Redis (production): requires Redis server, supports distributed workers
2. InProcess (development): zero-dependency, uses threading.Queue + file persistence

Queue naming convention:
  - openclaw:requests  — incoming model requests from frontend
  - openclaw:results   — completed results for frontend to consume
  - openclaw:feedback  — feedback events for Memory evolution

Message format (JSON):
  Request: {
    "request_id": "uuid",
    "appid": "default",
    "type": "chat|code|tool_call",
    "prompt": "...",
    "priority": 1-5,
    "model_hint": "optional",
    "parameters": {},
    "context": [],
    "tools": [],
    "constraints": {},
    "timestamp": "ISO8601"
  }

  Result: {
    "request_id": "uuid",
    "appid": "default",
    "status": "success|failed|timeout",
    "routing": {
      "route_path": "gateway|agent_chain|direct_local|local_inference",
      "complexity_score": 0-100,
      "selected_model": "...",
      "reason": "..."
    },
    "result": {
      "model_name": "...",
      "output": "...",
      "latency_ms": 0,
      "usage": {}
    },
    "error": null,
    "total_latency_ms": 0,
    "timestamp": "ISO8601"
  }

  Feedback: {
    "request_id": "uuid",
    "route_path": "...",
    "success": true,
    "latency_ms": 0,
    "request_summary": "...",
    "timestamp": "ISO8601"
  }
"""

import json
import logging
import os
import threading
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Queue names
QUEUE_REQUESTS = "openclaw:requests"
QUEUE_RESULTS = "openclaw:results"
QUEUE_FEEDBACK = "openclaw:feedback"


class MessageQueue(ABC):
    """Abstract message queue interface."""

    @abstractmethod
    def push(self, queue_name: str, message: Dict) -> str:
        """Push a message to the queue. Returns message ID."""
        ...

    @abstractmethod
    def pop(self, queue_name: str, timeout: float = 0) -> Optional[Dict]:
        """Pop a message from the queue. Blocks up to timeout seconds.
        Returns None if queue is empty."""
        ...

    @abstractmethod
    def size(self, queue_name: str) -> int:
        """Return the number of messages in the queue."""
        ...

    @abstractmethod
    def peek(self, queue_name: str, limit: int = 10) -> List[Dict]:
        """Peek at messages without removing them."""
        ...

    @abstractmethod
    def flush(self, queue_name: str) -> int:
        """Remove all messages from the queue. Returns count removed."""
        ...

    @abstractmethod
    def health(self) -> Dict:
        """Return health status of the queue backend."""
        ...


class InProcessQueue(MessageQueue):
    """In-process message queue using threading.Queue + file persistence.

    Suitable for single-node development. Messages are persisted to JSONL
    files for durability across restarts.
    """

    def __init__(self, data_dir: str = ""):
        self._data_dir = data_dir or os.path.expanduser("~/.hermes/queues")
        os.makedirs(self._data_dir, exist_ok=True)

        self._queues: Dict[str, List[Dict]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._conditions: Dict[str, threading.Condition] = {}

        # Load persisted messages
        for name in [QUEUE_REQUESTS, QUEUE_RESULTS, QUEUE_FEEDBACK]:
            self._queues[name] = []
            self._locks[name] = threading.Lock()
            self._conditions[name] = threading.Condition(self._locks[name])
            self._load_from_file(name)

    def _file_path(self, queue_name: str) -> str:
        safe_name = queue_name.replace(":", "_")
        return os.path.join(self._data_dir, f"{safe_name}.jsonl")

    def _load_from_file(self, queue_name: str):
        path = self._file_path(queue_name)
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self._queues[queue_name].append(json.loads(line))
            logger.info(f"Loaded {len(self._queues[queue_name])} messages from {queue_name}")
        except Exception as e:
            logger.warning(f"Failed to load queue {queue_name}: {e}")

    def _persist_to_file(self, queue_name: str):
        path = self._file_path(queue_name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                for msg in self._queues[queue_name]:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Failed to persist queue {queue_name}: {e}")

    def push(self, queue_name: str, message: Dict) -> str:
        msg_id = message.get("request_id") or str(uuid.uuid4())
        message["_msg_id"] = msg_id
        message["_enqueued_at"] = datetime.now().isoformat()

        with self._conditions[queue_name]:
            self._queues[queue_name].append(message)
            self._persist_to_file(queue_name)
            self._conditions[queue_name].notify()

        return msg_id

    def pop(self, queue_name: str, timeout: float = 0) -> Optional[Dict]:
        with self._conditions[queue_name]:
            if self._queues[queue_name]:
                msg = self._queues[queue_name].pop(0)
                self._persist_to_file(queue_name)
                return msg

            if timeout <= 0:
                return None

            deadline = time.time() + timeout
            while not self._queues[queue_name]:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._conditions[queue_name].wait(timeout=remaining)

            if self._queues[queue_name]:
                msg = self._queues[queue_name].pop(0)
                self._persist_to_file(queue_name)
                return msg
            return None

    def size(self, queue_name: str) -> int:
        with self._locks[queue_name]:
            return len(self._queues[queue_name])

    def peek(self, queue_name: str, limit: int = 10) -> List[Dict]:
        with self._locks[queue_name]:
            return self._queues[queue_name][:limit]

    def flush(self, queue_name: str) -> int:
        with self._locks[queue_name]:
            count = len(self._queues[queue_name])
            self._queues[queue_name].clear()
            self._persist_to_file(queue_name)
            return count

    def health(self) -> Dict:
        return {
            "backend": "inprocess",
            "data_dir": self._data_dir,
            "queues": {
                name: len(msgs) for name, msgs in self._queues.items()
            },
        }


class RedisQueue(MessageQueue):
    """Redis-backed message queue for production use.

    Uses Redis LIST operations (LPUSH/RPOP) for FIFO queue semantics.
    Requires a running Redis server.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        import redis
        self._redis = redis.from_url(redis_url)
        self._redis_url = redis_url

    def push(self, queue_name: str, message: Dict) -> str:
        msg_id = message.get("request_id") or str(uuid.uuid4())
        message["_msg_id"] = msg_id
        message["_enqueued_at"] = datetime.now().isoformat()
        self._redis.rpush(queue_name, json.dumps(message, ensure_ascii=False))
        return msg_id

    def pop(self, queue_name: str, timeout: float = 0) -> Optional[Dict]:
        if timeout > 0:
            result = self._redis.blpop(queue_name, timeout=int(timeout))
            if result:
                _, data = result
                return json.loads(data)
            return None
        else:
            data = self._redis.lpop(queue_name)
            if data:
                return json.loads(data)
            return None

    def size(self, queue_name: str) -> int:
        return self._redis.llen(queue_name)

    def peek(self, queue_name: str, limit: int = 10) -> List[Dict]:
        messages = self._redis.lrange(queue_name, 0, limit - 1)
        return [json.loads(m) for m in messages]

    def flush(self, queue_name: str) -> int:
        count = self._redis.llen(queue_name)
        self._redis.delete(queue_name)
        return count

    def health(self) -> Dict:
        try:
            info = self._redis.info()
            return {
                "backend": "redis",
                "redis_url": self._redis_url,
                "redis_version": info.get("redis_version", "unknown"),
                "used_memory_human": info.get("used_memory_human", "?"),
                "connected_clients": info.get("connected_clients", 0),
                "queues": {
                    QUEUE_REQUESTS: self.size(QUEUE_REQUESTS),
                    QUEUE_RESULTS: self.size(QUEUE_RESULTS),
                    QUEUE_FEEDBACK: self.size(QUEUE_FEEDBACK),
                },
            }
        except Exception as e:
            return {"backend": "redis", "status": "error", "error": str(e)}


def create_queue(backend: str = "auto", redis_url: str = "redis://localhost:6379/0",
                 data_dir: str = "") -> MessageQueue:
    """Create a message queue instance.

    Args:
        backend: "redis", "inprocess", or "auto" (try Redis, fallback to inprocess)
        redis_url: Redis connection URL
        data_dir: Directory for inprocess queue persistence
    """
    if backend == "inprocess":
        return InProcessQueue(data_dir=data_dir)

    if backend == "redis":
        return RedisQueue(redis_url=redis_url)

    # auto: try Redis first, fallback to inprocess
    try:
        q = RedisQueue(redis_url=redis_url)
        # Test connection by calling a Redis command directly
        q._redis.ping()
        logger.info("Message queue: using Redis backend")
        return q
    except Exception:
        logger.info("Redis unavailable, falling back to inprocess queue")
        return InProcessQueue(data_dir=data_dir)
