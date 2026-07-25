# Workspace status — INERT (read before running anything here)

**This is not the live Hermes agent.** The live agent is `/home/info/.hermes/hermes-agent`
(v0.19.0, branch `joy-main-20260725`), run by `hermes-gateway.service`.

Verified 2026-07-25: nothing on this box references this workspace — no systemd unit,
no script in `~/scripts`, no `~/.local/bin` wrapper, no crontab entry. The
`/home/info/hermes` symlink points at the parent directory but nothing follows it.

## Why HEAD is pinned at v0.10.0

`HEAD` sits on `3c20ded04` (v0.10.0, April 2026) **on purpose**. The unique content here
is `hooks/compliance_gates/` (base, letter, sms, status + tests) and `roles/`, authored
2026-04-20/21 against that tree's hook API. Updating this checkout to current upstream
would leave that code orphaned against an incompatible base, so it was left pinned.

Those files were untracked until 2026-07-25 — the only copy on the box, one `git clean`
from loss. They are now committed on branch `preserve/compliance-gates-20260725`.

## If you want this workspace current

Port `hooks/compliance_gates/` to the current hook API first, then update — do not
fast-forward and hope. Rollback notes: `/opt/agentic/shared/core/runbooks/hermes-rollback.md`.
