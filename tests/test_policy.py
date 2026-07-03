from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tmux_team.config import RoleConfig, TeamConfig, load_config
from tmux_team.policy import PolicyContext, PolicyError, RolePolicy, TeamPolicy, authorize


class PolicyTests(unittest.TestCase):
    def test_config_parses_team_and_role_policy_separately_from_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / ".tmux-team" / "team.toml"
            config_path.parent.mkdir()
            config_path.write_text(
                f"""[team]
name = "policy-test"
runtime_dir = "{root / "runtime"}"
policy_mode = "permissive"

[roles.orchestrator]
mode = "human_visible"
notify_method = "display-message"

[roles.orchestrator.policy]
can_send_to = ["implementer"]
can_notify = ["implementer"]
can_capture_panes = ["collector"]
can_use_send_keys = true
can_change_role_state = true
can_bind_app_server = true
can_approve_stable = true
can_sleep = true
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        role = config.roles["orchestrator"]
        self.assertEqual(config.policy.mode, "permissive")
        self.assertEqual(role.capabilities["notify_method"], "display-message")
        self.assertNotIn("policy", role.capabilities)
        self.assertEqual(role.policy.can_send_to, ("implementer",))
        self.assertEqual(role.policy.can_notify, ("implementer",))
        self.assertEqual(role.policy.can_capture_panes, ("collector",))
        self.assertTrue(role.policy.can_use_send_keys)
        self.assertTrue(role.policy.can_change_role_state)
        self.assertTrue(role.policy.can_bind_app_server)
        self.assertTrue(role.policy.can_approve_stable)
        self.assertTrue(role.policy.can_sleep)

    def test_no_actor_keeps_operator_cli_permissive(self) -> None:
        config = self.config()

        authorize(config, PolicyContext(), "message.send", sender="operator", recipient="implementer")
        authorize(config, PolicyContext(), "role.state.change", role="implementer")
        authorize(config, PolicyContext(), "stable.approve", role="global")

    def test_actor_can_only_send_as_itself_by_default(self) -> None:
        config = self.config()

        authorize(
            config,
            PolicyContext(actor="implementer"),
            "message.send",
            sender="implementer",
            recipient="orchestrator",
        )

        with self.assertRaises(PolicyError):
            authorize(
                config,
                PolicyContext(actor="implementer"),
                "message.send",
                sender="orchestrator",
                recipient="collector",
            )

    def test_actor_can_only_work_its_own_inbox_by_default(self) -> None:
        config = self.config()

        authorize(config, PolicyContext(actor="implementer"), "inbox.next", role="implementer")
        authorize(config, PolicyContext(actor="implementer"), "inbox.reclaimable", role="implementer")
        authorize(config, PolicyContext(actor="implementer"), "inbox.ack", role="implementer")
        authorize(config, PolicyContext(actor="implementer"), "inbox.complete", role="implementer")

        for action in ("inbox.next", "inbox.reclaimable", "inbox.ack", "inbox.complete"):
            with self.subTest(action=action), self.assertRaises(PolicyError):
                authorize(config, PolicyContext(actor="implementer"), action, role="orchestrator")

    def test_actor_can_only_notify_itself_by_default(self) -> None:
        config = self.config()

        authorize(
            config, PolicyContext(actor="implementer"), "role.notify", role="implementer", method="app-server-turn"
        )

        with self.assertRaisesRegex(PolicyError, "not authorized to run role.notify"):
            authorize(
                config,
                PolicyContext(actor="implementer"),
                "role.notify",
                role="orchestrator",
                method="app-server-turn",
            )

        with self.assertRaisesRegex(PolicyError, "send-keys"):
            authorize(config, PolicyContext(actor="implementer"), "role.notify", role="implementer", method="send-keys")

    def test_can_notify_does_not_imply_can_use_send_keys(self) -> None:
        config = self.config(RolePolicy(can_notify=("orchestrator",)))

        authorize(
            config, PolicyContext(actor="implementer"), "role.notify", role="orchestrator", method="app-server-turn"
        )
        with self.assertRaisesRegex(PolicyError, "send-keys"):
            authorize(
                config,
                PolicyContext(actor="implementer"),
                "role.notify",
                role="orchestrator",
                method="send-keys",
            )

        config = self.config(RolePolicy(can_notify=("orchestrator",), can_use_send_keys=True))
        authorize(config, PolicyContext(actor="implementer"), "role.notify", role="orchestrator", method="send-keys")

    def test_privileged_actions_require_role_policy(self) -> None:
        config = self.config(
            RolePolicy(
                can_change_role_state=True,
                can_bind_app_server=True,
                can_approve_stable=True,
                can_sleep=True,
            )
        )
        context = PolicyContext(actor="implementer")

        authorize(config, context, "role.state.change", role="orchestrator")
        authorize(config, context, "codex.bind", role="orchestrator")
        authorize(config, context, "stable.approve", role="global")
        authorize(config, context, "team.sleep")
        authorize(config, context, "team.resume")

    def test_milestones_are_operator_or_orchestrator_only(self) -> None:
        config = self.config()

        authorize(config, PolicyContext(), "milestone.add", role="collector")
        authorize(config, PolicyContext(actor="orchestrator"), "milestone.add", role="collector")

        with self.assertRaisesRegex(PolicyError, "send evidence to orchestrator"):
            authorize(config, PolicyContext(actor="collector"), "milestone.add", role="collector")

    def test_watch_management_is_self_or_orchestrator(self) -> None:
        config = self.config()

        authorize(config, PolicyContext(), "watch.start", role="collector")
        authorize(config, PolicyContext(actor="collector"), "watch.start", role="collector")
        authorize(config, PolicyContext(actor="collector"), "watch.update", role="collector")
        authorize(config, PolicyContext(actor="collector"), "watch.complete", role="collector")
        authorize(config, PolicyContext(actor="collector"), "watch.list", role="collector")
        authorize(config, PolicyContext(actor="orchestrator"), "watch.start", role="collector")
        authorize(config, PolicyContext(actor="orchestrator"), "watch.list", role="")

        with self.assertRaisesRegex(PolicyError, "not authorized"):
            authorize(config, PolicyContext(actor="implementer"), "watch.start", role="collector")

        with self.assertRaisesRegex(PolicyError, "not authorized"):
            authorize(config, PolicyContext(actor="implementer"), "watch.list", role="collector")

    def test_pane_capture_is_self_or_orchestrator_by_default(self) -> None:
        config = self.config()

        authorize(config, PolicyContext(), "pane.capture", role="collector")
        authorize(config, PolicyContext(actor="orchestrator"), "pane.capture", role="collector")
        authorize(config, PolicyContext(actor="implementer"), "pane.capture", role="implementer")

        with self.assertRaisesRegex(PolicyError, "not authorized to run pane.capture"):
            authorize(config, PolicyContext(actor="implementer"), "pane.capture", role="collector")

    def test_role_policy_can_allow_pane_capture(self) -> None:
        config = self.config(RolePolicy(can_capture_panes=("collector",)))

        authorize(config, PolicyContext(actor="implementer"), "pane.capture", role="collector")

    def test_permissive_policy_mode_is_breakglass(self) -> None:
        config = self.config()
        context = PolicyContext(actor="implementer", mode="permissive")

        authorize(config, context, "message.send", sender="orchestrator", recipient="collector")
        authorize(config, context, "inbox.next", role="orchestrator")
        authorize(config, context, "role.state.change", role="orchestrator")

    def config(self, implementer_policy: RolePolicy | None = None) -> TeamConfig:
        return TeamConfig(
            name="test",
            runtime_dir=Path("/tmp/tmux-team-policy-test"),
            roles={
                "orchestrator": RoleConfig(name="orchestrator"),
                "implementer": RoleConfig(name="implementer", policy=implementer_policy or RolePolicy()),
                "collector": RoleConfig(name="collector"),
            },
            policy=TeamPolicy(),
        )


if __name__ == "__main__":
    unittest.main()
