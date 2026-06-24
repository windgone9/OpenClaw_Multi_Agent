# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenClaw Multi-Agent is a multi-agent intelligent scheduling system for LLM requests. It routes requests through five paths (direct_local, gateway, k8s_gateway, multimodal, local_inference) using a Hermes Agent core scheduling layer with self-learning feedback loops and Smart Router complexity scoring.

## Commands

### Start / Stop
```bash
./start-all.sh          # One-click start (Ollama + OfficialGW + Hermes)
./start-all.sh stop     # Stop all services
./start-all.sh status   # Check service status
./stop-all.sh           # Kill processes on all ports
```

### Run Individual Services
```bash
# Hermes Agent (core, port 8082)
source hermes-official-venv/bin/activate
python -m hermes.server

# LiteLLM Proxy (port 4000)
python -m hermes.litellm_proxy

# Stream Service (port 8084)
python -m hermes.stream_service

# Bridge (port 3001)
node bridge/orchestrator.mjs

# Gateway (port 3000)
node gateway/gateway.mjs

# Scheduler (port 8000)
python run.py
```

### Docker
```bash
cd docker
docker compose up -d              # Start all containers
docker compose up -d --scale litellm=3  # Scale LiteLLM instances
docker compose down
```

### Testing
No formal test framework — tests are ad-hoc Python scripts:
```bash
# Run individual test files directly
python hermes/test_agent.py
python hermes/test_routing_strategy.py
python hermes/test_hybrid_routing.py
python hermes/test_llm_enhancer.py
python hermes/test_official_agent.py

# Integration tests in tests/
python tests/test_regression.py
python tests/test_unified_api.py

# Batch testing
python batch_test.py
```

### Health Checks
```bash
curl http://localhost:8082/health    # Hermes
curl http://localhost:3000/health    # Gateway
curl http://localhost:3001/health    # Bridge
curl http://localhost:11434/api/tags # Ollama
curl http://localhost:4000/health    # LiteLLM (requires Authorization header)
curl http://localhost:8084/health    # Stream Service
```

### Install Dependencies
```bash
npm install
python3 -m venv hermes-official-venv
source hermes-official-venv/bin/activate
pip install -r requirements.txt
```

### Environment Setup
```bash
cp .env.example .env  # Edit .env for API keys and ports
```
Python services auto-load `.env` via `python-dotenv` (called at module top: `load_dotenv()`).

## Architecture

### Service Layers (request flow)

```
Request → Hermes Agent (:8082) → Route Decision → Dispatch
                                           ├─ direct_local    → Ollama (:11434)
                                           ├─ gateway         → OfficialGW (:3005)
                                           ├─ k8s_gateway     → OpenClaw K8S plugin
                                           ├─ multimodal      → Ollama vision models
                                           └─ local_inference → Ollama (privacy-constrained)
```

**Hermes is the core** — Bridge (:3001) and Gateway (:3000) are alternative/legacy routing layers. The current primary architecture is Hermes-centric with direct dispatch (no Bridge/Gateway middle layer).

### Key Modules in `hermes/`

| File | Role |
|------|------|
| `server.py` | FastAPI entry — queue APIs, K8S APIs, system APIs, unified `/v1/chat` entry that auto-routes by `type` field |
| `router.py` | `HermesRouter` — 6-dimension complexity scoring, memory-based routing, skill matching, 5 route paths |
| `dispatch_worker.py` | Thread-based workers consuming from message queue, executing route decisions |
| `official_agent_adapter.py` | `OfficialHermesAdapter` — routing via Ollama direct or Hermes Agent API, MEMORY.md closed-loop feedback |
| `agent.py` | `HermesAgent` with RoutingSkill system |
| `llm_enhancer.py` | LLM-based complexity enhancement for routing decisions |
| `message_queue.py` | `MessageQueue` abstract class — `InProcessQueue` (JSONL persistence) and `RedisQueue` backends |
| `stream_service.py` | Standalone Stream Service (:8084) — WebSocket/SSE streaming, FunASR ASR integration |
| `litellm_proxy.py` | Standalone LiteLLM Proxy (:4000) — OpenAI-compatible API, handles `/v1/chat/completions`, vision, ASR, embeddings |

### Unified Entry (`/v1/chat`)

Hermes `server.py` provides a single `/v1/chat` endpoint that auto-routes based on request `type`:
- `chat`/`vision` → LiteLLM Proxy
- `asr` → Stream Service
- `multimodal` → LiteLLM with vision model
- `stream_asr` → WebSocket to Stream Service

### Message Queue System

Three queues: `openclaw:requests`, `openclaw:results`, `openclaw:feedback`. Two backends:
- **InProcessQueue** — JSONL file persistence under `.hermes/queues/`, for single-node dev
- **RedisQueue** — for production/distributed
- Auto-detect via `QUEUE_BACKEND=auto` (tries Redis, falls back to InProcess)

### Self-Learning Loop

1. Dispatch results generate feedback (success/failure, latency)
2. Feedback writes to local JSONL + `.hermes/memories/MEMORY.md` (async background thread)
3. MEMORY.md routing patterns injected into LLM routing prompts
4. Circuit breaker: Ollama routing failures → skip LLM routing, use keyword-based fallback

### Concurrency Control

- Semaphores: Ollama GPU (max 2 concurrent), routing decisions (max 1), OfficialGW (max 3)
- Persistent httpx clients with connection pooling for Ollama

### Scheduler (Legacy Alternative)

`scheduler/` is a separate FastAPI app with hook system (pre/post), strategy routers (adaptive, load_balance, priority), and model registry. Entry via `run.py`. Not the primary path — Hermes is the main scheduler.

### Configuration

- `openclaw.json` — Master config (agents, model catalog, smart router rules/thresholds)
- `.env` — Environment variables (API keys, ports, thresholds). Python services auto-load via `python-dotenv`
- `docker/litellm_config.yaml` — LiteLLM model config
- `docker/nginx.conf` — Reverse proxy config
- `hermes/watchdog_config.json` — Service watchdog for auto-restart

### Smart Router Scoring

Six dimensions with configurable weights in `openclaw.json`:
- Request type (5-40), Tool calls (30+10n), Prompt length (0-18), Keywords (0-20), Context (0-15), Priority (-5~+10)
- Total score < threshold(40) → simple path; >= threshold → agent chain path

### K8S Deployment

`k8s/` contains 14 numbered YAML manifests (00-11) for KinD or real clusters, plus `kind-config.yaml` and `deploy.sh`.

## Language

The project is primarily documented in Chinese (README, code comments, API descriptions). Code identifiers and variable names are in English. Preserve Chinese comments when editing.

## Key Patterns

- All Python entry points start with `from dotenv import load_dotenv; load_dotenv()` at the top
- FastAPI apps use `CORSMiddleware` universally
- `httpx.AsyncClient` with connection pooling is preferred over `requests`
- Queue-based async dispatch: submit → queue → worker → result queue → feedback
- JSONL file persistence under `.hermes/` for single-node mode
