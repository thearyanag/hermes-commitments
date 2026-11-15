# Validation scope

This is a development build, not a production certification. The baseline is
unmodified Hermes v0.23.1 at
`3b421578588243f920886e6fe79a6faa87ea026e`. No patched Hermes fork is required.

## Automated checks

The release workflow runs the Python behavior tests with the pinned Hermes
environment (including real SessionDB tests), Node Desktop protocol tests, Ruff,
and Hermes `plugins doctor --ci` plus `plugins validate --install-deps` against
the exported package. Tests do not require provider credentials or send messages.
Packaging tests check that unrelated files are omitted, symlinks are rejected,
and identical source produces identical archives.

The Desktop tests substitute a fake SDK in a test-only dynamic import. Shipped
`desktop/plugin.js` imports only `@hermes/plugin-sdk`; the test loader is not part
of the installable package.

## Local live observations, 13–14 November 2026

- A private-chat promise was captured, followed up in real time, updated after a
  progress reply, and closed after completion. The model deferred a later nudge.
- A promise from private chat A was completed through the plugin control tool in
  private chat B; returning to A reflected completion.
- A controlled Bot Chat delivery ran through the worker, ledger, Desktop SDK,
  gateway, real conversation model, and renderer. One hidden input and one visible
  assistant reply were observed in the correct background chat. The instructions
  stayed hidden after switching away and back. The review decision was fixed for
  this transport test; it does not prove automatic timing or extraction quality.
- Two disposable gateway starts exercised worker ownership changes. Lease expiry
  was advanced between starts; this was not a downtime soak.

These are summarized local observations, not independently reproducible CI runs.
Raw transcripts, account configuration, fixture databases, and machine-specific
live harnesses are deliberately excluded from the public source package.

## Open qualification and implementation gaps

- Cold/retired, busy, restarted, and disconnected delivery via the new Desktop
  route needs live qualification. Legacy Python injection refuses retired local
  sessions on the baseline. Local delivery requires connected Desktop on the
  active profile; other profiles wait.
- Telegram/Discord and other messaging transports need separate live checks.
- Group topics remain isolated. Cross-topic access, multi-bot room observation,
  and membership-loss handling need integration. Group-to-DM routing is refused
  until replies can reconcile safely across those scopes.
- Strict silent cancellation after admission and universal delivery receipts
  are not available. Accepted, generated, and delivered are different outcomes;
  ambiguous attempts are not blindly retried. No exactly-once guarantee.
- Longer profile-switching/restart/downtime tests, native-cron coexistence, and
  broader model evaluations (ambiguity, false completion, unnecessary nudges)
  remain necessary.
- Lifecycle behavior depends on host identity metadata. Inputs above 24,000
  characters or serialized model context above 100,000 characters stay pending
  with a capacity error; larger-history retrieval remains work.

The minimum version is an API floor, not proof that every future Hermes release
or platform works. Automated Linux CI and local macOS observations must be
reported separately; a workflow file is not evidence that hosted CI has passed.
