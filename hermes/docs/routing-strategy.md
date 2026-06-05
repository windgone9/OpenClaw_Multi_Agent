
# Hermes Agent 智能路由策略 — 技术设计文档

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                      Routing Service (server.py)                │
│                        http://localhost:8082                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │           OfficialHermesAdapter (official_agent_adapter)  │  │
│  │                                                           │  │
│  │  ┌─────────────┐    ┌──────────────┐                      │  │
│  │  │ Mode A:     │    │ Mode B:      │                      │  │
│  │  │ Ollama      │    │ Hermes Agent │                      │  │
│  │  │ Direct      │    │ system_msg   │                      │  │
│  │  │ (~3.7s)     │    │ (~5.0s)      │                      │  │
│  │  └──────┬──────┘    └──────┬───────┘                      │  │
│  │         │                  │                              │  │
│  │         └────────┬─────────┘                              │  │
│  │                  ▼                                        │  │
│  │  ┌──────────────────────────────────────┐                 │  │
│  │  │  共享层 (Shared Layer)               │                 │  │
│  │  │  • _build_routing_system_message()   │                 │  │
│  │  │  • _post_validate_route()            │                 │  │
│  │  │  • _extract_route_from_text()        │                 │  │
│  │  │  • _check_memory_override()          │                 │  │
│  │  └──────────────────────────────────────┘                 │  │
│  │                  │                                        │  │
│  │                  ▼                                        │  │
│  │  ┌──────────────────────────────────────┐                 │  │
│  │  │  MEMORY.md (Single Source of Truth)  │                 │  │
│  │  │  • Routing Patterns Learned          │                 │  │
│  │  │  • Key Rules                         │                 │  │
│  │  │  • Feedback History                  │                 │  │
│  │  └──────────────────────────────────────┘                 │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 核心设计原则

1. **MEMORY.md 是路由规则的唯一真相源** — 所有路由规则存储在 `~/.hermes/memories/MEMORY.md`，代码中无硬编码规则
2. **双模式可切换** — Ollama Direct（低延迟）和 Hermes Agent（全框架）共享同一套规则和验证逻辑
3. **闭环进化** — 反馈 → 规则提取 → Memory 更新 → 下次推理生效
4. **多层兜底** — JSON 解析 → 自然语言提取 → Memory 关键词覆盖 → 类型约束覆盖

---

## 2. 路由策略详解

### 2.1 Mode A: Ollama Direct + Memory 闭环

**设计依据**: 本地 3B 模型在 Hermes Agent 框架下 tool calling 不稳定，且框架开销导致固定 2s 延迟。直接调用 Ollama API 绕过框架，保留 Memory 闭环能力。

**数据流**:

```
Request → _build_routing_system_message() → Ollama API → JSON/Text 解析 → _post_validate_route() → Response
                ↑                                                                      │
                └── 从 MEMORY.md 读取规则 ──────────────────────────────────────────────┘
                                              反馈写入 MEMORY.md (异步)
```

**关键参数**:

| 参数 | 值 | 说明 |
|------|-----|------|
| `num_ctx` | 2048 | 最小上下文窗口，降低推理开销 |
| `temperature` | 0.0 | 确定性输出，路由决策不需要随机性 |
| `num_predict` | 128 | 限制输出长度，路由 JSON 不需要长输出 |
| `format` | json | Ollama 原生 JSON 模式，提高输出格式稳定性 |

**延迟构成**:

| 阶段 | 耗时 | 说明 |
|------|------|------|
| Memory 读取 + 规则构建 | <1ms | 10s 缓存，命中时几乎为零 |
| Ollama LLM 推理 | 500-800ms | 3B 模型，~1500 prompt_tokens |
| JSON 解析 + 后验证 | <5ms | 纯计算 |
| **总计** | **3.0-3.7s** | 含 HTTP 往返和序列化开销 |

### 2.2 Mode B: Hermes Agent + system_message 注入

**设计依据**: 保留 Hermes Agent 框架的完整能力（Skill 匹配、Memory 工具、Tool Gateway），通过 `system_message` 参数注入路由规则，避免 tool calling 多轮交互。

**数据流**:

```
Request → _build_routing_system_message() → Hermes Agent API → Agent LLM 推理 → 解析 → _post_validate_route() → Response
                ↑                                                                                      │
                └── 从 MEMORY.md 读取规则 ────────────────────────────────────────────────────────────┘
                                              反馈写入 MEMORY.md (异步)
```

**system_message 注入机制**:

Hermes Agent API 的 `system_message` 参数被映射为 `epheral_system_prompt`，在 API 调用时追加到系统提示词末尾，不保存到 trajectory 历史。这意味着：

