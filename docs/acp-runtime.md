# External ACP TUI Runtime

The experimental ACP runtime runs each managed role as a visible Toad TUI. Toad owns the provider process and ACP
session; tmux-team owns durable work state and sends compact wake prompts through a private Unix socket. Task bodies
remain in SQLite and are never typed into the terminal composer.

## Install And Preflight

ACP support currently requires Python 3.14 and the temporary Toad control-socket branch:

```bash
uv tool install --python 3.14 \
  "tmux-team[acp] @ git+https://github.com/PheelaV/tmux-team.git"

toad --version
agent status
codex --version
claude --version
pool --version

npm install -g @agentclientprotocol/codex-acp
npm install -g @agentclientprotocol/claude-agent-acp

# Pool: install from https://poolside.ai/get-started, then authenticate/configure:
pool login
make install-skill SKILL_PROVIDERS=pool
```

`tmux-team[acp]` installs the ACP TUI/control transport, not every provider adapter. Install only the adapter(s) you use;
bootstrap preflights the selected command and prints its provider-specific install hint when it is missing. Package
installation remains non-interactive and does not choose a provider for the user.

Pool skill discovery is separate from Codex plugin installation. Pool scans `.poolside/skills/`, `.agents/skills/`, and
`~/.config/poolside/skills/`; `make install-skill SKILL_PROVIDERS=pool` copies the canonical skill to the global Pool directory (or
`POOL_SKILLS_HOME`). Every Pool role must have the skill available before bootstrap.

## Instruction Profiles

ACP does not weaken the role startup invariant: every spawned role must load the `start-tmux-team` skill and read its
invariants. `--instruction-profile compact|guided` controls only how much role-loop guidance is repeated in the startup
prompt. Use repeatable `--role-instruction-profile ROLE=PROFILE` overrides for mixed-capability teams. The selected
profile is persisted in role metadata and is independent of provider/model names.

Canonical provider presets:

| Provider | Local stdio command | Authentication |
| --- | --- | --- |
| Cursor | `agent acp` | Cursor Agent login |
| Codex | `codex-acp` | local ChatGPT/Codex login, API key, or configured gateway |
| Claude | `claude-agent-acp` | local Claude credentials/settings through the official Claude Agent SDK |
| Pool | `pool acp` | `pool login` or Poolside-owned `POOLSIDE_API_URL` / `POOLSIDE_API_KEY` configuration |

These are local child processes launched by Toad. Claude ACP does not require a remote URL. Pool can target a remote
OpenAI-compatible deployment, but endpoint/model/auth configuration remains Pool-owned and must be supplied through
Pool settings or environment, never tmux-team config. `pool acp setup --editor zed|jetbrains` only registers editors;
Toad invokes `pool acp` directly. Bootstrap a team with:

```bash
tmux-team bootstrap --project-root . \
  --agent-runtime acp \
  --acp-tui-bin toad \
  --acp-provider claude \
  --goal "Inspect the failing test and report verified evidence."
```

Pool example with generic runtime-only endpoint placeholders:

```bash
POOLSIDE_STANDALONE_BASE_URL='http://<openai-compatible-host>/v1' \
POOLSIDE_STANDALONE_MODEL='<deployment-model>' \
POOLSIDE_API_KEY='<runtime-secret>' \
tmux-team bootstrap --project-root . --agent-runtime acp --acp-provider pool
```

Do not commit deployment URLs, API keys, or private network addresses. Prefer Pool's credential/settings facilities or
shell/session environment injection.

Use `--acp-agent-command` to override a preset with `npx`, a pinned/local executable, provider flags, or an arbitrary
ACP stdio adapter. Unknown providers require an explicit command. Bootstrap fails before creating team state when a
preset executable is missing and prints its install hint.

Apply provider-advertised options before the first startup prompt when model/cost or permission behavior must be
deterministic:

```bash
tmux-team bootstrap --project-root . --agent-runtime acp --acp-provider codex \
  --acp-initial-config model=gpt-5.6-terra \
  --acp-initial-config reasoning_effort=medium \
  --acp-initial-config fast-mode=false
```

