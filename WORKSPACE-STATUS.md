# Workspace status — CURRENT SOURCE, INERT COMPLIANCE ARCHIVE

This checkout is not a live Hermes runtime. On 2026-09-06 it was fast-forwarded
to the verified history-join commit `ae5dc9ff7f0dd0a608cdb1722a9a4df5a401733c`,
which descends from current upstream `693641aa8b4359c602283bdbbc14041e03bc47bc`
and the preserved compliance branch at `194eba9ce2b1f94b40fb2b21e43605ac85fcadeb`.

The live information gateway still launches from `/home/info/.hermes/hermes-agent`.
Separately supervised FCAR gateway and dashboard processes use the pinned
`/opt/fcar-command-runtime/c66a78ba` release. No service, wrapper, scheduler, or
running process references this checkout.

## Preserved compliance material

`hooks/compliance_gates/` and `roles/ces_ceo.md` remain archival local material.
Their 17 focused unit tests pass on the current upstream tree, but they are not
registered in the current Hermes hook/runtime path and have no live consumer.
Do not treat their presence or green helper tests as deployment, current legal
policy approval, or authorization to send messages or change lead status.

The remaining medium gaps are explicit:

- no current-hook registration or end-to-end dispatcher test;
- no authoritative policy-owner review against current legal/compliance rules;
- no live runtime discovery, negative authorization, or delivery readback.

Keep this material inert until a separately scoped integration supplies those
owners and proofs. Do not expand its legal rules opportunistically.

The untracked `.omc/` state belongs to existing workspace tooling and was
preserved byte-for-byte across the source fast-forward.