- 每次请求的路由规则都是最新的（从 MEMORY.md 实时读取）
- 不会污染 Agent 的长期对话历史
- Agent 仍然可以使用 Memory 工具进行反馈写入

**Session 管理策略**:

```
⚠ 关键发现：不复用 Session

Hermes Agent 会将每次对话追加到 Session 历史中：
  请求 1: prompt_tokens = 1500  →  推理 ~0.5s
  请求 2: prompt_tokens = 3000  →  推理 ~2s
  请求 3: prompt_tokens = 4500  →  推理 ~4s
  请求 N: prompt_tokens = 9000+ →  推理 ~12s

解决方案：每次路由请求使用全新 Session（不发送 X-Hermes-Session-Id），
代价是多 ~1s Agent 初始化开销，但避免 token 累积导致的延迟退化。
```

**延迟构成**:

| 阶段 | 耗时 | 说明 |
|------|------|------|
| Agent 初始化 | ~1s | AIAgent 实例创建 + 工具注册 |
| Memory 读取 + 规则构建 | <1ms | 10s 缓存 |
| LLM 推理 | ~2s | 3B 模型，~1500 prompt_tokens |
| JSON 解析 + 后验证 | <5ms | 纯计算 |
| **总计** | **3.0-5.0s** | 稳定，不随请求次数增长 |

### 2.3 两种模式对比

| 维度 | Ollama Direct | Hermes Agent |
|------|--------------|--------------|
| 平均延迟 | 3.0-3.7s | 3.0-5.0s |
| 延迟稳定性 | 高（无框架开销） | 高（已禁用 Session 复用） |
| Skill 匹配 | 不支持 | 支持（Agent 自动匹配） |
| Memory 工具 | 不支持（直接写文件） | 支持（Agent 可调用） |
| Tool Gateway | 不支持 | 支持 |
| 适用场景 | 纯路由决策 | 需要 Agent 完整能力 |
| 切换方式 | `POST /official-agent/mode {"mode":"ollama_direct"}` | `POST /official-agent/mode {"mode":"hermes_agent"}` |

---

## 3. 路由决策流程

### 3.1 完整决策链

```
输入请求
    │
    ▼
┌─────────────────────────────────┐
│ Step 1: LLM 推理                │
│ system_message(含 Memory 规则)  │
│ + user_message(请求内容)        │
│ → 输出 JSON 或自然语言          │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│ Step 2: 输出解析                │
│ 2a. JSON 直接解析               │
│ 2b. 正则提取 JSON               │
│ 2c. 自然语言关键词提取           │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│ Step 3: 后验证 (_post_validate) │
│ 优先级从高到低:                  │
│ P1: 隐私约束覆盖 (绝对)         │
│ P2: Memory 规则覆盖 (学习)      │
│ P3: 类型约束覆盖 (Key Rules)    │
└──────────────┬──────────────────┘
               │
               ▼
┌─────────────────────────────────┐
│ Step 4: 反馈记录 (异步)         │
│ → 本地 JSONL (即时)             │
│ → MEMORY.md (异步后台线程)      │
│ → Skill 进化 (失败时触发)       │
└─────────────────────────────────┘
```

### 3.2 后验证优先级

后验证是路由决策的安全网，确保 LLM 输出不符合预期时仍能正确路由：

| 优先级 | 规则 | 来源 | 示例 |
|--------|------|------|------|
| P1 (最高) | `require_local=true` | 请求约束 | 医疗数据 → `local_inference` |
| P2 | Memory 规则关键词匹配 | MEMORY.md | "翻译" → `direct_local` |
| P3 | `type=code/code_execution` | MEMORY.md Key Rules | 代码请求 → `agent_chain` |

**P2 Memory 规则覆盖的工作方式**:

```python
def _check_memory_override(prompt, current_route):
    rules = _parse_routing_rules_from_memory()
    for rule in rules:
        if rule.route == current_route:
            continue  # 同一路由，无需覆盖
        if any(kw in prompt.lower() for kw in rule.keywords):
            return rule.route  # 命中更高优先级的规则
    return ""
```

当 LLM 输出 `direct_local` 但 prompt 中包含 "代码" 关键词时，Memory 规则 "Code generation/programming → agent_chain" 会覆盖为 `agent_chain`。

### 3.3 自然语言兜底

3B 模型在复杂 system prompt 下可能输出自然语言而非 JSON。兜底策略：

```
优先级 1: 隐私约束 → local_inference
优先级 2: Memory 规则关键词匹配 → 对应路由
优先级 3: 文本中包含路由名称 → 提取
优先级 4: 默认 → gateway
```