IDs and values must come from that adapter's ACP config surface. Bootstrap fails rather than sending the startup turn
when an option is missing, invalid, or not confirmed.

The layout contains a Toad/ACP operator agent in `tt-control` and visible role panes in `tt-agents`; ACP teams do not
need `tt-app-server`. The control agent is not a managed role and receives no role inbox work. Bootstrap verifies each
Toad socket and provider session before reporting success.
Managed panes use Toad compact mode, which keeps the single-session tab bar hidden, uses the full pane width, and
sets runtime-neutral terminal titles such as `tmux-team: collector`.

## Permissions

Provider-specific permission policy stays provider-owned:

```bash
--acp-agent-command "agent --force acp"
```

For constrained operation, put policy in project-local `.cursor/cli.json` rather than changing the user's global
configuration. The tmux-team startup loop needs both commands below:

```json
{
  "approvalMode": "allowlist",
  "permissions": {
    "allow": [
      "Shell(command)",
      "Shell(tmux-team)"
    ]
  }
}
```

Add only the commands and scoped filesystem access required by the role, for example `Shell(git)`, `Shell(uv)`,
`Read(**/*)`, `Write(src/**/*)`, and `Write(tests/**/*)`. Keep secrets and destructive commands denied. If roles use
separate worktrees, make the project-local policy available in each worktree before bootstrap.

tmux-team does not modify provider-global permission files.

Codex ACP advertises approval and sandbox modes and supports `INITIAL_AGENT_MODE=agent-full-access` as an explicit
autonomous launch setting. Claude ACP reads Claude Code user/project/local settings; use project-local
`.claude/settings.local.json` for deliberate local policy. The live demo creates `bypassPermissions` only in its
disposable Claude worktrees after the operator enables the real-provider test.

## Delivery And Control

ACP wake delivery is:

```text
SQLite inbox -> private Unix socket -> visible Toad TUI -> ACP session/prompt -> provider agent
```

Inspect or control a role without touching terminal input:

```bash
tmux-team acp status implementer
tmux-team acp wake implementer
tmux-team acp cancel implementer
```

## Provider Session Handoff

At an idle boundary, capture durable continuity and replace the provider process in the same pane:

```bash
tmux-team runtime prepare implementer \
  --summary "Focused fix passes; full verification remains."

tmux-team runtime switch implementer \
  --provider claude \
  --model sonnet \
  --handoff-file .tmux-team/runtime/handoffs/implementer/<handoff>.md
```

The bounded capsule includes role, inbox metadata, todos, memory, and Git state, but not inbox bodies or a full
transcript. `runtime prepare` drains the role before confirming an idle, empty provider queue. `runtime switch` accepts
only the latest unchanged capsule prepared for that role and source session, then atomically quiesces new external
prompts before replacing the pane. Active turns are refused unless
`--cancel-active` reaches idle. Failed replacement leaves the role `draining` for explicit recovery. Inspect current
and previous session metadata with `tmux-team runtime show <role>`.

## Current Limits

- Same-session changes are limited to options advertised by the active ACP agent.
- Canonical `--acp-provider` values select their standard command unless `--acp-agent-command` overrides it.
- Replacing the provider command still requires a runtime handoff.

## Sleep And Resume

ACP sleep defaults to exact restoration:

```bash
tmux-team sleep --acp-resume-policy exact
tmux-team resume
```

Sleep requires every role to be idle with an empty external queue, verifies `resumeSupported`, quiesces new prompts,
and snapshots the provider session ID plus launch/binding metadata and a fallback capsule. Resume starts Toad with
`--session-id`, requires the loaded ID to match, and then re-wakes durable pending inbox work.

Use a fresh provider session only by explicit choice:

```bash
tmux-team resume --acp-resume-policy handoff
```

Handoff mode requires the saved capsule and sends its recovery prompt. Exact restoration never silently falls back to
handoff. Provider retention, credentials, host changes, or adapter incompatibility can still make exact load fail; the
team remains paused for operator recovery.
