<h1 align="center">Commitments</h1>

<p align="center">
  <strong>Give Hermes a reason to check back.</strong>
</p>

https://github.com/user-attachments/assets/ed6bb92f-43ac-4c4b-94b9-104954de110e

You tell Hermes you'll send the proposal, finish the draft, or get back to someone.
Commitments keeps track of those promises and brings them back into the conversation
when a follow-up would help. As you make progress, finish, or change your mind, it
updates what is still outstanding.

Talk naturally. You don't need to turn every promise into a reminder.

## What it feels like

*Illustrative conversation—the model decides whether and when to follow up.*

> **You:** I'll send Maya the proposal tomorrow. The draft is nearly ready.
>
> *Later…*
>
> **Hermes:** Did you get the proposal out to Maya, or is anything still blocking it?
>
> **You:** Sent it this morning.
>
> *Commitments closes the item. No further nudges for that promise.*

Capture happens quietly. Follow-ups appear as ordinary Hermes replies, with the
internal wake-up instructions hidden from the Desktop chat.

## Why install it?

- **Keep promises from disappearing into chat history.** Track commitments made
  by you or Hermes, with persistent state between conversations.
- **Get a useful check-in.** The model considers deadlines, progress, blockers,
  and previous nudges when deciding whether to follow up or wait.
- **Pick up in another private chat.** Verified owner-private conversations within
  the same Hermes profile share commitment state.
- **Close the loop naturally.** “Sent it,” “I'm still waiting on feedback,” or
  “Let's drop this” can update the item. You can inspect or correct it explicitly.
- **Use the Hermes setup you already have.** Background processing uses a small
  model through Hermes's existing authentication. No separate provider account,
  external queue, or extra Python dependencies.

Underneath the conversation, the plugin maintains a SQLite ledger, runs background
reviews, and submits follow-up turns. State and scheduling live beyond a single
prompt or chat window.