---

## 4. Memory 驱动的路由规则

### 4.1 MEMORY.md 结构

```markdown
# Routing Decision Memory

## Routing Patterns Learned
- Category [keyword1, keyword2, ...] → route_path (optional notes)

## Key Rules
- constraint → route_path

## Feedback History
- route_path ✓/✗ latency_ms 'summary'
```

### 4.2 规则格式规范

```
- Category [kw1,kw2,...] → route (notes)
```

| 字段 | 说明 | 示例 |
|------|------|------|
| Category | 规则类别名 | `Code generation/programming` |
| [keywords] | 匹配关键词列表（小写） | `[代码,code,实现,编写]` |
| route | 目标路由路径 | `agent_chain` |
| (notes) | 可选备注 | `(learned from feedback)` |

**有效路由路径**: `direct_local` | `gateway` | `agent_chain` | `local_inference`

### 4.3 规则解析流程

```python
_parse_routing_rules_from_memory():
    1. 读取 MEMORY.md（10s 缓存）
    2. 提取 "Routing Patterns Learned" 和 "Key Rules" 两个 section
    3. 逐行解析 "- Category [keywords] → route" 格式
    4. 提取 keywords（[] 内的逗号分隔列表）
    5. 验证 route 是否在有效路径集合中
    6. 返回结构化规则列表: [{category, keywords, route, raw_line}]
```

### 4.4 规则注入到 System Message

```python
_build_routing_system_message():
    1. 从 MEMORY.md 解析规则
    2. 格式化为可读规则文本:
       "- Code generation/programming(代码, code, 实现) → agent_chain"
    3. 注入到 ROUTING_PROMPT_TEMPLATE 的 {routing_rules} 占位符
    4. 最终 system_message 约 1200 chars (~300 tokens)
```

**System Message 模板**:

```
你是路由决策助手。根据请求内容选择最优执行路径。

重要：不要调用任何工具！直接输出JSON结果。

路由选项(必须选其一):
1. direct_local - 简单闲聊、打招呼、无技术内容
2. gateway - 一般对话、信息查询、分析比较
3. agent_chain - 复杂多步规划、代码生成、编程实现、自主执行
4. local_inference - 隐私敏感、必须本地执行

{routing_rules}    ← 从 MEMORY.md 动态生成

只输出JSON，不要输出其他内容: {"route_path":"选项","complexity_score":0-100,"reason":"原因"}
```

---

## 5. Skill 进化闭环

### 5.1 进化触发条件

```
反馈记录 (success=False)
    → _evolve_rule_from_feedback(failed_route, request_summary)
    → 从 summary 提取期望路由 (desired_route)
    → 匹配已有规则类别或创建新类别
    → 更新/新增规则到 MEMORY.md
    → 下次推理自动生效
```

### 5.2 进化流程

```
失败反馈: "翻译请求被误路由到gateway应走direct_local"
                    │
                    ▼
┌─────────────────────────────────────┐
│ 1. 提取期望路由                     │
│    "应走direct_local" → direct_local│
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ 2. 匹配已有规则类别                 │
│    summary 包含 "翻译"              │
│    → 匹配 "Translation requests"   │
│    → keywords: [翻译, translate]    │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ 3. 更新规则                         │
│    旧: - Translation requests       │
│        [翻译,translate] → gateway   │
│    新: - Translation requests       │
│        [翻译,translate]             │
│        → direct_local (learned...)  │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│ 4. 失效缓存                         │
│    _memory_context_cached_at = 0    │
│    下次请求重新读取 MEMORY.md       │
└─────────────────────────────────────┘
```

### 5.3 类别匹配策略

| 策略 | 说明 | 优先级 |
|------|------|--------|
| 已有规则关键词匹配 | 遍历 Memory 规则的 keywords，检查 summary 是否包含 | 最高 |
| 中文主题关键词映射 | 翻译/数据库/闲聊/配置/部署/测试/监控/文档 | 次高 |
| 无法匹配 | 跳过进化，避免错误规则 | 兜底 |

### 5.4 规则更新安全机制

1. **精确行替换**: 使用逐行精确匹配替换，而非 `content.replace()`，避免误替换相似内容
2. **路由验证**: 新路由必须与失败路由不同，否则跳过
3. **期望路由必须存在**: `_extract_desired_route` 无法提取时跳过进化（而非默认 gateway）
4. **文件大小限制**: 新增规则后文件不超过 1500 chars，否则跳过新增

---

## 6. Memory 长度超限处理

### 6.1 问题背景

Hermes Agent 的 Memory 工具有 1375 字符的硬限制，且只支持 replace 语义（不支持 append）。随着规则增多和反馈积累，MEMORY.md 容易超限。

