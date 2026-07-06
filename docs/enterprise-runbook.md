# DeepResearch 当前部署形态企业化运维 Runbook

本文档用于当前阶段的工程化部署，不假设已经迁移到企业服务器或 Kubernetes。部署形态仍保持：本机/单机 Docker 依赖服务 + FastAPI API 进程 + 独立 Worker 进程 + 前端静态构建。

## 1. 当前推荐拓扑

```text
Browser
  -> Frontend dev server / static dist
  -> FastAPI API process
       -> PostgreSQL: users, thread ownership, durable results, audit events
       -> Redis: session, queue stream, task status, SSE events, quotas, checkpoint
       -> Milvus/MinIO/Etcd: knowledge base vector storage stack
       -> LLM provider / MCP search provider
  -> Worker process
       -> Redis task stream
       -> LangGraph
       -> LLM provider / MCP search provider
       -> PostgreSQL result store
```

生产式本地运行时，API 和 Worker 应分开启动：

- API：`run_uvicorn_backend.bat`
- Worker：`run_worker.bat` 或 `run_worker.sh`
- 仅本地开发一键模式：`run_backend.bat`，该模式会显式启用内嵌 Worker

## 2. 环境变量与密钥

真实密钥不得提交到 Git。推荐配置位置：

- Docker 依赖服务：复制 `.env.docker.example` 为 `.env`，填入真实 `POSTGRES_PASSWORD`、`REDIS_PASSWORD`、`MINIO_ROOT_USER`、`MINIO_ROOT_PASSWORD`。
- 后端应用：复制 `backend/.env.example` 为 `backend/.env`，填入真实 `APP_TOKEN`、`MCP_APP_ID`、`METRICS_TOKEN`、`REDIS_URL`、`DATABASE_URL` 等。
- 文件型密钥：支持 `APP_TOKEN_FILE`、`MCP_APP_ID_FILE`、`METRICS_TOKEN_FILE`，适合未来接入 Docker secrets 或企业密钥系统。

生产环境必须满足：

- `ENVIRONMENT=production`
- `SESSION_COOKIE_SECURE=true`
- `EMBEDDED_TASK_WORKER=false` 或不设置
- `REDIS_URL` 必须带密码
- `DATABASE_URL` 不得使用默认 PostgreSQL 密码

## 3. 启动顺序

1. 配置 Docker 依赖服务密钥：

   ```powershell
   Copy-Item .env.docker.example .env
   # 编辑 .env，填入真实密码
   docker compose up -d
   ```

2. 初始化数据库：

   ```powershell
   cd backend
   python -m agent.db.init_db
   ```

3. 启动 API-only 后端：

   ```powershell
   .\run_uvicorn_backend.bat
   ```

4. 启动独立 Worker：

   ```powershell
   .\run_worker.bat
   ```

5. 构建或启动前端：

   ```powershell
   cd frontend
   npm run build
   # 本地调试可用 npm run dev
   ```

## 4. 健康检查

API 提供两个健康检查：

- `GET /health/live`：进程存活，不检查依赖。
- `GET /health/ready`：检查 Redis 与 PostgreSQL。

建议上线前执行：

