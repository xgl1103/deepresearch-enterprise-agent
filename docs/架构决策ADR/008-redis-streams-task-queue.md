# ADR-008: 选择 Redis Streams 作为异步任务队列

## 状态
Accepted（已采纳）

## 背景
全链路异步化和流式输出（ADR-007）是在 LangGraph 同步请求-响应模式下实现的 —— 前端 POST → 后端执行完整流水线 → 返回最终报告。存在问题：
- HTTP 连接易超时（执行时间 2-5 分钟，浏览器/代理默认 60s）
- Plan 确认需要暂停等待用户输入，"暂停→继续"无法用单次 HTTP 请求表达
- 流式 token 事件和图事件需要独立于 HTTP 响应生命周期进行推送

因此需要一套异步任务系统：任务异步执行，前端通过 SSE 接收事件，支持 Plan 暂停/继续。

## 决策
使用 Redis Streams 实现任务队列 + 事件流。生产环境使用独立 Worker 进程消费；本地开发允许显式启用 FastAPI 内嵌协程，复用 ADR-007 已打通的流式链路。

## 理由

### 架构：
```
POST /api/research  →  XADD research:tasks  →  返回 task_id

[独立 asyncio worker；本地模式可由 FastAPI lifespan 启动]
  XREADGROUP research:tasks  →  graph.astream()
    图事件  →  XADD research:events:{task_id}
    token   →  XADD research:events:{task_id}  (复用 ADR-007 的 _emit_token)

GET /api/research/{task_id}/stream  →
  XREAD research:events:{task_id}  →  SSE 推送
```

### Plan 暂停/继续：
1. Worker 执行到 `awaiting_plan_confirmation` → 图暂停 → `task_paused` 事件
2. 前端展示 Plan → 用户确认/反馈 → 再次 POST（带 `plan` + `plan_status`）
3. Worker 从暂停点继续 → Research 或 replan

### 选择 Redis Streams 的原因：
1. **统一技术栈**：Redis 已用于搜索缓存（ADR-004），零额外组件
2. **Consumer Group**：XREADGROUP + XACK at-least-once 消费，支持多消费者
3. **SSE 天然适配**：XREAD 阻塞读取 + `>` 消费最新消息
4. **自动过期**：事件流 `EXPIRE 86400` + MAXLEN 控制内存

### 放弃的方案：
- **Celery**：需独立 Broker + Backend，不支持原生事件流，部署复杂
- **FastAPI BackgroundTasks**：不持久化，进程重启丢失
- **WebSocket**：状态管理复杂，断线重连需自行实现

### 风险与缓解：
- **风险**：Redis 单点故障
- **缓解**：企业内并发极低（<10 请求/min），风险可接受；后续可升级 Sentinel

## 后果

### 正面：
- 零额外组件（仅 Redis）
- 代码约 230 行，比 Celery 方案减少约 60%
- SSE 支持 `Last-Event-ID` 断线重连
- 流式链路在 ADR-007 已打通，此 ADR 仅改变事件分发通道（从 HTTP response → Redis Stream → SSE）

### 负面：
- Redis 成为核心单点依赖（宕机则系统不可用）
- 不支持任务优先级和延迟执行

## 2026-07 工程化补强

- Worker consumer name 改为每进程唯一，支持多实例并发消费。
- 使用 `XAUTOCLAIM` 接管崩溃实例遗留的 pending 消息。
- 失败任务自动重试，超过阈值写入 `research:tasks:dead-letter`。
- 任务生命周期写入 `research:task-status:{task_id}`，并提供所有权受控的状态接口。
- `EMBEDDED_TASK_WORKER=false` 时 API 与 Worker 完全解耦。

## 相关文档
- [任务队列实现](../../backend/src/agent/task_queue.py)
- [SSE 流式 Hook](../../../frontend/src/lib/useResearchStream.ts)
- [FastAPI 路由](../src/agent/app.py:108)
- [ADR-007: 全链路异步化 + LLM 流式输出](007-full-async-and-streaming.md)
- [ADR-004: Milvus + Redis 缓存](004-milvus-and-redis-cache.md)

## 变更记录
- 2025-12-20: 初始决策 — Redis Streams 任务队列 + SSE 上线 by @yunfang
- 2026-01-05: Plan 暂停/继续机制与任务队列集成 by @yunfang