### 6.2 分层处理策略

```
┌─────────────────────────────────────────────────────────┐
│                  MEMORY.md 容量管理                      │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │ 第一层: 结构分离                                   │  │
│  │ Routing Patterns + Key Rules = 核心规则 (不裁剪)   │  │
│  │ Feedback History = 历史反馈 (可裁剪)               │  │
│  └───────────────────────────────────────────────────┘  │
│                         │                               │
│                         ▼                               │
│  ┌───────────────────────────────────────────────────┐  │
│  │ 第二层: 反馈滚动窗口                               │  │
│  │ 保留最近 10 条反馈，超出时删除最旧的               │  │
│  └───────────────────────────────────────────────────┘  │
│                         │                               │
│                         ▼                               │
│  ┌───────────────────────────────────────────────────┐  │
│  │ 第三层: 仅裁剪反馈行                               │  │
│  │ 超限时只删除 Feedback History 中的行               │  │
│  │ 绝不删除 Routing Patterns 或 Key Rules             │  │
│  └───────────────────────────────────────────────────┘  │
│                         │                               │
│                         ▼                               │
│  ┌───────────────────────────────────────────────────┐  │
│  │ 第四层: 新增规则保护                               │  │
│  │ 新增规则后文件 > 1500 chars → 跳过新增             │  │
│  │ 确保现有规则不被意外裁剪                           │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

### 6.3 具体实现

#### 反馈写入时的裁剪逻辑

```python
def _write_feedback_to_memory(...):
    # 1. 读取现有内容
    content = read(MEMORY_FILE)

    # 2. 追加新反馈行
    feedback_lines.append(new_line)

    # 3. 滚动窗口：只保留最近 10 条
    feedback_lines = feedback_lines[-10:]

    # 4. 重组内容
    new_content = before + feedback_header + "\n" + "\n".join(feedback_lines) + "\n"

    # 5. 超限处理：只裁剪反馈行，不动路由规则
    if len(new_content) > 1500:
        feedback_lines = feedback_lines[1:]  # 删除最旧的一条
        new_content = before + feedback_header + "\n" + "\n".join(feedback_lines) + "\n"

    # 6. 写入文件
    write(MEMORY_FILE, new_content)
```

#### 规则进化时的保护逻辑

```python
def _evolve_rule_from_feedback(...):
    # 新增规则后检查文件大小
    if len(new_content) > 1500:
        # 跳过新增，保护现有规则
        return

    # 更新已有规则时使用精确行替换
    # 避免误删其他规则
    for line in lines:
        if line.strip() == old_line.strip():
            line = new_rule  # 精确替换
```

### 6.4 容量估算

| 内容 | 平均大小 | 说明 |
|------|---------|------|
| 标题 + section headers | ~80 chars | 固定开销 |
| 每条路由规则 | ~80-120 chars | 含 category + keywords + route |
| 每条反馈记录 | ~40-60 chars | route + ✓/✗ + latency + summary |
| **12 条规则** | ~1100 chars | 当前规则数量 |
| **10 条反馈** | ~500 chars | 滚动窗口上限 |
| **总计** | ~1680 chars | 超出 1500 限制 |

**实际运行时**: 反馈行会被持续裁剪以保持在 1500 chars 以内，路由规则作为核心数据不被裁剪。

### 6.5 未来优化方向

| 方案 | 说明 | 优先级 |
|------|------|--------|
| 规则关键词精简 | 每条规则只保留 5-8 个高区分度关键词 | 高 |
| 反馈独立存储 | 反馈存入 JSONL 文件，MEMORY.md 只保留规则 | 高 |
| 规则优先级排序 | 高频命中的规则排前，低频规则可淘汰 | 中 |
| 规则合并 | 相同路由的规则合并关键词 | 中 |
| 外部规则存储 | 规则存入 SQLite，MEMORY.md 只存摘要 | 低 |

---

## 7. 缓存策略

### 7.1 三级缓存

| 缓存对象 | TTL | 存储位置 | 失效条件 |
|---------|-----|---------|---------|
| 健康检查 | 30s | `_health_cache` | TTL 过期 |
| Memory 上下文 | 10s | `_memory_context_cache` | TTL 过期 / 反馈写入 / 规则进化 |
| Ollama 模型 | keep_alive=30m | Ollama 运行时 | 30 分钟无请求后卸载 |

### 7.2 缓存失效时机

```
反馈写入 MEMORY.md
    → _memory_context_cached_at = 0    ← 立即失效
    → 下次请求重新读取 MEMORY.md       ← 保证规则最新

