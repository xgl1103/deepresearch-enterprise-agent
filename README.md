# DeepResearch 企业工程化实战项目

本项目是一个面向企业工程化场景改造的 DeepResearch 应用，核心目标不是只跑通 Demo，而是把“研究型 Agent”拆成可鉴权、可排队、可持久化、可观测、可运维的服务形态。

![DeepResearch 应用截图](./app.png)

## 开源说明

本仓库以 MIT License 开源，适合用于学习、二次开发和工程实践参考。项目中的 `.env.example` 与 `.env.docker.example` 只提供配置模板，不包含真实密钥。

请不要提交以下内容：

- 真实 API Key、App Token、数据库密码、Redis 密码；
- 本地 `.env`、日志、缓存、备份、构建产物；
- 任何包含个人账号、企业内部地址或未脱敏业务数据的文件。

## 当前能力

- 前端：React + Vite，支持模型选择、研究强度选择、SSE 进度展示、计划确认后继续研究。
- 后端 API：FastAPI，提供登录、模型列表、研究任务提交、任务流式事件、取消、结果读取、健康检查和 metrics。
- Agent 编排：LangGraph State 管理研究计划、搜索、反思、写作和最终报告生成。
- 队列与 Worker：Redis Streams 承接异步任务，API 与 Worker 可分离运行。
- 数据持久化：PostgreSQL 保存用户、任务归属、研究结果、来源、审计事件。
- 权限控制：登录 Session、任务 owner 校验、LangGraph 原生端点 default-deny。
- 可观测性：健康检查、Prometheus metrics、结构化日志、审计事件。
- 运维脚本：Docker Compose 依赖服务、备份恢复脚本、密钥扫描脚本、GitHub Actions。

## 项目结构

```text
.
├── backend/                 # FastAPI、LangGraph、Worker、数据库与测试
│   ├── src/agent/
│   ├── test/
│   ├── langgraph.json
│   └── pyproject.toml
├── frontend/                # React + Vite 前端
│   ├── src/
│   └── package.json
├── docs/
│   └── enterprise-runbook.md # 当前单机部署形态的企业化运维说明
├── scripts/                 # 备份、恢复、密钥扫描脚本
├── docker-compose.yml
├── .env.docker.example
└── .github/workflows/ci.yml
```

## 环境准备

- Python 3.12 或兼容版本
- Node.js 22+
- Docker Desktop
- PostgreSQL / Redis 由 `docker-compose.yml` 启动
- 可用的大模型 API Key、MCP 搜索应用配置

密钥不要提交到 Git。按需复制示例文件：

```powershell
Copy-Item .env.docker.example .env
Copy-Item backend/.env.example backend/.env
```

然后在 `backend/.env` 中配置：

- `APP_TOKEN`
- `MCP_APP_ID`
- `METRICS_TOKEN`
- `DATABASE_URL`
- `REDIS_URL`
- 模型相关配置

## 本地启动

1. 启动依赖服务：

```powershell
docker compose up -d
```

2. 初始化数据库：

```powershell
cd backend
python -m agent.db.init_db
```

3. 启动 API 服务：

```powershell
cd ..
.\run_uvicorn_backend.bat
```

4. 启动独立 Worker：

```powershell
.\run_worker.bat
```

5. 启动前端：

```powershell
.\run_frontend.bat
```

历史脚本 `run_fontend.bat` / `run_fontend.sh` 保留兼容，但推荐使用拼写正确的 `run_frontend.*`。

## 常用地址

- 前端：http://127.0.0.1:5173/
- API：http://127.0.0.1:2024/
- Swagger：http://127.0.0.1:2024/docs
- 存活检查：http://127.0.0.1:2024/health/live
- 就绪检查：http://127.0.0.1:2024/health/ready

## 质量检查

提交或交付前建议执行：

```powershell
python scripts/secret_scan.py

cd backend
python -m pytest -q

cd ..\frontend
npm run lint
npm run build
```

当前已配置 GitHub Actions：

- 后端依赖安装与全量测试
- 前端 lint 与生产构建
- Docker Compose 配置校验
- 运维脚本语法检查
- committed-file 密钥扫描

## 企业化说明

当前项目已经具备企业部署雏形，但仍不是完整企业平台。详细边界见 [docs/enterprise-runbook.md](./docs/enterprise-runbook.md)。

后续可以继续补：

- 企业 SSO / RBAC；
- 集中日志与告警；
- 数据库与对象存储高可用；
- 灰度发布；
- 长期评测集与人工验收体系；
- 更严格的 Python ruff 规则治理。
