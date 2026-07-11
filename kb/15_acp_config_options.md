# ACP Session Config Options

This note defines the common model, reasoning, mode, and model-related
configuration surface shared by the visible ACP TUI and tmux-team.

The authoritative protocol is the stabilized ACP v1 Session Config Options
contract. Provider model catalogs and option identifiers are discovered at
runtime; tmux-team and the TUI must not maintain hard-coded provider lists.

## ACP Contract

An ACP session creation or load response may include the complete
`configOptions` list.

Each option has:

- `id`: agent-defined stable identifier within the session;
- `name` and optional `description`;
- `category`: commonly `model`, `thought_level`, `mode`, or `model_config`;
- `type`: `select` or `boolean`;
- `currentValue`;
- `options` for a select option.

The client changes one option with:

```json
{
  "method": "session/set_config_option",
  "params": {
    "sessionId": "session-id",
    "configId": "model",
    "value": "provider-model-id"
  }
}
```

The agent response contains the complete updated `configOptions` list. The
client replaces its local state with that list rather than patching one field.

The agent may also send:

```json
{
  "method": "session/update",
  "params": {
    "sessionId": "session-id",
    "update": {
      "sessionUpdate": "config_option_update",
      "configOptions": []
    }
  }
}
```

That notification also replaces the complete local configuration state.

## Legacy Modes

ACP config options supersede the older session modes API.

- When `configOptions` contains a `category="mode"` option, the TUI uses
  `session/set_config_option`.
- When config options are absent but legacy `modes` are present, the existing
  `session/set_mode` path remains available.
- The TUI must not present duplicate mode selectors.

## Toad State And UI

Toad stores the complete current config-option list on its active ACP agent and
publishes updates to the conversation UI.

Local Toad commands:

- `/model`: select the advertised `category="model"` option;
- `/effort`: select the advertised `category="thought_level"` option;
- `/mode`: select the advertised `category="mode"` option;
- `/config`: inspect or change every advertised option, including
  `model_config` and unknown future categories.

The command palette uses the same picker implementation. Commands are shown
only when the corresponding category exists. Unsupported commands explain that
the active agent did not advertise the option.

Select options render only values supplied by the agent. Boolean options render
an on/off selector. Toad sends the selected raw value and uses the returned full
state as confirmation.

Initial implementation changes config only while the ACP session is idle.
Busy, asking, starting, failed, or quiesced sessions reject mutation.

## Toad Control Socket

The generic local control socket adds two protocol-v1 actions.

### `configOptions`

Request:

```json
{
  "version": 1,
  "id": "request-id",
  "action": "configOptions",
  "sessionId": "optional-session-id"
}
```

Response:

```json
{
  "version": 1,
  "id": "request-id",
  "ok": true,
  "sessionId": "session-id",
  "configOptions": []
}
```

If the agent did not advertise config options, return an empty list. Absence is
not replaced by a provider-specific fabricated catalog.

### `setConfig`

Request:

```json
{
  "version": 1,
  "id": "request-id",
  "action": "setConfig",
  "sessionId": "optional-session-id",
  "configId": "model",
  "value": "provider-model-id"
}
```

`value` is a string for select options and a boolean for boolean options.

Success returns the complete confirmed state:

```json
{
  "version": 1,
  "id": "request-id",
  "ok": true,
  "sessionId": "session-id",
  "configOptions": []
}
```

Stable errors:

- `config_options_unsupported`
- `unknown_config_option`
- `invalid_config_value`
- existing session/state errors such as `not_ready`, `unknown_session`, and
  `ambiguous_session`

The control action calls the same Toad/ACP mutation path as the interactive
picker. It never edits provider configuration files or starts a new session.

## tmux-team CLI

```text
tmux-team runtime options ROLE
tmux-team runtime configure ROLE --set CONFIG_ID=VALUE [--set ...]
```

`runtime options` prints option ID, category, type, current value, and available
select values from the live Toad response.

`runtime configure`:

1. requires a visible `acp_tui` role and idle control-socket session;
2. fetches the live options;
3. validates each requested ID and value against advertised type/options;
4. sends `setConfig` sequentially;
5. treats each returned full option list as authoritative;
6. verifies the session ID did not change;
7. atomically persists each agent-confirmed full state before attempting the
   next requested change;
8. appends same-session config-change lineage for each confirmed mutation.

The config stores:

- `acp_config`: mapping of every advertised option ID to confirmed current
  value;
- `acp_model`: current value of the first `category="model"` option;
- `acp_effort`: current value of the first `category="thought_level"` option;
- `acp_mode`: current value of the first `category="mode"` option.

`model_config` and unknown categories remain available in `acp_config` without
gaining provider-specific top-level fields.

## Session Lineage

Same-session configuration does not create a handoff capsule or a new lineage
segment. It appends a `config_changed` JSONL event containing:

- timestamp, actor, role, provider, and session ID;
- requested changes;
- confirmed old and new current-value maps.

Provider/session replacement continues to use `runtime prepare/switch`.

## Safety

- Never hard-code model names or reasoning levels.
- Never persist an unconfirmed requested value.
- Never infer success from a successful socket write alone.
- Never silently start a new session for a config change.
- Serialize configuration commands per role so concurrent operators cannot
  persist an older confirmed state over a newer provider response.
- Refuse mutation when the session ID in the response differs from the current
  binding.
- Preserve task bodies exclusively in SQLite.
- A failed multi-option command reports the confirmed state returned by the
  last successful change; it does not claim atomic provider rollback.

## Acceptance Checks

1. Session new/load captures full config options.
2. Agent `config_option_update` replaces state.
3. Select and boolean options validate correctly.
4. Model/effort/mode pickers use categories, not hard-coded IDs.
5. Legacy mode fallback remains functional when config options are absent.
6. Socket `configOptions` returns the exact current list.
7. Socket `setConfig` returns the agent-confirmed complete replacement list.
8. Human and control-socket changes share one mutation path.
9. tmux-team persists only confirmed values and session identity.
10. Same-session lineage records old/new values without creating a handoff.
