# ACP Provider Matrix

## Decision

tmux-team treats ACP providers as local stdio children of Toad. It does not own a remote provider URL, provider
credentials, model catalogs, or global permission policy.

Three canonical provider names have ergonomic command presets:

| Provider | Preset command | Adapter ownership |
| --- | --- | --- |
| `cursor` | `agent acp` | Cursor Agent native ACP server |
| `codex` | `codex-acp` | `@agentclientprotocol/codex-acp`, backed by Codex app-server |
| `claude` | `claude-agent-acp` | `@agentclientprotocol/claude-agent-acp`, backed by the official Claude Agent SDK |
| `pool` | `pool acp` | Poolside Pool CLI native ACP server; deployment configuration remains Pool-owned |

An explicit `--acp-agent-command` always overrides the preset. This supports flags, `npx`, pinned/local executables,
and arbitrary future adapters without growing a provider-specific execution abstraction in core.

## Authentication And Policy

- Cursor uses Cursor Agent login and provider-owned CLI policy.
- Codex ACP uses local ChatGPT/Codex login, API-key environment, or a configured gateway.
- Claude ACP uses local Claude credentials and Claude Code settings. It does not require an ACP URL.
- Pool ACP uses `pool login` or Poolside API URL/key settings. `pool acp setup --editor ...` is not needed for Toad.
- Pool skill discovery uses `.poolside/skills/`, `.agents/skills/`, or `~/.config/poolside/skills/`; do not assume the
  Codex plugin directory alone guarantees native Pool skill registration.
- Direct OpenAI-compatible standalone deployments use Pool-owned runtime configuration such as
  `POOLSIDE_STANDALONE_BASE_URL`, `POOLSIDE_STANDALONE_MODEL`, and `POOLSIDE_API_KEY`; private endpoints and credentials
  must not enter tmux-team config examples or tracked fixtures.
- tmux-team checks that the local preset executable exists but never installs adapters or modifies global policy during
  bootstrap.

Autonomous test policy is explicit and disposable. The Codex live target sets `INITIAL_AGENT_MODE=agent-full-access`.
The Claude live target writes ignored `.claude/settings.local.json` files only into the generated demo worktrees. These
test conveniences are not production bootstrap behavior.

Initial model/cost options are a bootstrap boundary, not a post-start convenience. `--acp-initial-config` is applied
after the provider session advertises `configOptions` but before tmux-team submits its startup prompt. Any unsupported,
invalid, or unconfirmed value aborts bootstrap without consuming a model turn on the provider default.

## Instruction Profiles

Do not create provider- or model-specific skill forks. The canonical `start-tmux-team` skill and invariants remain
mandatory for every role. A role may use `compact` or `guided` startup instructions: compact states the versioned role
loop tersely, while guided repeats the expanded operational steps. Selection is explicit team/role configuration and is
persisted for recovery; tmux-team must not infer it from a changing model catalog.

## Test Contract

Cursor, Codex, Claude, and Pool use the same public-snapshot scenario and verifier. A provider does not pass merely by
starting. It must demonstrate:

- private control-socket wake delivery with task bodies remaining in SQLite;
- role-owned worktrees and durable inbox claim/ack/complete flow;
- obligations, one-shot watchdog pressure, notices, completion replies, milestones, and stable commit approval/sync;
- a real implementation commit independently verified by the collector;
- clean final inbox state;
- exact sleep/resume with matching provider session IDs.

Release validation treats native Codex as a fourth runtime path beside the three ACP providers. Native Codex keeps its
app-server transport and does not depend on Toad or an ACP adapter.

The real matrix is intentionally opt-in and sequential because it consumes provider calls. Deterministic unit and tmux
smoke tests remain credential-free and cover preset resolution and launch command construction.