```powershell
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

`/metrics` 使用独立 `METRICS_TOKEN` 保护：

```powershell
curl -H "Authorization: Bearer <METRICS_TOKEN>" http://127.0.0.1:8000/metrics
```

## 5. 队列与 Worker 运维

任务流使用 Redis Streams：

- 主队列：`research:tasks`
- Consumer Group：`research-workers`
- 死信队列：`research:tasks:dead-letter`
- 状态键前缀：`research:task-status:*`

Worker 已具备：

- 唯一 consumer name，避免多 Worker 冲突；
- `XAUTOCLAIM` 接管异常退出 Worker 遗留的 pending 任务；
- 失败重试，超过阈值进入死信队列；
- 任务状态持久写入 Redis；
- 完成/失败/暂停/取消结果写入 PostgreSQL。

排障建议：

```powershell
docker exec deepresearch-redis redis-cli XLEN research:tasks
docker exec deepresearch-redis redis-cli XPENDING research:tasks research-workers
docker exec deepresearch-redis redis-cli XLEN research:tasks:dead-letter
```

如果 Redis 已启用密码，使用：

```powershell
docker exec -e REDISCLI_AUTH=<REDIS_PASSWORD> deepresearch-redis redis-cli XLEN research:tasks
```

## 6. 数据持久化与备份恢复

持久化范围：

- PostgreSQL：用户、会话归属、研究结果、审计事件；
- Redis：任务队列、任务状态、SSE 事件、配额、checkpoint；
- Milvus/MinIO/Etcd：知识库向量和对象存储。

当前已提供备份脚本：

```powershell
.\scripts\backup.ps1
```

备份输出目录：`backups/<timestamp>/`，包含：

- `postgres.dump`
- `redis.rdb`
- `manifest.json`

恢复 PostgreSQL：

```powershell
.\scripts\restore-postgres.ps1 -BackupFile .\backups\<timestamp>\postgres.dump
```

恢复前建议先停止 API 和 Worker，避免恢复过程中继续写入。

## 7. 鉴权、权限与审计

安全边界：

- 所有业务 API 默认需要登录；
- 任务恢复、SSE、取消、状态读取、结果读取均校验 thread ownership；
- LangGraph 原生端点使用 default-deny，并按 owner 过滤；
- Session 有 idle TTL 和 absolute TTL；
- 修改密码后会注销该用户所有 session。

审计事件写入 `audit_events`：

- 登录成功/失败；
- 修改密码成功/失败；
- 提交研究任务成功/失败/拒绝；
- 取消任务成功/拒绝；
- 读取研究结果成功/未找到/拒绝。

审计 details 不应包含密码、API Key、完整 prompt 等敏感内容。

## 8. 限流与成本控制

Redis Lua 原子配额覆盖：

- 用户分钟级请求数；
- 用户每日请求数；
- 用户并发研究任务数。

应用层还会限制：

- `initial_search_query_count` 最大值；
- `max_research_loops` 最大值。

LLM 封装层记录：

- 请求数；
- token usage；
- 按 `MODEL_PRICING_JSON` 估算成本。

## 9. 质量门禁

本地提交前建议执行：

```powershell
python scripts/secret_scan.py
cd backend
python -m pytest -q
cd ..\frontend
npm run build
```

评测门禁通过 `backend/eval/run_eval.py` 参数控制：

```powershell
cd backend
python eval/run_eval.py --min-e2e-score 3.5 --min-component-score 3.0 --max-errors 0
```

门禁失败会以退出码 `2` 结束，适合接入 CI/CD。

GitHub Actions 已配置：

- 后端依赖安装与全量 pytest；
- 前端 `npm ci` 与 `npm run build`；
- Docker Compose 配置校验；
- Shell/PowerShell 脚本语法检查；
- committed-file 密钥扫描。

## 10. 发布与回滚

发布前检查：

1. `git status --short` 为空；
2. 后端测试通过；
3. 前端构建通过；
4. `docker compose --env-file .env.docker.example config --quiet` 通过；
5. 备份脚本成功生成 `postgres.dump` 和 `redis.rdb`；
6. `/health/ready` 返回 `ok`。

回滚建议：

1. 停止 API 和 Worker；
2. 切回上一个 Git commit；
3. 必要时恢复 PostgreSQL 备份；
4. 启动 API 和 Worker；
5. 检查 `/health/ready`、`/metrics`、任务队列 pending/dead-letter。

## 11. 当前仍需注意

当前工程化已经具备企业部署雏形，但还不是完整企业平台：

- 没有接入企业 SSO、RBAC、LDAP/OIDC；
- 没有集中日志系统和告警平台；
- 没有正式的对象存储/数据库托管高可用；
- 没有灰度发布系统；
- 没有长期评测数据集和人工验收流程。

这些是后续从“单机工程化”走向“企业生产平台”的下一阶段。
