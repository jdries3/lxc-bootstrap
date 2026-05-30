from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from unittest import mock
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


class UserPackageHydrationTests(unittest.TestCase):
    def test_missing_src_defaults_to_matching_apk_and_deb_package_names(self) -> None:
        package = MODULE.hydrate_user_package({"p": "git", "req": False})
        self.assertEqual(package.program, "git")
        self.assertFalse(package.required)
        self.assertEqual(package.alpine_package, "git")
        self.assertEqual(package.debian_package, "git")
        self.assertIsNone(package.alpine_mise_tool)
        self.assertIsNone(package.debian_mise_tool)

    def test_partial_debian_override_inherits_default_alpine_name(self) -> None:
        package = MODULE.hydrate_user_package({"p": "xz", "src": "deb:xz-utils"})
        self.assertEqual(package.alpine_package, "xz")
        self.assertEqual(package.debian_package, "xz-utils")

    def test_empty_debian_source_disables_that_distribution_only(self) -> None:
        package = MODULE.hydrate_user_package({"p": "ip6tables", "src": "deb:"})
        self.assertEqual(package.alpine_package, "ip6tables")
        self.assertIsNone(package.debian_package)

    def test_mise_source_is_exclusive(self) -> None:
        package = MODULE.hydrate_user_package(
            {"p": "bat", "src": "mise:aqua:sharkdp/bat"}
        )
        self.assertEqual(package.alpine_mise_tool, "aqua:sharkdp/bat")
        self.assertEqual(package.debian_mise_tool, "aqua:sharkdp/bat")
        self.assertIsNone(package.alpine_package)
        self.assertIsNone(package.debian_package)

    def test_distro_specific_mise_source_is_supported(self) -> None:
        package = MODULE.hydrate_user_package(
            {"p": "bat", "src": "deb:mise:aqua:sharkdp/bat"}
        )
        self.assertEqual(package.alpine_package, "bat")
        self.assertEqual(package.debian_package, None)
        self.assertIsNone(package.alpine_mise_tool)
        self.assertEqual(package.debian_mise_tool, "aqua:sharkdp/bat")

    def test_mixing_global_mise_and_repo_sources_is_rejected(self) -> None:
        with self.assertRaises(MODULE.BootstrapError):
            MODULE.hydrate_user_package(
                {"p": "bat", "src": "mise:aqua:sharkdp/bat,deb:bat"}
            )


class UserPackageCatalogParityTests(unittest.TestCase):
    def test_catalog_contains_expected_user_facing_tools_from_bootstraps(self) -> None:
        actual = {entry["p"] for entry in MODULE.USER_PACKAGES}
        expected = {
            "atuin",
            "bat",
            "btop",
            "curl",
            "dust",
            "eza",
            "fastfetch",
            "fzf",
            "git",
            "jq",
            "lazydocker",
            "nushell",
            "ripgrep",
            "starship",
            "trippy",
            "wget",
            "yq",
            "zellij",
        }
        self.assertTrue(expected.issubset(actual))


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

    def test_build_mise_config_contains_expected_tools_for_debian(self) -> None:
        config = MODULE.build_mise_config("debian")
        self.assertIn('"aqua:atuinsh/atuin" = "latest"', config)
        self.assertIn('"aqua:starship/starship" = "latest"', config)
        self.assertIn('"aqua:zellij-org/zellij" = "latest"', config)
        self.assertNotIn('"git" = "latest"', config)

    def test_build_mise_config_is_empty_on_alpine_for_repo_backed_tools(self) -> None:
        config = MODULE.build_mise_config("alpine")
        self.assertEqual(config.strip(), "[tools]")

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