规则进化更新 MEMORY.md
    → _memory_context_cached_at = 0    ← 立即失效
    → 下次请求使用新规则               ← 进化立即生效
```

---

## 8. 反馈系统

### 8.1 双通道写入

| 通道 | 延迟 | 可靠性 | 用途 |
|------|------|--------|------|
| 本地 JSONL | <1ms (即时) | 高 | 完整反馈记录，调试分析 |
| MEMORY.md | ~2s (异步) | 中 | 规则进化，闭环学习 |

### 8.2 异步写入流程

```
record_feedback_via_memory()
    │
    ├── 1. 即时写入本地 JSONL
    │      ~/.hermes/routing_feedback/feedback.jsonl
    │
    └── 2. 加入反馈队列 (_feedback_queue)
           │
           ▼
       后台线程 (每 2s 批量处理)
           │
           ├── _write_feedback_to_memory()
           │      写入 MEMORY.md Feedback History
           │
           └── if not success:
                  _evolve_rule_from_feedback()
                  提取新规则 → 更新 MEMORY.md Routing Patterns
```

### 8.3 反馈格式

**本地 JSONL** (完整记录):
```json
{"timestamp":"2026-06-04T10:30:00","skill":"routing-decision","route_path":"agent_chain","success":true,"latency_ms":1200,"request_summary":"请用Python实现快速排序"}
```

**MEMORY.md** (精简记录):
```
- agent_chain ✓ 1200ms '请用Python实现快速排序'
```

---

## 9. 路由路径枚举

| 路径 | 用途 | 典型场景 |
|------|------|---------|
| `direct_local` | 简单本地处理 | 闲聊、打招呼、翻译 |
| `gateway` | 通用对话/信息查询 | 什么是微服务、比较分析 |
| `agent_chain` | 复杂多步执行 | 代码生成、部署、测试 |
| `local_inference` | 隐私敏感/必须本地 | 医疗数据、个人信息 |

**约束强制路由**:
- `require_local=true` → 强制 `local_inference`（P1 优先级，不可覆盖）
- `type=code/code_execution` → 强制 `agent_chain`（P3 优先级）

---

## 10. 配置参考

### 10.1 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `OFFICIAL_AGENT_URL` | `http://127.0.0.1:8642` | Hermes Agent API 地址 |
| `OFFICIAL_AGENT_KEY` | - | API 认证密钥 |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API 地址 |
| `OLLAMA_MODEL` | `qwen2.5:3b-routing` | 路由推理模型 |
| `USE_DIRECT_OLLAMA` | `false` | 默认路由模式 |
| `BRIDGE_TIMEOUT` | `3.0` | Bridge 服务超时（秒） |
| `SCHEDULER_TIMEOUT` | `3.0` | Scheduler 服务超时（秒） |

### 10.2 运行时模式切换

```bash
# 切换到 Ollama Direct 模式
curl -X POST http://localhost:8082/official-agent/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"ollama_direct"}'

# 切换到 Hermes Agent 模式
curl -X POST http://localhost:8082/official-agent/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"hermes_agent"}'
```

### 10.3 Ollama 模型常驻配置

```bash
# 设置 keep_alive 防止模型卸载（冷启动 ~2s → 热启动 ~0.5s）
curl http://localhost:11434/api/generate \
  -d '{"model":"qwen2.5:3b-routing","keep_alive":"30m"}'
```

---

## 11. 性能基准

### 11.1 实测数据（qwen2.5:3b-routing, MacBook Pro M-series）

| 场景 | Ollama Direct | Hermes Agent |
|------|--------------|--------------|
| 闲聊 (你好) | 3.0-3.7s | 3.1-5.4s |
| 代码生成 (Python排序) | 3.5-4.0s | 3.9-4.2s |
| 隐私敏感 (医疗脱敏) | 3.5-6.0s | 3.5-6.0s |
| 通用问答 (什么是微服务) | 3.7-4.0s | 4.0-4.2s |
| 翻译 (翻译成英文) | 3.5-6.0s | 3.5-6.0s |
| **8 场景全通过率** | **8/8** | **8/8** |

### 11.2 延迟退化问题（已修复）

| 问题 | 原因 | 修复 |
|------|------|------|
| Session 复用导致延迟从 5s → 15s | prompt_tokens 累积 (1500→9000+) | 禁用 Session 复用，每次请求新建 Session |
| 首次请求 5s+ | Ollama 模型冷启动 | keep_alive=30m 防止卸载 |
| Hermes Agent 35s 延迟 | 93 个工具 + 25 个 Skills 注入 system prompt | 禁用 skills 工具集 + system_message 注入 |
