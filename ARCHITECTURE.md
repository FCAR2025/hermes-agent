# Architecture — Hermes Agent

Hermes Agent (`hermes-agent`) is a single-tenant, self-improving personal AI agent sharing one Python agent core across an interactive CLI, a ~20-platform messaging gateway, an Ink React TUI, a FastAPI + React web dashboard, an IDE ACP server, and an Electron desktop shell.

## Physical Codemap

- `run_agent.py` & `agent/` — `AIAgent` facade (`run_agent.py`) plus turn-loop phases (`agent/turn_*.py`), prompt assembly, context compression (`trajectory_compressor.py`), memory, model resolution, and `agent/subdirectory_hints.py`. Guide: `@agent/AGENTS.md`.
- `tools/`, `model_tools.py`, `toolsets.py` — zero-dependency `tools/registry.py`, self-registering tool modules (`tools/*.py`), terminal execution backends (`tools/environments/`: local, docker, ssh, modal, daytona, singularity), tool discovery (`model_tools.py`), and surface-gated toolsets (`toolsets.py`). Guide: `@tools/AGENTS.md`.
- `gateway/` — multi-platform daemon facade (`gateway/run.py` + `gateway/run_*.py`), session guards, `gateway/platform_registry.py`, and direct adapters (`gateway/platforms/`). Guides: `@gateway/AGENTS.md`, `gateway/platforms/ADDING_A_PLATFORM.md`.
- `cli.py` & `hermes_cli/` — REPL and `_SLASH_DISPATCH` (`cli.py` + `hermes_cli/cli_*_mixin.py`), subcommands, profile-aware setup/config/auth/update pipelines, kanban (`hermes_cli/kanban*.py`), and FastAPI dashboard host (`hermes_cli/web_server.py` + `hermes_cli/web_routers/`). Guide: `@hermes_cli/AGENTS.md`.
- `plugins/` — runtime loader (`plugins/plugin_loader.py`) and bundled plugins across `platforms/` (including Agent-to-Agent in `plugins/platforms/a2a/` — design: `@plugins/platforms/a2a/DESIGN.md`), `memory/`, `context_engine/`, `model-providers/`, `browser/`, `kanban/`, `image_gen/`, `video_gen/`, `web/`. Guide: `@plugins/AGENTS.md`.
- `cron/` — scheduled/interval job store (`cron/jobs.py`) and ticker/dispatcher (`cron/scheduler.py` + `cron/scheduler_*.py`). Guide: `@cron/AGENTS.md`.
- `skills/` & `optional-skills/` — categorical built-in skills (`skills/`), opt-in catalog skills (`optional-skills/`), and skill curation (`agent/curator*.py`). Guide: `@skills/AGENTS.md`.
- `tui_gateway/` & `ui-tui/` — Python JSON-RPC backend (`tui_gateway/server.py` + `methods_*.py`) driving the Ink React terminal UI (`ui-tui/` + `ui-tui/packages/hermes-ink`). Guide: `@tui_gateway/AGENTS.md`.
- `apps/desktop/`, `apps/shared/`, `apps/bootstrap-installer/`, `web/`, `acp_adapter/` — Electron + React desktop app (`@apps/desktop/AGENTS.md`, `@apps/desktop/src/AGENTS.md`, visual contract: `@apps/desktop/DESIGN.md`), shared JSON-RPC client (`apps/shared/`), Tauri Rust installer (`apps/bootstrap-installer/src-tauri`), Vite React dashboard (`web/`, `@web/AGENTS.md`), and IDE ACP adapter (`acp_adapter/`).
- `hermes_state.py` (`hermes_state_*.py`), `hermes_constants.py`, `hermes_logging.py`, `providers/` — SQLite `SessionDB` facade + topical siblings, profile-aware `get_hermes_home()` paths, rotating logs, and model provider adapters.

## Architectural Invariants

- **Prompt caching is sacred:** System prompts and prior conversation turns remain byte-stable across turns (context compression is the sole history mutation). Slash commands that alter prompt state default to next-session deferral unless `--now` is passed; strict user/assistant role alternation never breaks.
- **Narrow core waist, expansive edges:** Every core tool schema is sent on every model call. Add capability via the Footprint Ladder: extend existing code → CLI + skill → `check_fn` service-gated tool → plugin → catalog MCP server → core tool last.
- **Session-scoped surface gating:** GUI/platform-specific tools live in named toolsets (`desktop_ui`, `project`) enabled per session via `_load_enabled_toolsets(platform)`, never gated on backend process env vars like `HERMES_DESKTOP=1`.
- **Facade + `<stem>_<topic>.py` siblings:** Former god files are thin facades backed by topical siblings. Siblings may late-import facades inside functions, never creating module-level import cycles. Enforce lint, `AGENTS.md` size caps (`scripts/ci/check_agents_md_size.py`), and code-health ratchets via `python3 scripts/check`.
- **Profile-aware paths & secret separation:** Always resolve user state via `get_hermes_home()` / `display_hermes_home()`. Behavioral config belongs in `config.yaml`; `.env` holds credentials only.
- **OS-level security boundary (`@SECURITY.md`):** Approval prompts, redaction, and Skills Guard are cooperative heuristics; true containment against adversarial inputs requires terminal-backend sandboxing or whole-process container/OpenShell isolation, plus mandatory caller allowlists on network-exposed adapters.

## Dependency Direction

```text
Leaf Primitives (hermes_constants.py, tools/registry.py)
  -> Tool & State Implementations (tools/*.py, providers/, hermes_state_*.py)
    -> Tool Orchestration (model_tools.py, toolsets.py)
      -> Agent Core (run_agent.py, agent/turn_*.py, trajectory_compressor.py)
        -> Host Surfaces (cli.py, hermes_cli/, gateway/, tui_gateway/, cron/, acp_adapter/)
          -> Client UIs (ui-tui/, web/, apps/desktop/ via apps/shared/ JSON-RPC)
```

Plugins (`plugins/*`) register against core ABCs and hooks without modifying core modules.