**Current status:** Development build, tested on unmodified Hermes **0.23.1**.
The private-chat loop and controlled Desktop Bot Chat delivery have been exercised.
Full group support and external messaging delivery are still being qualified.
See [current limitations](#current-limitations).

## Get started

You'll need Hermes **0.23.1 or later**, an authenticated model provider, and a
running gateway. For local follow-ups, keep Hermes Desktop open and connected to
the profile you want to use.

### 1. Install the plugin

```sh
hermes plugins install thearyanag/hermes-commitments --enable
```

This uses [Hermes's built-in installer](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins)
to fetch, scan, and install the plugin and request activation. Follow any consent
prompts, then complete the setup below.

Catalog approval is not required for installation by repository. Source builds
and release checks are documented in [Release packaging](docs/RELEASING.md).

### 2. Choose a background model

Open **Settings → Models → Auxiliary models → Commitments → Change** in Hermes
Desktop. Choose a small model from a provider you're already signed in to.

Prefer the terminal? Run `hermes model`, choose **Configure auxiliary models**,
then **Commitments**. Both pickers save the selection for you—no YAML editing.

Background checks use the `commitments` auxiliary model; the conversational
follow-up uses the chat's main model. Hermes may fall back to its main model if
an auxiliary request fails. Available model/usage information is recorded locally;
missing cost information stays unknown. There is no fixed spending guarantee.

### 3. Enable Desktop follow-ups

Allow Commitments to initiate follow-up turns in your intended Hermes profile:

```sh
hermes config set plugins.entries.commitments.allow_gateway_injection true
```

Restart the gateway after configuring it.

In Hermes Desktop, open **Capabilities → Plugins**, use **Rescan** after a manual
install, and enable **Commitments**. Both the Python and Desktop components must
be enabled for local follow-ups.

Open a private chat and run:

```text
/commitments status
```

Then use Hermes normally. Check `/commitments list` to see what it has captured.
The model may decide to wait; a review does not always produce a message.

Desktop services its active connected profile. If you close Desktop or switch
profiles, local follow-ups wait. Old waiting decisions are reviewed again when
that profile reconnects.

## You're in control

Ask Hermes about your commitments in conversation, or use `/commitments` directly.

| What you want to do | Command |
| --- | --- |
| See what's tracked | `/commitments list` |
| Inspect an item or its changes | `/commitments show ID` · `/commitments history ID` |
| Mark done, drop, or reopen | `/commitments done ID` · `/commitments drop ID` · `/commitments reopen ID` |
| Undo a change or forget an item | `/commitments undo ID` · `/commitments forget ID` |
| Pause or resume follow-ups in this scope | `/commitments pause` · `/commitments resume` |
| Check setup and processing state | `/commitments status` |
| Choose another verified owner-private destination | `/commitments sources`, then `/commitments route ITEM_ID TARGET_SOURCE_ID` |

The model decides timing and frequency unless you set preferences. There are
**no default quiet hours or frequency caps**. For example:

```text
/commitments settings {"quiet_hours":{"start":"23:00","end":"08:00","timezone":"Asia/Kolkata"}}
/commitments settings {"frequency_limit":{"count":2,"window_seconds":86400}}
```

Use `settings global JSON` for tracker-wide defaults. Scope-specific settings take
precedence. Set `quiet_hours` or `frequency_limit` to `null` to clear it. An explicit
one-off reminder time can override quiet hours for that occurrence.

Only the verified tracker owner can change settings. Local ownership follows the
Hermes OS user. Messaging identities can be explicitly linked through the plugin's
`owner_identities` setting using verified `platform:user_id` values; display names
or group-admin status do not grant settings authority.

Archiving a source chat pauses its follow-ups. Unarchiving makes them eligible
again. Reset preserves commitments where Hermes supplies the old/new identities.
Deleting the original source chat deletes its anchored commitments and pending work.

## Privacy and permissions

Commitments reads the active profile's conversation history and source metadata,
host configuration, and gateway status. Relevant excerpts and commitment context
are sent to your configured model provider through Hermes's existing authentication
and fallback. The plugin never reads or stores provider credentials.

Its ledger lives at `<Hermes home>/plugin-data/commitments/data.db`. It stores
commitments, supporting quotes, pending observations, attributed changes, routing,
model usage, and follow-up attempts. Full observation bodies clear after successful
reconciliation. History stays until deletion or your configured `retention_days`
for inactive items. Non-content attempt counts and deletion markers remain to
prevent stale work from returning. Hermes's own chat history is separate.

A worker thread runs inside the gateway, and the Desktop component polls through
the Hermes SDK. The plugin starts no subprocesses, runs no shell commands, makes
no direct external HTTP requests, and sends no telemetry. Follow-up turns retain
the conversation's normal tools and approval policy. It does not patch Hermes,
grant itself permissions, or update itself.

Hidden wake-up instructions remain in backend/model history. Hiding them from the
chat is a presentation feature, not encryption or deletion.

## Current limitations

- **Desktop availability:** Local follow-ups need a running gateway and Desktop
  connected to the relevant profile. Cold/closed, busy, restarted, and disconnected
  delivery paths need further live testing.
- **Groups and messaging:** Full group/room support and Telegram/Discord transport
  qualification remain unfinished. Topics stay isolated, and group-to-DM routing
  is currently refused.
- **Delivery uncertainty:** Submission acceptance does not prove visible delivery.
  Ambiguous attempts are not blindly resent. An already-admitted turn may finish
  or show a cancellation message; there is no exactly-once or silent-cancellation
  guarantee.
- **Model judgment and scale:** Capture, completion, and timing need broader
  evaluation. Inputs above 24,000 characters or serialized model context above
  100,000 characters stay pending with a capacity error.

See [validation scope](docs/VALIDATION.md) for tested scenarios and remaining work.
The Hermes version floor describes the required APIs; later releases still need
compatibility checks.

## Disable or remove

`/commitments pause` pauses follow-ups in the current scope; it does not stop all
tracking. To stop the plugin, disable its Desktop component, run
`hermes plugins disable commitments`, and restart the gateway. An already-admitted
turn may still finish.

`hermes plugins uninstall commitments` removes the installation but retains plugin
data. For a complete data reset, stop both components and delete only
`<Hermes home>/plugin-data/commitments/` in the intended profile. This also removes
deduplication history, so old conversations can be re-observed after reinstalling.

## Development

[Contributing and tests](CONTRIBUTING.md) · [Validation](docs/VALIDATION.md) ·
[Release packaging](docs/RELEASING.md) · [Changelog](CHANGELOG.md)

Licensed under [MIT](LICENSE).
