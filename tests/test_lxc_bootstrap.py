from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parent.parent / "lxc-bootstrap.py"
SPEC = importlib.util.spec_from_file_location("lxc_bootstrap", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load module from {MODULE_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ChooseSavedOrOverrideTests(unittest.TestCase):
    def test_default_is_used_when_no_saved_or_override(self) -> None:
        value = MODULE.choose_saved_or_override(
            kind="engine",
            saved=None,
            override=None,
            force=False,
            default="docker",
        )
        self.assertEqual(value, "docker")

    def test_override_requires_force_when_saved_differs(self) -> None:
        with self.assertRaises(MODULE.BootstrapError):
            MODULE.choose_saved_or_override(
                kind="runtime",
                saved="crun",
                override="youki",
                force=False,
                default="crun",
            )

    def test_override_wins_when_forced(self) -> None:
        value = MODULE.choose_saved_or_override(
            kind="runtime",
            saved="crun",
            override="youki",
            force=True,
            default="crun",
        )
        self.assertEqual(value, "youki")


class DockerPayloadTests(unittest.TestCase):
    def test_update_docker_daemon_payload_preserves_existing_fields(self) -> None:
        payload = MODULE.update_docker_daemon_payload(
            {"log-driver": "json-file", "runtimes": {"old": {"path": "/bin/old"}}},
            runtime_name="crun",
            runtime_path="/usr/bin/crun",
            storage_driver="overlay2",
        )
        self.assertEqual(payload["log-driver"], "json-file")
        self.assertEqual(payload["default-runtime"], "crun")
        self.assertEqual(payload["runtimes"]["crun"]["path"], "/usr/bin/crun")
        self.assertEqual(payload["runtimes"]["old"]["path"], "/bin/old")
        self.assertEqual(payload["storage-driver"], "overlay2")


class ConfigHelperTests(unittest.TestCase):
    def test_set_sshd_directive_replaces_commented_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "sshd_config"
            config_path.write_text("#PermitRootLogin yes\nPasswordAuthentication yes\n")

            MODULE.set_sshd_directive(config_path, "PermitRootLogin", "prohibit-password")
            MODULE.set_sshd_directive(config_path, "PasswordAuthentication", "no")
            contents = config_path.read_text()

            self.assertIn("PermitRootLogin prohibit-password\n", contents)
            self.assertIn("PasswordAuthentication no\n", contents)
            self.assertNotIn("#PermitRootLogin yes", contents)

    def test_build_mise_config_contains_expected_tools(self) -> None:
        config = MODULE.build_mise_config()
        self.assertIn('"aqua:atuinsh/atuin" = "latest"', config)
        self.assertIn('"aqua:starship/starship" = "latest"', config)
        self.assertIn('"aqua:zellij-org/zellij" = "latest"', config)

    def test_storage_backend_labels_match_expected_modes(self) -> None:
        self.assertEqual(MODULE.storage_backend_label_for_mode("docker", "privileged"), "overlay2")
        self.assertEqual(
            MODULE.storage_backend_label_for_mode("docker", "unprivileged"),
            "fuse-overlayfs",
        )
        self.assertEqual(
            MODULE.storage_backend_label_for_mode("podman", "privileged"),
            "overlay(native)",
        )
        self.assertEqual(
            MODULE.storage_backend_label_for_mode("podman", "unknown"),
            "overlay+fuse-overlayfs",
        )


if __name__ == "__main__":
    unittest.main()
