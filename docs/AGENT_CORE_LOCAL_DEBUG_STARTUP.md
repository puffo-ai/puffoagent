# Agent Core Local Debug Startup

Use this for local backend + Web + agent-core debugging on macOS.

Recommended ports:

```text
backend:    3000
web:        5173
agent-core: 63387
postgres:   5432
```

## 1. Backend

```bash
cd /Users/glimmer/Desktop/projects/puffo.ai/puffo-server

# First time, or when Postgres is not running:
# make db-up

PORT=3000 WEB_BASE_URL=http://localhost:5173 make run-dev
```

Health check:

```bash
curl http://127.0.0.1:3000/health
```

`make run-dev` enables the local dev fixture harness:

```text
POST /_dev/reset
POST /_dev/seed/{scenario}
GET  /_dev/state
POST /_dev/auth-headers
```

If Postgres is running but startup fails with `role "puffo" does not exist`,
your local Postgres is not the docker-compose database expected by `.env`.
Either start the repo's Docker database with `make db-up`, or create the local
dev role/database explicitly:

```bash
psql -h 127.0.0.1 -p 5432 -U postgres -d postgres \
  -c "CREATE ROLE puffo LOGIN PASSWORD 'puffo';"
psql -h 127.0.0.1 -p 5432 -U postgres -d postgres \
  -c "CREATE DATABASE puffo OWNER puffo;"
```

## 2. Web

```bash
cd /Users/glimmer/Desktop/projects/puffo.ai/agent/puffo-core-han-group/client/web

# First time only:
# npm install
# npm run wasm:build

VITE_DEFAULT_SERVER_URL=http://localhost:5173/local npm run dev
```

Open:

```text
http://127.0.0.1:5173
```

The Vite `/local` proxy forwards to `http://localhost:3000`.

## 3. Agent Core

```bash
cd /Users/glimmer/Desktop/projects/puffo.ai/agent/agent-core

# After code changes:
# npm run build
# npm run build:native:dev

AGENT_CORE_HOME="$HOME/.agent-core-localdev" \
AGENT_CORE_SERVER_URL=http://127.0.0.1:3000 \
AGENT_CORE_DEV_ROUTES=1 \
npm run start -- --json
```

Health check:

```bash
curl http://127.0.0.1:63387/health
```

Provider check:

```bash
AGENT_CORE_HOME="$HOME/.agent-core-localdev" \
AGENT_CORE_SERVER_URL=http://127.0.0.1:3000 \
npm run doctor
```

## Notes

- Keep the agent-core terminal open. `agent start` is a foreground daemon.
- `AGENT_CORE_HOME="$HOME/.agent-core-localdev"` keeps local test daemon state
  separate from any real installed agent-core state.
- The current Web `AgentsPane` still contains older `/v1/*` bridge plumbing.
  The new localhost integration surface is under `client/web/src/agent-core/*`.
  When debugging new agent-core behavior, confirm the UI path you are using has
  been wired to `AgentCoreHttpClient`.
- Provider readiness still depends on each provider's own local auth. A CLI can
  be installed but not ready if the user has not logged in.