class PackageManagerTests(unittest.TestCase):
    @mock.patch("subprocess.run")
    def test_package_exists_alpine_exact_match(self, mock_run: mock.MagicMock) -> None:
        from subprocess import CompletedProcess
        mock_run.return_value = CompletedProcess(
            args=["apk", "search", "-q", "-x", "bash"],
            returncode=0,
            stdout="bash\n",
            stderr="",
        )
        pm = MODULE.PackageManager(console=None, os_type="alpine")
        self.assertTrue(pm.package_exists("bash"))
        mock_run.assert_called_with(
            ["apk", "search", "-q", "-x", "bash"],
            capture_output=True,
            text=True,
            check=False,
        )

    @mock.patch("subprocess.run")
    def test_package_exists_alpine_version_suffix(self, mock_run: mock.MagicMock) -> None:
        from subprocess import CompletedProcess
        mock_run.return_value = CompletedProcess(
            args=["apk", "search", "-q", "-x", "bash"],
            returncode=0,
            stdout="bash-5.2.21-r0\n",
            stderr="",
        )
        pm = MODULE.PackageManager(console=None, os_type="alpine")
        self.assertTrue(pm.package_exists("bash"))

    @mock.patch("subprocess.run")
    def test_package_exists_alpine_no_match(self, mock_run: mock.MagicMock) -> None:
        from subprocess import CompletedProcess
        mock_run.return_value = CompletedProcess(
            args=["apk", "search", "-q", "-x", "nonexistent"],
            returncode=0,
            stdout="",
            stderr="",
        )
        pm = MODULE.PackageManager(console=None, os_type="alpine")
        self.assertFalse(pm.package_exists("nonexistent"))

    @mock.patch("subprocess.run")
    def test_get_apk_package_tag_standard_repo(self, mock_run: mock.MagicMock) -> None:
        from subprocess import CompletedProcess
        mock_run.return_value = CompletedProcess(
            args=["apk", "policy", "openssh"],
            returncode=0,
            stdout="openssh policy:\n  9.3_p1-r0: lib/apk/db/installed\n  9.3_p2-r0: https://dl-cdn.alpinelinux.org/alpine/v3.23/main\n  9.1_p1-r0: https://dl-cdn.alpinelinux.org/alpine/v3.22/main\n",
            stderr="",
        )
        pm = MODULE.PackageManager(console=None, os_type="alpine")
        self.assertIsNone(pm.get_apk_package_tag("openssh"))

    @mock.patch("subprocess.run")
    def test_get_apk_package_tag_testing_only(self, mock_run: mock.MagicMock) -> None:
        from subprocess import CompletedProcess
        # Simulate uninstalled package returning non-zero returncode from apk policy
        mock_run.return_value = CompletedProcess(
            args=["apk", "policy", "trippy"],
            returncode=1,
            stdout="trippy policy:\n  0.13.0-r0: @testing https://dl-cdn.alpinelinux.org/alpine/edge/testing\n",
            stderr="",
        )
        pm = MODULE.PackageManager(console=None, os_type="alpine")
        self.assertEqual(pm.get_apk_package_tag("trippy"), "@testing")

    @mock.patch("subprocess.run")
    def test_get_apk_package_tag_both(self, mock_run: mock.MagicMock) -> None:
        from subprocess import CompletedProcess
        mock_run.return_value = CompletedProcess(
            args=["apk", "policy", "somepackage"],
            returncode=0,
            stdout="somepackage policy:\n  1.0.0-r0: https://dl-cdn.alpinelinux.org/alpine/v3.23/community\n  1.1.0-r0: @testing https://dl-cdn.alpinelinux.org/alpine/edge/testing\n",
            stderr="",
        )
        pm = MODULE.PackageManager(console=None, os_type="alpine")
        self.assertIsNone(pm.get_apk_package_tag("somepackage"))

    @mock.patch("subprocess.run")
    def test_get_apk_package_tag_nonexistent(self, mock_run: mock.MagicMock) -> None:
        from subprocess import CompletedProcess
        mock_run.return_value = CompletedProcess(
            args=["apk", "policy", "nonexistent"],
            returncode=1,
            stdout="",
            stderr="ERROR: nonexistent: No such package\n",
        )
        pm = MODULE.PackageManager(console=None, os_type="alpine")
        self.assertIsNone(pm.get_apk_package_tag("nonexistent"))



if __name__ == "__main__":
    unittest.main()

