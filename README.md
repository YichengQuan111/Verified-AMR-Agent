# AMR Agent

AMR Agent P0 is a controlled, verifiable orchestration platform for four warehouse AMRs. The LLM interprets goals and produces structured plans; deterministic Python/C++ components remain responsible for validation, planning, simulation, and evidence.

## Current implementation status

- P0-00: frozen scope and seed data.
- P0-01: Python/C++ project skeleton, locked direct dependencies, layered configuration, structured logging, environment and smoke checks.
- P0-02: local Qwen text-model launch profiles and service runbook.
- P0-03: OpenAI-compatible model gateway with alias gating, timeouts, version records, and one structured-output repair attempt.
- P0-04: strict Pydantic domain contracts and exported JSON Schemas.
- P0-05: five independent 2-shot prompts, bounded context summaries, provenance, and deterministic budgets.
- P0-06: FastAPI Router/Service/Repository stack with eight PostgreSQL core tables and forward-only Alembic migration.

## Quick checks (Windows PowerShell)

```powershell
Set-Location 'C:\Users\QYC\Documents\AMR_Agent'
& 'E:\Anaconda\envs\torch128\python.exe' -m pip install -r .\requirements.lock -r .\requirements-dev.lock
docker compose up -d postgres qdrant
.\scripts\run_smoke.ps1
```

从 P0-06 起，统一冒烟会执行幂等 Alembic upgrade，并在真实 PostgreSQL 上验证事务回滚；因此 PostgreSQL 必须先启动。该命令不会自动删除核心表。

The live model is intentionally a separate gate. Start exactly one Qwen profile first, then run:

```powershell
& 'E:\Anaconda\envs\torch128\python.exe' .\scripts\check_model_gateway.py
```

See [docs/PROJECT_SETUP.md](docs/PROJECT_SETUP.md), [docs/MODEL_GATEWAY.md](docs/MODEL_GATEWAY.md), [docs/DATABASE.md](docs/DATABASE.md), and [docs/P001_P003_FILE_GUIDE.md](docs/P001_P003_FILE_GUIDE.md) for setup, gateway contracts, database/API design, and the P0-01/P0-03 learning guide.

Ongoing repository rules and handoff entry points:

- [AGENTS.md](AGENTS.md): permanent requirements for comments, file-purpose records, handoff updates, and verification.
- [docs/FILE_PURPOSES.md](docs/FILE_PURPOSES.md): continuing file responsibility registry.
- [docs/HANDOFF_CONTEXT.md](docs/HANDOFF_CONTEXT.md): current status and downstream context for the next work package.
