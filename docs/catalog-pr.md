# Draft: Add Commitments to the plugin catalog

Commitments notices promises in Hermes conversations, persists their progress,
and evaluates whether a useful follow-up is due. It shares state across verified
owner-private chats in a profile and closes items using conversational evidence
or explicit controls. A Desktop component submits hidden wake-up instructions;
the normal assistant reply remains visible.

This is a development release. Local hidden delivery requires a running gateway
and Desktop connected to the relevant profile. Full groups/rooms, group-to-DM
reconciliation, external messaging transport qualification, and cold/busy/restart
delivery qualification remain incomplete. It does not claim exactly-once delivery
or strict silent cancellation after admission.

## Public surfaces

Python tool/command and lifecycle/model/admission hooks, auxiliary LLM task,
session-scoped plugin RPCs, plugin SQLite storage, read-only SessionDB, host config,
session identity and runtime status APIs; Desktop `@hermes/plugin-sdk` gateway
requests. No core monkeypatching, DOM/CSS rewriting, self-updater, compiled
payload, or extra Python dependency. Tested baseline: unmodified Hermes v0.23.1,
commit `3b421578588243f920886e6fe79a6faa87ea026e`.

## Disclosures

- Network: conversation excerpts and commitment context go to the selected model
  provider through Hermes's existing auth and fallback. No direct third-party
  HTTP calls or plugin telemetry. Follow-up turns use the conversation's normal
  model/tools and approvals, so their network behavior follows that configuration.
- Reads: active-profile conversation history/metadata, host configuration and
  gateway status outside plugin data; local OS identity for ownership.
- Storage: local SQLite holds commitments, supporting quotes, pending
  observations, attributed changes, routing, usage and dispatch state. Full
  observations clear after reconciliation; inactive history persists until
  deletion or configured retention. No credentials are read or stored.
- Background: a gateway-owned worker thread and Desktop SDK polling. No shell
  commands, child processes, separate daemon, or approval bypass. Local follow-ups
  wait when Desktop is absent. Ambiguous submissions are not blindly replayed.
- Permission: the operator must enable the components, configure the auxiliary
  model and explicitly grant `allow_gateway_injection`. The plugin never grants
  itself permission.

## Fill before submitting

- Public source commit: **PENDING** (full 40-character SHA).
- Hosted CI and exact-package doctor/validator receipts: **PENDING**.
- Sanitized screenshots of visible Desktop follow-up behavior: **PENDING**.

See `README.md` for setup, data handling, and limitations, and
`docs/VALIDATION.md` for the scope of local observations. This draft is not a
submitted PR or catalog acceptance.
