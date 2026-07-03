from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import TeamConfig


@dataclass(frozen=True)
class TeamPolicy:
    mode: str = "strict"


@dataclass(frozen=True)
class RolePolicy:
    can_send_as: tuple[str, ...] = ()
    can_send_to: tuple[str, ...] = ()
    can_notify: tuple[str, ...] = ()
    can_claim: tuple[str, ...] = ()
    can_ack: tuple[str, ...] = ()
    can_complete: tuple[str, ...] = ()
    can_capture_panes: tuple[str, ...] = ()
    can_use_send_keys: bool = False
    can_change_role_state: bool = False
    can_bind_app_server: bool = False
    can_approve_stable: bool = False
    can_sleep: bool = False


@dataclass(frozen=True)
class PolicyContext:
    actor: str | None = None
    mode: str = "strict"


class PolicyError(PermissionError):
    pass


def normalize_policy_mode(value: Any) -> str:
    if value is None:
        return "strict"
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized in ("strict", "default", "enforce", "enforced"):
        return "strict"
    if normalized in ("permissive", "breakglass", "break_glass", "yolo", "yolo_breakglass", "off", "disabled"):
        return "permissive"
    raise ValueError(f"Invalid policy mode: {value} (expected strict or permissive)")


def parse_team_policy(team_data: Mapping[str, Any]) -> TeamPolicy:
    raw_policy = team_data.get("policy")
    mode = team_data.get("policy_mode")

    if isinstance(raw_policy, Mapping):
        mode = raw_policy.get("mode", mode)
        if raw_policy.get("permissive") is True or raw_policy.get("breakglass") is True:
            mode = "permissive"
    elif raw_policy is not None:
        mode = raw_policy

    if team_data.get("policy_permissive") is True or team_data.get("policy_breakglass") is True:
        mode = "permissive"

    return TeamPolicy(mode=normalize_policy_mode(mode))


def parse_role_policy(raw_policy: Any) -> RolePolicy:
    if raw_policy is None:
        return RolePolicy()
    if not isinstance(raw_policy, Mapping):
        raise ValueError("role policy must be a TOML table")

    return RolePolicy(
        can_send_as=_string_tuple(raw_policy, "can_send_as"),
        can_send_to=_string_tuple(raw_policy, "can_send_to"),
        can_notify=_string_tuple(raw_policy, "can_notify"),
        can_claim=_string_tuple(raw_policy, "can_claim"),
        can_ack=_string_tuple(raw_policy, "can_ack"),
        can_complete=_string_tuple(raw_policy, "can_complete"),
        can_capture_panes=_string_tuple(raw_policy, "can_capture_panes"),
        can_use_send_keys=_boolean(raw_policy, "can_use_send_keys"),
        can_change_role_state=_boolean(raw_policy, "can_change_role_state"),
        can_bind_app_server=_boolean(raw_policy, "can_bind_app_server"),
        can_approve_stable=_boolean(raw_policy, "can_approve_stable"),
        can_sleep=_boolean(raw_policy, "can_sleep"),
    )


def authorize(config: TeamConfig, context: PolicyContext, action: str, **resource: str) -> None:
    mode = normalize_policy_mode(context.mode)
    actor = _normalized_actor(context.actor)
    if mode == "permissive" or actor is None or actor == "operator":
        return

    role_config = config.roles.get(actor)
    if role_config is None:
        raise PolicyError(f"Unknown actor role: {actor}")
    policy = role_config.policy

    if action == "message.send":
        sender = _required_resource(action, resource, "sender")
        recipient = resource.get("recipient")
        if not _role_matches(sender, actor, policy.can_send_as):
            raise PolicyError(f"actor {actor!r} is not authorized to send as {sender!r}")
        if policy.can_send_to and recipient is not None and not _matches(recipient, policy.can_send_to):
            raise PolicyError(f"actor {actor!r} is not authorized to send to {recipient!r}")
        return

    if action == "role.notify":
        _authorize_role_resource(actor, resource, "role", policy.can_notify, action)
        method = resource.get("method")
        if method == "send-keys" and not policy.can_use_send_keys:
            raise PolicyError(f"actor {actor!r} is not authorized to use tmux send-keys notification")
        return

    if action in ("inbox.next", "inbox.list", "inbox.reclaimable"):
        _authorize_role_resource(actor, resource, "role", policy.can_claim, action)
        return

    if action == "inbox.ack":
        _authorize_role_resource(actor, resource, "role", policy.can_ack, action)
        return

    if action == "inbox.complete":
        _authorize_role_resource(actor, resource, "role", policy.can_complete, action)
        return

    if action in ("memory.read", "memory.update"):
        _authorize_role_resource(actor, resource, "role", (), action)
        return

    if action == "milestone.add":
        if actor != "orchestrator":
            raise PolicyError(f"actor {actor!r} is not authorized to record milestones; send evidence to orchestrator")
        return

    if action == "milestone.list":
        return

    if action == "watch.list":
        role = resource.get("role")
        if not role or actor == "orchestrator" or role == actor:
            return
        raise PolicyError(f"actor {actor!r} is not authorized to list watches for role {role!r}")

    if action in ("watch.start", "watch.update", "watch.complete"):
        role = _required_resource(action, resource, "role")
        if actor == "orchestrator" or role == actor:
            return
        raise PolicyError(f"actor {actor!r} is not authorized to run {action} for role {role!r}")

    if action == "pane.capture":
        if actor == "orchestrator":
            return
        _authorize_role_resource(actor, resource, "role", policy.can_capture_panes, action)
        return

    if action == "role.state.change":
        if not policy.can_change_role_state:
            raise PolicyError(f"actor {actor!r} is not authorized to change role state")
        return

    if action == "codex.bind":
        if not policy.can_bind_app_server:
            raise PolicyError(f"actor {actor!r} is not authorized to bind Codex app-server roles")
        return

    if action == "stable.approve":
        if not policy.can_approve_stable:
            raise PolicyError(f"actor {actor!r} is not authorized to approve stable commits")
        return

    if action in ("team.sleep", "team.resume"):
        if not policy.can_sleep:
            verb = "resume" if action == "team.resume" else "sleep"
            raise PolicyError(f"actor {actor!r} is not authorized to {verb} the team")
        return


def _authorize_role_resource(
    actor: str,
    resource: Mapping[str, str],
    key: str,
    allowed_roles: tuple[str, ...],
    action: str,
) -> None:
    role = _required_resource(action, resource, key)
    if not _role_matches(role, actor, allowed_roles):
        raise PolicyError(f"actor {actor!r} is not authorized to run {action} for role {role!r}")


def _role_matches(value: str, actor: str, configured: tuple[str, ...]) -> bool:
    return value == actor or _matches(value, configured)


def _matches(value: str, configured: tuple[str, ...]) -> bool:
    return "*" in configured or value in configured


def _required_resource(action: str, resource: Mapping[str, str], key: str) -> str:
    value = resource.get(key)
    if value is None:
        raise PolicyError(f"missing policy resource {key!r} for {action}")
    return value


def _normalized_actor(actor: str | None) -> str | None:
    if actor is None:
        return None
    actor = actor.strip()
    return actor or None


def _boolean(raw_policy: Mapping[str, Any], key: str) -> bool:
    value = raw_policy.get(key, False)
    if isinstance(value, bool):
        return value
    raise ValueError(f"policy key {key!r} must be a boolean")


def _string_tuple(raw_policy: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw_policy.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, list):
        raise ValueError(f"policy key {key!r} must be a string or list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"policy key {key!r} must contain only strings")
        result.append(item)
    return tuple(result)
