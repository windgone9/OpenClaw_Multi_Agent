# Handoff: OpenClaw Multi-Agent 性能优化
**生成时间**: 2026-06-25
**原会话状态**: 上下文已满 (API Error 400 - input length exceeded 202745)

---

## 1. 当前进度总览

| Phase | 状态 | 说明 |
|-------|------|------|
| Phase 1: 持久化 httpx 客户端 | ✅ 完成 | 所有 services 使用共享客户端 |
| Phase 2: 消除同步阻塞 | ✅ 完成 | async handlers 移除 asyncio.to_thread |
| Phase 3: InProcessQueue 持久化优化 | 🔄 进行中 (98%) | 已实现批量延迟持久化，待验证 |
| Phase 4: K8S/Docker 配置优化 | ✅ 完成 | 资源 limits 和 replica 调整 |
| Phase 5: 代码质量优化 | 🔄 进行中 (70%) | 关键词去重已完成，Dashboard 并行化待做 |

---

## 2. Phase 3: InProcessQueue 持久化优化（已完成的工作）

### 已修改文件
- `hermes/message_queue.py`

### 已实现的功能
1. **新增属性**:
   - `_dirty_count: Dict[str, int]` - 自上次持久化以来的操作计数
   - `_PERSIST_THRESHOLD = 10` - 每 N 次操作触发持久化
   - `_PERSIST_INTERVAL = 5.0` - 后台线程定时持久化间隔
   - `_global_lock` - 后台线程用全局锁
   - `_persist_thread` / `_persist_stop_event` - 后台线程控制

2. **新增方法**:
   - `_start_persist_thread()` - 启动后台持久化线程
   - `_persist_loop()` - 后台线程主循环，每 5 秒检查并持久化脏队列

3. **修改 push/pop 逻辑**:
   - `push()`: 不再立即持久化，改为 `_dirty_count += 1`，超阈值时立即持久化
   - `pop()`: 同样改为标记脏，由后台线程处理
   - `__init__`: 末尾调用 `_start_persist_thread()`

### 未完成/待验证
- ⚠️ 语法检查已通过 (`ast.parse` OK)
- ⚠️ 未运行集成测试验证持久化是否正常工作
- ⚠️ 需验证 `_persist_loop()` 中的锁是否正确（`_queue_names` 与 `_dirty_count` 的并发安全）

### Phase 3 的启动指令
请验证 hermes/message_queue.py 的持久化逻辑：

运行测试: pytest tests/test_queue_persistence.py
检查后台线程是否正确启动和退出
验证数据在服务重启后能恢复
如有问题，修复锁竞争或线程安全问题
text

---

## 3. Phase 5: 代码质量优化（已完成的工作）

### 3.1 关键词去重 ✅ 已完成
- **修改文件**: `hermes/router.py`
- **变更内容**:
  - 新增模块级常量 `MULTIMODAL_KEYWORDS` 和 `MULTI_STEP_KEYWORDS`
  - 删除 3 处重复的关键词列表定义，统一引用常量
  - 去重后代码更简洁，维护性提升

### 3.2 Dashboard 健康检查并行化 ❌ 未完成
**需要实现**: 将 Dashboard 的串行健康检查改为 `asyncio.gather` 并行执行

**涉及文件**: 
- `dashboard/health.py` 或类似路径（需确认实际文件位置）
- 可能涉及 `hermes/dashboard.py` 或 `services/dashboard/health_checker.py`

**当前问题**: 
- 多个服务健康检查是串行执行的，每个服务等待超时（如 5s）会累积延迟
- 需要改为并行检查，总耗时 = max(各服务耗时) 而不是 sum(各服务耗时)

**期望实现**:
```python
# Before: 串行
for service in services:
    result = await check_service(service)
    
# After: 并行
results = await asyncio.gather(*[check_service(s) for s in services])

Phase 5 的启动指令

text
1. 关键词去重已完成，无需额外操作
2. 请找到 Dashboard 健康检查的代码位置（可能是 hermes/dashboard.py 或 services/health.py）
3. 将串行检查改为 asyncio.gather 并行
4. 注意错误处理：单个服务失败不应阻塞其他服务
5. 运行测试验证: pytest tests/test_dashboard_health.py

4. 项目关键信息

目录结构

text
~/MyWork/OpenClaw_Multi_Agent/
├── hermes/
│   ├── message_queue.py    # Phase 3 已修改
│   ├── router.py           # Phase 5 已修改（关键词去重）
│   ├── dashboard.py        # 可能包含健康检查（需确认）
│   └── services/           # Phase 1 已优化
├── deployment/
│   └── k8s-config.yaml     # Phase 4 已修改
└── tests/
    ├── test_queue_persistence.py
    └── test_dashboard_health.py
Git 状态

text
On branch feature/multi-agent-optimization
Changes not staged:
  modified: hermes/message_queue.py
  modified: hermes/router.py
已完成 Phases 的参考模式

Phase 1/2 模式: 共享资源（httpx clients）+ 异步化，参考 hermes/services/base/client.py
Phase 4 模式: K8S 资源配置调整，参考 deployment/k8s-config.yaml
5. 未解决的问题/已知坑

Phase 3 线程安全: _persist_loop() 中读取 _queue_names 和 _dirty_count 需要确保锁覆盖完整
Phase 3 退出清理: 当前没有实现 stop() 或 shutdown() 来优雅停止后台线程
Dashboard 位置不明: 需要先确认 Dashboard 健康检查代码的确切位置
测试覆盖: Phase 3 和 Phase 5 的测试用例可能不完整或需要更新
6. 验证命令

bash
# 验证 Phase 3
pytest tests/test_queue_persistence.py -v

# 验证 Phase 5（Dashboard 并行化后）
pytest tests/test_dashboard_health.py -v

# 完整 E2E 测试
pytest tests/test_e2e.py -v

# 检查语法
python3 -c "import ast; ast.parse(open('hermes/message_queue.py').read())"
python3 -c "import ast; ast.parse(open('hermes/router.py').read())"
7. 给新会话的启动指令

text
请继续 OpenClaw 项目的 Phase 3 和 Phase 5 工作：

Phase 3 (优先):
- 验证 hermes/message_queue.py 的后台持久化功能
- 运行 pytest tests/test_queue_persistence.py
- 修复发现的问题（特别是线程安全和优雅退出）

Phase 5 (其次):
- 找到 Dashboard 健康检查的代码位置
- 将串行检查改为 asyncio.gather 并行
- 添加适当的超时和错误处理

完成后运行完整测试套件确保没有回归。
