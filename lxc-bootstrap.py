# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "rich",
#   "tomlkit",
# ]
# ///

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import tomlkit
from rich.console import Console

PROJECT_NAME = "lxc-bootstrap"
PROJECT_VERSION = "0.1.7"
DEBUG_ENABLED = False
STATE_DIR = Path("/etc/lxc-bootstrap")
STATE_FILE = STATE_DIR / "state.toml"
GITHUB_USER = os.environ.get("LXC_BOOTSTRAP_GITHUB_USER", "jdries3")
RAW_SCRIPT_URL = (
    "https://raw.githubusercontent.com/jdries3/lxc-bootstrap/main/lxc-bootstrap.py"
)
MISE_CONFIG_DIR = Path("/root/.config/mise")
MISE_CONFIG_PATH = MISE_CONFIG_DIR / "config.toml"
MISE_HOME = Path("/root/.local/share/mise")
MISE_SHIMS_DIR = MISE_HOME / "shims"
LOCAL_BIN_DIR = Path("/root/.local/bin")
BASH_COMPLETION_DIR = Path("/root/.local/share/bash-completion/completions")
FISH_COMPLETION_DIR = Path("/root/.config/fish/completions")
YOUKI_INSTALL_PATH = Path("/usr/local/bin/youki")
YOUKI_RELEASE_URL = "https://api.github.com/repos/youki-dev/youki/releases/latest"

USER_PACKAGES = [
    {"p": "atuin", "src": "deb:mise:aqua:atuinsh/atuin"},
    {"p": "bat", "src": "deb:mise:aqua:sharkdp/bat"},
    {"p": "btop", "src": "deb:mise:aqua:aristocratos/btop"},
    {"p": "curl"},
    {"p": "dust", "src": "deb:mise:aqua:bootandy/dust"},
    {"p": "eza", "src": "deb:mise:aqua:eza-community/eza"},
    {"p": "fastfetch", "src": "deb:mise:aqua:fastfetch-cli/fastfetch"},
    {"p": "fzf", "src": "deb:mise:aqua:junegunn/fzf"},
    {"p": "git", "req": False},
    {"p": "jq", "src": "deb:mise:aqua:jqlang/jq"},
    {"p": "lazydocker", "src": "deb:mise:aqua:jesseduffield/lazydocker"},
    {"p": "nushell", "src": "deb:mise:aqua:nushell/nushell"},
    {"p": "ripgrep", "src": "deb:mise:aqua:BurntSushi/ripgrep"},
    {"p": "starship", "src": "deb:mise:aqua:starship/starship"},
    {"p": "trippy", "src": "deb:mise:aqua:fujiapple852/trippy"},
    {"p": "wget"},
    {"p": "yq", "src": "apk:yq-go,deb:mise:aqua:mikefarah/yq"},
    {"p": "zellij", "src": "deb:mise:aqua:zellij-org/zellij"},
]

COMPLETION_GENERATORS: dict[str, dict[str, list[str]]] = {
    "mise": {
        "bash": ["mise", "completion", "bash", "--include-bash-completion-lib"],
        "fish": ["mise", "completion", "fish"],
    },
    "atuin": {
        "bash": ["atuin", "gen-completions", "--shell", "bash"],
        "fish": ["atuin", "gen-completions", "--shell", "fish"],
    },
    "starship": {
        "bash": ["starship", "completions", "bash"],
        "fish": ["starship", "completions", "fish"],
    },
    "yq": {
        "bash": ["yq", "completion", "bash"],
        "fish": ["yq", "completion", "fish"],
    },
}


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageSpec:
    candidates: tuple[str, ...]
    required: bool = True


@dataclass
class Options:
    engine: str | None
    runtime: str | None
    disable_testing_repos: bool
    run_setup_apkrepos: bool
    skip_ssh_keys: bool
    force_engine_switch: bool
    force_runtime_switch: bool
    force_storage_backend_switch: bool
    install_bash: bool
    install_fish: bool
    no_color: bool
    debug: bool


@dataclass
class Context:
    options: Options
    console: Console
    os_type: str
    environment: str
    privilege_mode: str
    engine: str
    runtime: str
    state: dict[str, Any]
    github_user: str


@dataclass(frozen=True)
class UserPackage:
    program: str
    required: bool = True
    alpine_package: str | None = None
    debian_package: str | None = None
    alpine_mise_tool: str | None = None
    debian_mise_tool: str | None = None


class PackageManager:
    def __init__(self, console: Console, os_type: str, debug: bool = False):
        self.console = console
        self.os_type = os_type
        self.debug_enabled = debug

    def update(self) -> None:
        if self.os_type == "alpine":
            run_command(self.console, ["apk", "update"])
        else:
            run_command(self.console, ["apt-get", "update"])

    def upgrade(self) -> None:
        if self.os_type == "alpine":
            run_command(self.console, ["apk", "upgrade", "-q"])
        else:
            env = os.environ.copy()
            env["DEBIAN_FRONTEND"] = "noninteractive"
            run_command(self.console, ["apt-get", "upgrade", "-y"], env=env)

    def install(self, packages: Sequence[str]) -> None:
        if not packages:
            return
        if self.os_type == "alpine":
            run_command(self.console, ["apk", "add", "--no-cache", "-q", *packages])
        else:
            env = os.environ.copy()
            env["DEBIAN_FRONTEND"] = "noninteractive"
            run_command(self.console, ["apt-get", "install", "-y", *packages], env=env)

    def package_exists(self, package: str) -> bool:
        if self.os_type == "alpine":
            result = subprocess.run(
                ["apk", "search", "-q", "-x", package],
                capture_output=True,
                text=True,
                check=False,
            )
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if any(line == package for line in lines):
                return True
            pattern = re.compile(r"^" + re.escape(package) + r"-\d")
            return any(pattern.match(line) for line in lines)


        result = subprocess.run(
            ["apt-cache", "show", package],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())

    def get_apk_package_tag(self, package: str) -> str | None:
        if self.os_type != "alpine":
            return None
        result = subprocess.run(
            ["apk", "policy", package],
            capture_output=True,
            text=True,
            check=False,
        )
        debug(self.console, f"apk policy {package} output:\n{result.stdout}")

        available_in_standard = False
        available_in_testing = False

        for line in result.stdout.splitlines():
            line_stripped = line.strip()
            if not line_stripped:
                continue
            if line_stripped.endswith("policy:"):
                continue
            if line_stripped.endswith(":"):
                continue

            if ": " in line_stripped:
                _, right = line_stripped.split(":", 1)
                tokens = right.split()
            else:
                tokens = line_stripped.split()

            if not tokens:
                continue

            if "@testing" in tokens:
                available_in_testing = True

            has_repo_url = any(t.startswith("http://") or t.startswith("https://") for t in tokens)
            has_any_tag = any(t.startswith("@") for t in tokens)

            if has_repo_url and not has_any_tag:
                available_in_standard = True

        if not available_in_standard and available_in_testing:
            debug(self.console, f"{package} resolved to @testing")
            return "@testing"
        debug(self.console, f"{package} resolution: available_in_standard={available_in_standard}, available_in_testing={available_in_testing}")
        return None


class ServiceManager:
    def __init__(self, console: Console, os_type: str):
        self.console = console
        self.os_type = os_type

    def _systemd_available(self) -> bool:
        return shutil.which("systemctl") is not None and Path("/run/systemd/system").exists()

    def is_available(self, service: str) -> bool:
        if self.os_type == "alpine":
            result = subprocess.run(
                ["rc-service", "--list"], capture_output=True, text=True, check=False
            )
            return any(line.split()[:1] == [service] for line in result.stdout.splitlines())

        if not self._systemd_available():
            return False
        result = subprocess.run(
            ["systemctl", "list-unit-files"],
            capture_output=True,
            text=True,
            check=False,
        )
        candidates = {service}
        if "." not in service:
            candidates.add(f"{service}.service")
            candidates.add(f"{service}.socket")
        return any(candidate in result.stdout for candidate in candidates)

    def enable_and_start_any(self, candidates: Sequence[str], display_name: str) -> bool:
        for candidate in candidates:
            if not self.is_available(candidate):
                continue
            if self.os_type == "alpine":
                subprocess.run(
                    ["rc-update", "add", candidate, "default"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                result = subprocess.run(
                    ["rc-service", candidate, "start"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode != 0:
                    warn(
                        self.console,
                        f"Failed to start {display_name} service '{candidate}'; continuing",
                    )
                else:
                    info(self.console, f"Enabled and started {display_name} via {candidate}")
                return True

            if not self._systemd_available():
                warn(
                    self.console,
                    f"systemd is not active; skipping {display_name} service management",
                )
                return False
            subprocess.run(
                ["systemctl", "enable", candidate],
                capture_output=True,
                text=True,
                check=False,
            )
            result = subprocess.run(
                ["systemctl", "start", candidate],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                warn(
                    self.console,
                    f"Failed to start {display_name} unit '{candidate}'; continuing",
                )
            else:
                info(self.console, f"Enabled and started {display_name} via {candidate}")
            return True

        warn(self.console, f"No available service unit found for {display_name}")
        return False

    def restart_any(self, candidates: Sequence[str], display_name: str) -> bool:
        for candidate in candidates:
            if not self.is_available(candidate):
                continue
            if self.os_type == "alpine":
                subprocess.run(
                    ["rc-service", candidate, "restart"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return True
            if self._systemd_available():
                subprocess.run(
                    ["systemctl", "restart", candidate],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return True
        return False


def info(console: Console, message: str) -> None:
    console.print(f"[cyan][{PROJECT_NAME}][/cyan] {message}")


def success(console: Console, message: str) -> None:
    console.print(f"[green][{PROJECT_NAME}] SUCCESS:[/green] {message}")


def warn(console: Console, message: str) -> None:
    console.print(f"[yellow][{PROJECT_NAME}] WARNING:[/yellow] {message}")


def fail(console: Console, message: str) -> None:
    console.print(f"[red][{PROJECT_NAME}] ERROR:[/red] {message}")


def debug(console: Console, message: str) -> None:
    if DEBUG_ENABLED and console:
        console.print(f"[grey50][{PROJECT_NAME}] DEBUG:[/grey50] {message}")


def run_command(
    console: Console,
    command: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    if env:
        debug(console, f"Environment overrides: {env}")
    info(console, f"Running: {' '.join(command)}")
    result = subprocess.run(command, text=True, capture_output=True, env=env, check=False)
    debug(console, f"Command exit code: {result.returncode}")
    if result.stdout.strip():
        console.print(result.stdout.rstrip())
    if result.returncode != 0 and result.stderr.strip():
        console.print(result.stderr.rstrip(), style="yellow")
    if check and result.returncode != 0:
        raise BootstrapError(
            f"Command failed with exit code {result.returncode}: {' '.join(command)}"
        )
    return result


def backup_once(path: Path) -> None:
    backup = path.with_suffix(path.suffix + ".orig")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)


def load_state(console: Console | None = None) -> dict[str, Any]:
    if not STATE_FILE.exists():
        if console:
            debug(console, f"State file {STATE_FILE} does not exist")
        return {}
    try:
        content = STATE_FILE.read_text()
        state = tomlkit.parse(content).unwrap()
        if console:
            debug(console, f"Loaded state from {STATE_FILE}: {state}")
        return state
    except Exception as e:
        if console:
            debug(console, f"Failed to parse state file {STATE_FILE}: {e}")
        return {}


def save_state(ctx: Context) -> None:
    debug(ctx.console, f"Saving state to {STATE_FILE}...")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc["bootstrap_engine"] = ctx.engine
    doc["bootstrap_runtime"] = ctx.runtime
    doc["bootstrap_install_bash"] = ctx.options.install_bash
    doc["bootstrap_install_fish"] = ctx.options.install_fish
    doc["bootstrap_lxc_privilege_mode"] = ctx.privilege_mode
    doc["bootstrap_runtime_environment"] = ctx.environment
    doc["bootstrap_version"] = PROJECT_VERSION
    doc["bootstrap_last_run"] = subprocess.run(
        ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True, check=False
    ).stdout.strip()
    STATE_FILE.write_text(tomlkit.dumps(doc))
    STATE_FILE.chmod(0o644)
    debug(ctx.console, "State saved successfully")


def choose_saved_or_override(
    *,
    kind: str,
    saved: str | None,
    override: str | None,
    force: bool,
    default: str,
) -> str:
    if override:
        if saved and saved != override and not force:
            raise BootstrapError(
                f"{kind} mismatch: saved={saved}, requested={override}. "
                f"Use --force-{kind}-switch to override."
            )
        return override
    if saved:
        return saved
    return default


def detect_os(console: Console | None = None) -> str:
    if console:
        debug(console, "Detecting OS...")
    if Path("/etc/alpine-release").exists():
        if console:
            debug(console, f"Found /etc/alpine-release: {Path('/etc/alpine-release').read_text().strip()}")
        return "alpine"
    if Path("/etc/debian_version").exists():
        if console:
            debug(console, f"Found /etc/debian_version: {Path('/etc/debian_version').read_text().strip()}")
        return "debian"
    raise BootstrapError("Unsupported OS. Expected Alpine or Debian.")


def detect_environment(console: Console) -> str:
    debug(console, "Detecting guest environment...")
    try:
        systemd_container = Path("/run/systemd/container").read_text().strip()
        debug(console, f"Checked /run/systemd/container: {systemd_container}")
        if systemd_container in {"lxc", "lxc-libvirt"}:
            return "lxc"
    except FileNotFoundError:
        pass

    try:
        environ = Path("/proc/1/environ").read_bytes().split(b"\0")
        container_entry = [e.decode("utf-8", errors="replace") for e in environ if e.startswith(b"container=")]
        debug(console, f"Checked /proc/1/environ container entry: {container_entry}")
        if b"container=lxc" in environ or b"container=lxc-libvirt" in environ:
            return "lxc"
    except (FileNotFoundError, PermissionError):
        pass

    try:
        mountinfo = Path("/proc/1/mountinfo").read_text()
        lxcfs_present = "lxcfs" in mountinfo or "/dev/.lxc/" in mountinfo
        debug(console, f"Checked /proc/1/mountinfo for lxcfs/lxc: {lxcfs_present}")
        if lxcfs_present:
            return "lxc"
    except (FileNotFoundError, PermissionError):
        pass

    try:
        product_name = Path("/sys/class/dmi/id/product_name").read_text().strip()
        debug(console, f"Checked DMI product_name: {product_name}")
        for marker in ("KVM", "QEMU", "VirtualBox", "VMware", "Virtual Machine", "Bochs", "Q35"):
            if marker in product_name:
                return "vm"
    except (FileNotFoundError, PermissionError):
        pass

    try:
        cpuinfo = Path("/proc/cpuinfo").read_text()
        has_hypervisor = "hypervisor" in cpuinfo
        debug(console, f"Checked /proc/cpuinfo for hypervisor flag: {has_hypervisor}")
        if has_hypervisor:
            return "vm"
    except (FileNotFoundError, PermissionError):
        pass

    warn(console, "Could not determine whether this host is an LXC or a VM")
    return "unknown"


def detect_container_privilege_mode(console: Console) -> str:
    debug(console, "Detecting container privilege mode...")
    try:
        uid_map = Path("/proc/self/uid_map").read_text().strip()
        debug(console, f"Checked /proc/self/uid_map:\n{uid_map}")
        lines = uid_map.splitlines()
    except (FileNotFoundError, PermissionError) as e:
        debug(console, f"Failed to read /proc/self/uid_map: {e}")
        warn(console, "Could not determine UID mapping; defaulting to unprivileged-safe settings")
        return "unknown"

    if not lines:
        debug(console, "uid_map has no lines")
        return "unknown"
    parts = lines[0].split()
    if len(parts) < 2 or parts[0] != "0":
        debug(console, f"First line of uid_map does not split to expected format: {parts}")
        return "unknown"
    mode = "privileged" if parts[1] == "0" else "unprivileged"
    debug(console, f"Determined mode from parts: {mode}")
    return mode


def storage_backend_label_for_mode(engine: str, mode: str) -> str:
    if engine == "docker" and mode == "privileged":
        return "overlay2"
    if engine == "docker":
        return "fuse-overlayfs"
    if engine == "podman" and mode == "privileged":
        return "overlay(native)"
    if engine == "podman":
        return "overlay+fuse-overlayfs"
    return "unknown"


def engine_storage_path(engine: str) -> Path | None:
    if engine == "docker":
        return Path("/var/lib/docker")
    if engine == "podman":
        return Path("/var/lib/containers/storage")
    return None


def guard_storage_backend_transition(ctx: Context) -> None:
    previous_mode = ctx.state.get("bootstrap_lxc_privilege_mode")
    if not previous_mode:
        return
    previous_backend = storage_backend_label_for_mode(ctx.engine, previous_mode)
    current_backend = storage_backend_label_for_mode(ctx.engine, ctx.privilege_mode)
    if previous_backend == current_backend:
        return

    storage_path = engine_storage_path(ctx.engine)
    has_existing_state = bool(storage_path and storage_path.exists() and any(storage_path.iterdir()))
    if not has_existing_state:
        warn(
            ctx.console,
            f"UID mapping changed ({previous_mode} -> {ctx.privilege_mode}); switching "
            f"{ctx.engine} storage backend ({previous_backend} -> {current_backend}) because "
            "no existing engine state was found",
        )
        return

    if ctx.options.force_storage_backend_switch:
        warn(
            ctx.console,
            f"Forcing {ctx.engine} storage backend change ({previous_backend} -> {current_backend})",
        )
        return

    raise BootstrapError(
        f"Refusing to switch {ctx.engine} storage backend ({previous_backend} -> {current_backend}) "
        f"because existing engine state was found after UID mapping mode changed. "
        "Reset engine storage first or re-run with --force-storage-backend-switch."
    )


def configure_repositories(ctx: Context) -> None:
    if ctx.os_type != "alpine":
        if ctx.options.disable_testing_repos:
            info(ctx.console, "Debian does not use Alpine testing repositories; skipping that flag")
        return

    repo_file = Path("/etc/apk/repositories")
    alpine_version = "v3.23"
    try:
        os_release = Path("/etc/os-release").read_text().splitlines()
        for line in os_release:
            if line.startswith("VERSION_ID="):
                alpine_version = f"v{line.split('=', 1)[1].strip().strip('"').split('.', 2)[0]}.{line.split('=', 1)[1].strip().strip('"').split('.', 2)[1]}"
                break
    except Exception:
        pass

    if ctx.options.run_setup_apkrepos and shutil.which("setup-apkrepos"):
        stdin = open("/dev/tty") if Path("/dev/tty").exists() and sys.stdin.isatty() else subprocess.DEVNULL
        subprocess.run(["setup-apkrepos", "-cf"], stdin=stdin, check=False)
        if stdin not in {subprocess.DEVNULL}:
            stdin.close()

    lines = repo_file.read_text().splitlines() if repo_file.exists() else []
    required = [
        f"https://dl-cdn.alpinelinux.org/alpine/{alpine_version}/main",
        f"https://dl-cdn.alpinelinux.org/alpine/{alpine_version}/community",
    ]
    normalized = [line.replace("http://", "https://") for line in lines if line.strip()]

    for required_line in required:
        uncommented = False
        for index, line in enumerate(normalized):
            if line.lstrip("# ") == required_line:
                normalized[index] = required_line
                uncommented = True
                break
        if not uncommented:
            normalized.append(required_line)

    normalized = [line for line in normalized if "@testing" not in line]
    if not ctx.options.disable_testing_repos:
        normalized.append("@testing https://dl-cdn.alpinelinux.org/alpine/edge/testing")

    backup_once(repo_file)
    repo_file.write_text("\n".join(normalized) + "\n")
    success(ctx.console, "Repository configuration complete")


def prepare_system(ctx: Context, package_manager: PackageManager) -> None:
    info(ctx.console, "Preparing system")
    if ctx.os_type == "alpine":
        rc_conf = Path("/etc/rc.conf")
        existing = rc_conf.read_text().splitlines() if rc_conf.exists() else []
        updated = []
        saw = False
        for line in existing:
            if line.startswith("rc_cgroup_mode="):
                updated.append('rc_cgroup_mode="unified"')
                saw = True
            else:
                updated.append(line)
        if not saw:
            updated.append('rc_cgroup_mode="unified"')
        rc_conf.write_text("\n".join(updated) + "\n")

    Path("/lib/modules").mkdir(parents=True, exist_ok=True)
    kernel = subprocess.run(["uname", "-r"], capture_output=True, text=True, check=False).stdout.strip()
    if kernel:
        modules_dir = Path("/lib/modules") / kernel
        modules_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("modules.dep", "modules.alias", "modules.symbols"):
            target = modules_dir / filename
            target.touch(exist_ok=True)

    package_manager.update()
    package_manager.upgrade()
    success(ctx.console, "System preparation complete")


def spec(*candidates: str, required: bool = True) -> PackageSpec:
    return PackageSpec(candidates=tuple(candidates), required=required)


def hydrate_user_package(definition: dict[str, Any]) -> UserPackage:
    program = definition.get("p")
    if not isinstance(program, str) or not program.strip():
        raise BootstrapError("Each user package entry must include a non-empty 'p' value")
    program = program.strip()

    required = definition.get("req", True)
    if not isinstance(required, bool):
        raise BootstrapError(f"User package '{program}' has a non-boolean 'req' value")

    src = definition.get("src")
    if src is None:
        return UserPackage(
            program=program,
            required=required,
            alpine_package=program,
            debian_package=program,
        )
    if not isinstance(src, str) or not src.strip():
        raise BootstrapError(f"User package '{program}' has an invalid 'src' value")

    tokens = [token.strip() for token in src.split(",") if token.strip()]
    if not tokens:
        raise BootstrapError(f"User package '{program}' has an empty 'src' value")

    alpine_package = program
    debian_package = program
    alpine_mise_tool: str | None = None
    debian_mise_tool: str | None = None
    seen_kinds: set[str] = set()

    for token in tokens:
        if ":" not in token:
            raise BootstrapError(
                f"User package '{program}' has an invalid source token '{token}'"
            )

        kind, value = token.split(":", 1)
        kind = kind.strip()
        value = value.strip()

        if kind == "mise":
            if len(tokens) != 1:
                raise BootstrapError(
                    f"User package '{program}' cannot mix global mise sources with distro sources"
                )
            if not value:
                raise BootstrapError(f"User package '{program}' has an empty mise source")
            return UserPackage(
                program=program,
                required=required,
                alpine_package=None,
                debian_package=None,
                alpine_mise_tool=value,
                debian_mise_tool=value,
            )

        if kind not in {"apk", "deb"}:
            raise BootstrapError(
                f"User package '{program}' has an unsupported source kind '{kind}'"
            )
        if kind in seen_kinds:
            raise BootstrapError(
                f"User package '{program}' defines '{kind}' more than once"
            )
        seen_kinds.add(kind)

        repo_value = value or None
        mise_value: str | None = None
        if value.startswith("mise:"):
            mise_value = value.split(":", 1)[1].strip()
            if not mise_value:
                raise BootstrapError(
                    f"User package '{program}' has an empty distro-specific mise source"
                )
            repo_value = None

        if kind == "apk":
            alpine_package = repo_value
            alpine_mise_tool = mise_value
        else:
            debian_package = repo_value
            debian_mise_tool = mise_value

    return UserPackage(
        program=program,
        required=required,
        alpine_package=alpine_package,
        debian_package=debian_package,
        alpine_mise_tool=alpine_mise_tool,
        debian_mise_tool=debian_mise_tool,
    )


def hydrated_user_packages() -> list[UserPackage]:
    return [hydrate_user_package(definition) for definition in USER_PACKAGES]


def repo_package_for_os(package: UserPackage, os_type: str) -> str | None:
    return package.alpine_package if os_type == "alpine" else package.debian_package


def mise_tool_for_os(package: UserPackage, os_type: str) -> str | None:
    return package.alpine_mise_tool if os_type == "alpine" else package.debian_mise_tool


def repo_user_package_specs(ctx: Context, packages: Sequence[UserPackage]) -> list[PackageSpec]:
    specs: list[PackageSpec] = []
    for package in packages:
        if mise_tool_for_os(package, ctx.os_type):
            continue
        repo_package = repo_package_for_os(package, ctx.os_type)
        if repo_package is None:
            continue
        specs.append(spec(repo_package, required=package.required))
    return specs


def mise_user_packages(os_type: str, packages: Sequence[UserPackage]) -> list[UserPackage]:
    return [package for package in packages if mise_tool_for_os(package, os_type)]


def base_package_specs(ctx: Context) -> list[PackageSpec]:
    if ctx.os_type == "alpine":
        specs = [
            spec("bash"),
            spec("ca-certificates"),
            spec("dbus"),
            spec("fuse-overlayfs"),
            spec("ip6tables", required=False),
            spec("iptables"),
            spec("openssh"),
            spec("slirp4netns", required=False),
            spec("tar"),
            spec("xz"),
        ]
    else:
        specs = [
            spec("bash"),
            spec("ca-certificates"),
            spec("dbus"),
            spec("fuse-overlayfs"),
            spec("iptables"),
            spec("openssh-server"),
            spec("slirp4netns", required=False),
            spec("tar"),
            spec("uidmap", required=False),
            spec("xz-utils"),
        ]

    if ctx.environment != "lxc":
        specs.append(spec("qemu-guest-agent", required=False))
    if ctx.options.install_fish:
        specs.append(spec("fish", required=False))
    if ctx.options.install_bash:
        specs.append(spec("bash-completion", required=False))
    return specs


def engine_package_specs(ctx: Context) -> list[PackageSpec]:
    if ctx.engine == "docker":
        if ctx.os_type == "alpine":
            return [
                spec("containerd", required=False),
                spec("docker"),
                spec("docker-cli", required=False),
                spec("docker-cli-compose", required=False),
            ]
        return [
            spec("containerd", required=False),
            spec("docker.io"),
            spec("docker-compose-v2", "docker-compose-plugin", "docker-compose", required=False),
        ]

    if ctx.os_type == "alpine":
        return [
            spec("podman"),
            spec("podman-compose", required=False),
            spec("podman-openrc", required=False),
        ]
    return [
        spec("podman"),
        spec("podman-compose", required=False),
    ]


def runtime_package_specs(ctx: Context) -> list[PackageSpec]:
    if ctx.runtime == "runc":
        return [spec("runc")]
    if ctx.runtime == "crun":
        return [spec("crun")]
    return []


def resolve_packages(
    console: Console, package_manager: PackageManager, specs: Sequence[PackageSpec]
) -> list[str]:
    resolved: list[str] = []
    for package_spec in specs:
        selected = next(
            (candidate for candidate in package_spec.candidates if package_manager.package_exists(candidate)),
            None,
        )
        if selected:
            if package_manager.os_type == "alpine":
                tag = package_manager.get_apk_package_tag(selected)
                if tag:
                    selected = f"{selected}{tag}"
            resolved.append(selected)
            continue
        if package_spec.required:
            raise BootstrapError(
                f"Required package not found in repositories: {', '.join(package_spec.candidates)}"
            )
        warn(console, f"Skipping unavailable optional package(s): {', '.join(package_spec.candidates)}")
    return resolved


def install_base_packages(ctx: Context, package_manager: PackageManager) -> None:
    info(ctx.console, "Installing base packages")
    packages = resolve_packages(ctx.console, package_manager, base_package_specs(ctx))
    package_manager.install(packages)
    success(ctx.console, "Base package installation complete")


def install_engine_packages(ctx: Context, package_manager: PackageManager) -> None:
    info(ctx.console, f"Installing {ctx.engine} packages")
    packages = resolve_packages(ctx.console, package_manager, engine_package_specs(ctx))
    package_manager.install(packages)
    success(ctx.console, f"{ctx.engine.capitalize()} package installation complete")


def install_runtime(ctx: Context, package_manager: PackageManager) -> None:
    if ctx.runtime == "youki":
        install_youki(ctx)
        return

    packages = resolve_packages(ctx.console, package_manager, runtime_package_specs(ctx))
    package_manager.install(packages)
    success(ctx.console, f"Installed runtime packages for {ctx.runtime}")


def install_mise(ctx: Context, package_manager: PackageManager) -> None:
    if shutil.which("mise") or (LOCAL_BIN_DIR / "mise").exists() or Path("/usr/bin/mise").exists():
        info(ctx.console, "mise is already installed")
        return

    info(ctx.console, "Installing mise")
    if ctx.os_type == "alpine" and package_manager.package_exists("mise"):
        package_manager.install(["mise"])
        success(ctx.console, "Installed mise from Alpine packages")
        return

    with urllib.request.urlopen(
        urllib.request.Request("https://mise.run", headers={"User-Agent": PROJECT_NAME})
    ) as response:
        script = response.read().decode("utf-8")
    with tempfile.NamedTemporaryFile("w", delete=False) as handle:
        handle.write(script)
        installer_path = Path(handle.name)
    installer_path.chmod(0o755)
    try:
        run_command(ctx.console, ["sh", str(installer_path)])
    finally:
        installer_path.unlink(missing_ok=True)
    success(ctx.console, "Installed mise")


def install_repo_user_packages(
    ctx: Context, package_manager: PackageManager, packages: Sequence[UserPackage]
) -> None:
    specs = repo_user_package_specs(ctx, packages)
    if not specs:
        info(ctx.console, "No repo-backed user packages selected for installation")
        return

    info(ctx.console, "Installing repo-backed user packages")
    resolved_packages = resolve_packages(ctx.console, package_manager, specs)
    package_manager.install(resolved_packages)
    success(ctx.console, "Repo-backed user package installation complete")


def mise_binary() -> str:
    resolved = shutil.which("mise")
    if resolved:
        return resolved
    for candidate in (LOCAL_BIN_DIR / "mise", Path("/usr/bin/mise")):
        if candidate.exists():
            return str(candidate)
    raise BootstrapError("mise is not installed")


def build_mise_config(os_type: str, packages: Sequence[UserPackage] | None = None) -> str:
    source_packages = hydrated_user_packages() if packages is None else list(packages)
    selected_packages = mise_user_packages(os_type, source_packages)
    lines = ["[tools]"]
    lines.extend(
        f'"{mise_tool_for_os(package, os_type)}" = "latest"'
        for package in selected_packages
        if mise_tool_for_os(package, os_type)
    )
    return "\n".join(lines) + "\n"


def configure_mise(
    ctx: Context, package_manager: PackageManager, packages: Sequence[UserPackage]
) -> None:
    selected_packages = mise_user_packages(ctx.os_type, packages)
    if not selected_packages:
        info(ctx.console, "No mise-backed user packages selected for installation")
        return

    info(ctx.console, "Configuring mise-backed user packages")
    install_mise(ctx, package_manager)
    MISE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    MISE_CONFIG_PATH.write_text(build_mise_config(ctx.os_type, selected_packages))
    MISE_CONFIG_PATH.chmod(0o644)
    env = os.environ.copy()
    env["PATH"] = f"{LOCAL_BIN_DIR}:{env.get('PATH', '')}"
    run_command(ctx.console, [mise_binary(), "install"], env=env)
    success(ctx.console, "mise-backed user package installation complete")


def install_user_packages(ctx: Context, package_manager: PackageManager) -> list[UserPackage]:
    packages = hydrated_user_packages()
    install_repo_user_packages(ctx, package_manager, packages)
    configure_mise(ctx, package_manager, packages)
    return packages


def completion_command_for(tool: str, shell: str) -> list[str] | None:
    return COMPLETION_GENERATORS.get(tool, {}).get(shell)


def write_completion_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o644)


def generate_completion_script(command: Sequence[str]) -> str | None:
    env = os.environ.copy()
    env["PATH"] = f"{LOCAL_BIN_DIR}:{MISE_SHIMS_DIR}:{env.get('PATH', '')}"
    result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout


def install_generated_completions(ctx: Context, packages: Sequence[UserPackage]) -> None:
    if ctx.options.install_bash:
        BASH_COMPLETION_DIR.mkdir(parents=True, exist_ok=True)
    if ctx.options.install_fish:
        FISH_COMPLETION_DIR.mkdir(parents=True, exist_ok=True)

    completion_tools = set()
    for package in packages:
        if repo_package_for_os(package, ctx.os_type) or mise_tool_for_os(package, ctx.os_type):
            completion_tools.add(package.program)
    if shutil.which("mise"):
        completion_tools.add("mise")

    for tool in sorted(completion_tools):
        if ctx.options.install_bash:
            bash_command = completion_command_for(tool, "bash")
            if bash_command:
                content = generate_completion_script(bash_command)
                if content:
                    write_completion_file(BASH_COMPLETION_DIR / tool, content)
                else:
                    warn(ctx.console, f"Could not generate bash completions for {tool}")

        if ctx.options.install_fish:
            fish_command = completion_command_for(tool, "fish")
            if fish_command:
                content = generate_completion_script(fish_command)
                if content:
                    write_completion_file(FISH_COMPLETION_DIR / f"{tool}.fish", content)
                else:
                    warn(ctx.console, f"Could not generate fish completions for {tool}")


def discover_alpine_completion_packages(
    package_manager: PackageManager, repo_packages: Sequence[str], shell: str
) -> list[str]:
    discovered: list[str] = []
    for package in repo_packages:
        candidate = f"{package}-{shell}-completion"
        if package_manager.package_exists(candidate):
            discovered.append(candidate)
    return sorted(set(discovered))


def install_repo_completion_packages(
    ctx: Context, package_manager: PackageManager, packages: Sequence[UserPackage]
) -> None:
    if ctx.os_type != "alpine":
        return

    repo_packages = [
        repo_package
        for package in packages
        if not mise_tool_for_os(package, ctx.os_type)
        for repo_package in [repo_package_for_os(package, ctx.os_type)]
        if repo_package
    ]

    completion_packages: list[str] = []
    if ctx.options.install_bash:
        completion_packages.extend(
            discover_alpine_completion_packages(package_manager, repo_packages, "bash")
        )
    if ctx.options.install_fish:
        completion_packages.extend(
            discover_alpine_completion_packages(package_manager, repo_packages, "fish")
        )

    if completion_packages:
        package_manager.install(sorted(set(completion_packages)))


def configure_completions(
    ctx: Context, package_manager: PackageManager, packages: Sequence[UserPackage]
) -> None:
    info(ctx.console, "Configuring shell completions")
    install_repo_completion_packages(ctx, package_manager, packages)
    install_generated_completions(ctx, packages)
    success(ctx.console, "Shell completions configured")


def parse_youki_version(output: str) -> str | None:
    match = re.search(r"(\d+\.\d+\.\d+)", output)
    return match.group(1) if match else None


def current_youki_version() -> str | None:
    if not YOUKI_INSTALL_PATH.exists() and not shutil.which("youki"):
        return None
    result = subprocess.run([str(shutil.which("youki") or YOUKI_INSTALL_PATH), "--version"], capture_output=True, text=True, check=False)
    return parse_youki_version(result.stdout or result.stderr)


def youki_target_triplet(os_type: str) -> tuple[str, str]:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        arch = "aarch64"
    elif machine in {"x86_64", "amd64"}:
        arch = "x86_64"
    else:
        raise BootstrapError(f"Unsupported architecture for youki: {machine}")
    libc = "musl" if os_type == "alpine" else "gnu"
    return arch, libc


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": PROJECT_NAME})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


def download_to_path(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": PROJECT_NAME})
    with urllib.request.urlopen(request) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def install_youki(ctx: Context) -> None:
    info(ctx.console, "Installing youki from GitHub releases")
    release = fetch_json(YOUKI_RELEASE_URL)
    target_version = str(release["tag_name"]).lstrip("v")
    installed_version = current_youki_version()
    if installed_version == target_version:
        info(ctx.console, f"youki {installed_version} is already installed")
        return

    arch, libc = youki_target_triplet(ctx.os_type)
    expected_name_suffix = f"{arch}-{libc}.tar.gz"
    asset = next(
        (
            item
            for item in release.get("assets", [])
            if str(item.get("name", "")).endswith(expected_name_suffix)
        ),
        None,
    )
    if not asset:
        raise BootstrapError(f"Could not find a youki asset for {arch}-{libc}")

    with tempfile.TemporaryDirectory() as temp_dir:
        archive_path = Path(temp_dir) / str(asset["name"])
        download_to_path(str(asset["browser_download_url"]), archive_path)

        digest = str(asset.get("digest", ""))
        if digest.startswith("sha256:"):
            actual = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            expected = digest.split(":", 1)[1]
            if actual != expected:
                raise BootstrapError("Downloaded youki archive failed SHA-256 verification")

        with tarfile.open(archive_path, "r:gz") as archive:
            member = next((m for m in archive.getmembers() if Path(m.name).name == "youki"), None)
            if not member:
                raise BootstrapError("Downloaded youki archive did not contain a youki binary")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise BootstrapError("Failed to extract the youki binary from the archive")
            YOUKI_INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with YOUKI_INSTALL_PATH.open("wb") as handle:
                handle.write(extracted.read())

    YOUKI_INSTALL_PATH.chmod(0o755)
    success(ctx.console, f"Installed youki {target_version} to {YOUKI_INSTALL_PATH}")


def runtime_binary_path(runtime: str) -> str:
    if runtime == "youki":
        return str(YOUKI_INSTALL_PATH)
    resolved = shutil.which(runtime)
    if resolved:
        return resolved
    candidate = Path("/usr/bin") / runtime
    if candidate.exists():
        return str(candidate)
    raise BootstrapError(f"Runtime binary not found on PATH: {runtime}")


def read_toml_document(console: Console, path: Path) -> Any:
    if not path.exists() or not path.read_text().strip():
        return tomlkit.document()
    try:
        return tomlkit.parse(path.read_text())
    except Exception:
        backup_once(path)
        warn(console, f"Invalid TOML detected at {path}; recreating the file")
        return tomlkit.document()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_once(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_toml(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_once(path)
    path.write_text(tomlkit.dumps(document))


def update_docker_daemon_payload(
    existing: dict[str, Any], *, runtime_name: str, runtime_path: str, storage_driver: str
) -> dict[str, Any]:
    payload = dict(existing)
    runtimes = dict(payload.get("runtimes") or {})
    runtimes[runtime_name] = {"path": runtime_path}
    payload["runtimes"] = runtimes
    payload["default-runtime"] = runtime_name
    payload["storage-driver"] = storage_driver
    return payload


def configure_docker(ctx: Context, services: ServiceManager) -> None:
    info(ctx.console, "Configuring Docker")
    daemon_path = Path("/etc/docker/daemon.json")
    existing: dict[str, Any] = {}
    if daemon_path.exists() and daemon_path.read_text().strip():
        try:
            existing = json.loads(daemon_path.read_text())
        except json.JSONDecodeError:
            backup_once(daemon_path)
            warn(ctx.console, "Existing Docker daemon.json is invalid JSON; recreating it")

    storage_driver = "overlay2" if ctx.privilege_mode == "privileged" else "fuse-overlayfs"
    payload = update_docker_daemon_payload(
        existing,
        runtime_name=ctx.runtime,
        runtime_path=runtime_binary_path(ctx.runtime),
        storage_driver=storage_driver,
    )
    write_json(daemon_path, payload)
    services.enable_and_start_any(["docker"], "Docker")
    docker_sock = Path("/var/run/docker.sock")
    if docker_sock.exists():
        docker_sock.chmod(0o660)
    success(ctx.console, "Docker configuration complete")


def configure_podman_storage(ctx: Context) -> None:
    storage_path = Path("/etc/containers/storage.conf")
    doc = read_toml_document(ctx.console, storage_path)
    if "storage" not in doc:
        doc["storage"] = tomlkit.table()
    storage = doc["storage"]
    storage["driver"] = "overlay"
    if "options" not in storage:
        storage["options"] = tomlkit.table()
    options = storage["options"]
    if "overlay" not in options:
        options["overlay"] = tomlkit.table()
    overlay = options["overlay"]
    if ctx.privilege_mode == "privileged":
        overlay.pop("mount_program", None)
    else:
        overlay["mount_program"] = "/usr/bin/fuse-overlayfs"
    write_toml(storage_path, doc)


def configure_podman_runtime(ctx: Context) -> None:
    containers_conf = Path("/etc/containers/containers.conf")
    doc = read_toml_document(ctx.console, containers_conf)
    if "engine" not in doc:
        doc["engine"] = tomlkit.table()
    doc["engine"]["runtime"] = runtime_binary_path(ctx.runtime)
    write_toml(containers_conf, doc)


def configure_podman_compat(ctx: Context, services: ServiceManager) -> None:
    services.enable_and_start_any(["podman", "podman.socket"], "Podman")
    profile_path = Path("/etc/profile.d/podman-docker-host.sh")
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text('export DOCKER_HOST="unix:///run/podman/podman.sock"\n')
    profile_path.chmod(0o644)

    fish_conf = Path("/etc/fish/conf.d/podman-docker-host.fish")
    fish_conf.parent.mkdir(parents=True, exist_ok=True)
    fish_conf.write_text('set -gx DOCKER_HOST "unix:///run/podman/podman.sock"\n')
    fish_conf.chmod(0o644)

    podman_sock = Path("/run/podman/podman.sock")
    docker_sock = Path("/var/run/docker.sock")
    docker_sock.parent.mkdir(parents=True, exist_ok=True)
    if podman_sock.exists() and not docker_sock.exists():
        docker_sock.symlink_to(podman_sock)


def configure_podman(ctx: Context, services: ServiceManager) -> None:
    info(ctx.console, "Configuring Podman")
    if ctx.os_type == "alpine":
        services.enable_and_start_any(["cgroups"], "cgroups")
    configure_podman_storage(ctx)
    configure_podman_runtime(ctx)
    configure_podman_compat(ctx, services)
    success(ctx.console, "Podman configuration complete")


def ssh_service_names(os_type: str) -> list[str]:
    return ["sshd"] if os_type == "alpine" else ["ssh", "sshd"]


def set_sshd_directive(path: Path, directive: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*#?\s*{re.escape(directive)}\b")
    replaced = False
    updated: list[str] = []
    for line in lines:
        if pattern.match(line) and not replaced:
            updated.append(f"{directive} {value}")
            replaced = True
        elif pattern.match(line):
            continue
        else:
            updated.append(line)
    if not replaced:
        updated.append(f"{directive} {value}")
    path.write_text("\n".join(updated) + "\n")


def configure_ssh(ctx: Context, services: ServiceManager) -> None:
    if ctx.options.skip_ssh_keys:
        warn(ctx.console, "Skipping SSH configuration (--skip-ssh-keys)")
        return

    info(ctx.console, f"Configuring SSH for GitHub user {ctx.github_user}")
    services.enable_and_start_any(ssh_service_names(ctx.os_type), "SSH")

    ssh_dir = Path("/root/.ssh")
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    ssh_dir.chmod(0o700)
    auth_keys = ssh_dir / "authorized_keys"
    existing_lines = [line for line in auth_keys.read_text().splitlines() if line.strip()] if auth_keys.exists() else []
    existing_keys = set(existing_lines)

    with urllib.request.urlopen(
        urllib.request.Request(
            f"https://github.com/{ctx.github_user}.keys",
            headers={"User-Agent": PROJECT_NAME},
        )
    ) as response:
        fetched_keys = [line.strip() for line in response.read().decode("utf-8").splitlines() if line.strip()]
    if not fetched_keys:
        raise BootstrapError(f"No SSH keys were found for GitHub user {ctx.github_user}")

    merged = list(existing_lines)
    for key in fetched_keys:
        if key not in existing_keys:
            merged.append(key)
            existing_keys.add(key)
    auth_keys.write_text("\n".join(merged) + "\n")
    auth_keys.chmod(0o600)

    sshd_config = Path("/etc/ssh/sshd_config")
    if sshd_config.exists():
        backup_once(sshd_config)
    set_sshd_directive(sshd_config, "PermitRootLogin", "prohibit-password")
    set_sshd_directive(sshd_config, "PasswordAuthentication", "no")
    set_sshd_directive(sshd_config, "PubkeyAuthentication", "yes")
    services.restart_any(ssh_service_names(ctx.os_type), "SSH")
    success(ctx.console, "SSH configuration complete")


def ensure_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text().splitlines() if path.exists() else []
    if line not in existing:
        existing.append(line)
    path.write_text("\n".join(existing) + "\n")


def build_profile_paths_sh() -> str:
    return textwrap.dedent(
        f"""\
        # Managed by {PROJECT_NAME}.
        export PATH="{LOCAL_BIN_DIR}:{MISE_SHIMS_DIR}:$PATH"
        """
    )


def build_profile_paths_fish() -> str:
    return textwrap.dedent(
        f"""\
        # Managed by {PROJECT_NAME}.
        if not contains {LOCAL_BIN_DIR} $fish_user_paths
            set -Ua fish_user_paths {LOCAL_BIN_DIR}
        end
        if not contains {MISE_SHIMS_DIR} $fish_user_paths
            set -Ua fish_user_paths {MISE_SHIMS_DIR}
        end
        """
    )


def build_rerun_aliases_sh() -> str:
    return textwrap.dedent(
        f"""\
        # Re-run {PROJECT_NAME} from GitHub using uv.
        if command -v uv >/dev/null 2>&1; then
            alias lxc-bootstrap='uv run {RAW_SCRIPT_URL}'
        elif [ -x /root/.local/bin/uv ]; then
            alias lxc-bootstrap='/root/.local/bin/uv run {RAW_SCRIPT_URL}'
        fi
        """
    )


def build_rerun_aliases_fish() -> str:
    return textwrap.dedent(
        f"""\
        # Re-run {PROJECT_NAME} from GitHub using uv.
        if command -sq uv
            alias lxc-bootstrap 'uv run {RAW_SCRIPT_URL}'
        else if test -x /root/.local/bin/uv
            alias lxc-bootstrap '/root/.local/bin/uv run {RAW_SCRIPT_URL}'
        end
        """
    )


def build_shared_aliases_sh(engine: str) -> str:
    engine_block = ""
    if engine == "docker":
        engine_block = textwrap.dedent(
            """\
            alias ctr='docker'
            alias ctrc='docker compose'
            alias ctrps='docker ps -a'
            alias ctri='docker images'
            alias d='docker'
            alias dc='docker compose'
            alias dps='docker ps -a'
            alias di='docker images'
            """
        )
    else:
        engine_block = textwrap.dedent(
            """\
            alias ctr='podman'
            alias ctrc='podman compose'
            alias ctrps='podman ps -a'
            alias ctri='podman images'
            alias p='podman'
            alias pc='podman compose'
            alias psa='podman ps -a'
            alias pi='podman images'
            """
        )
    return textwrap.dedent(
        f"""\
        # Managed by {PROJECT_NAME}.
        if command -v eza >/dev/null 2>&1; then
            alias ls='eza'
            alias ll='eza -lah --icons=auto'
            alias lt='eza --tree -L 2 --icons=auto'
        else
            alias ll='ls -lah'
            alias lt='ls -lah'
        fi
        alias grep='grep --color=auto'
        alias cls='clear'
        alias c='clear'
        alias ..='cd ..'
        alias ...='cd ../..'
        alias lzd='lazydocker'
        alias bt='btop'
        alias rg='rg --smart-case'
        alias du='dust'
        {engine_block.rstrip()}
        """
    )


def build_shared_aliases_fish(engine: str) -> str:
    if engine == "docker":
        engine_block = textwrap.dedent(
            """\
            alias ctr 'docker'
            alias ctrc 'docker compose'
            alias ctrps 'docker ps -a'
            alias ctri 'docker images'
            alias d 'docker'
            alias dc 'docker compose'
            alias dps 'docker ps -a'
            alias di 'docker images'
            """
        )
    else:
        engine_block = textwrap.dedent(
            """\
            alias ctr 'podman'
            alias ctrc 'podman compose'
            alias ctrps 'podman ps -a'
            alias ctri 'podman images'
            alias p 'podman'
            alias pc 'podman compose'
            alias psa 'podman ps -a'
            alias pi 'podman images'
            """
        )
    return textwrap.dedent(
        f"""\
        # Managed by {PROJECT_NAME}.
        if command -sq eza
            alias ls 'eza'
            alias ll 'eza -lah --icons=auto'
            alias lt 'eza --tree -L 2 --icons=auto'
        else
            alias ll 'ls -lah'
            alias lt 'ls -lah'
        end
        alias grep 'grep --color=auto'
        alias cls 'clear'
        alias c 'clear'
        alias .. 'cd ..'
        alias ... 'cd ../..'
        alias lzd 'lazydocker'
        alias bt 'btop'
        alias rg 'rg --smart-case'
        alias du 'dust'
        {engine_block.rstrip()}
        """
    )


def configure_shells(ctx: Context) -> None:
    info(ctx.console, "Configuring shell environment")
    profile_paths = Path("/etc/profile.d/lxc-bootstrap-paths.sh")
    profile_paths.parent.mkdir(parents=True, exist_ok=True)
    profile_paths.write_text(build_profile_paths_sh())
    profile_paths.chmod(0o644)

    profile_aliases = Path("/etc/profile.d/lxc-bootstrap-aliases.sh")
    profile_aliases.write_text(build_rerun_aliases_sh() + "\n" + build_shared_aliases_sh(ctx.engine))
    profile_aliases.chmod(0o644)

    fish_paths = Path("/etc/fish/conf.d/lxc-bootstrap-paths.fish")
    fish_paths.parent.mkdir(parents=True, exist_ok=True)
    fish_paths.write_text(build_profile_paths_fish())
    fish_paths.chmod(0o644)

    fish_aliases = Path("/etc/fish/conf.d/lxc-bootstrap-aliases.fish")
    fish_aliases.write_text(build_rerun_aliases_fish() + "\n" + build_shared_aliases_fish(ctx.engine))
    fish_aliases.chmod(0o644)

    bashrc = Path("/root/.bashrc")
    bashrc.touch(exist_ok=True)
    ensure_line(bashrc, ". /etc/profile.d/lxc-bootstrap-paths.sh")
    ensure_line(bashrc, ". /etc/profile.d/lxc-bootstrap-aliases.sh")
    ensure_line(
        bashrc,
        'if [ -r /usr/share/bash-completion/bash_completion ]; then . /usr/share/bash-completion/bash_completion; fi',
    )
    ensure_line(bashrc, 'command -v mise >/dev/null 2>&1 && eval "$(mise activate bash)"')
    ensure_line(bashrc, 'command -v atuin >/dev/null 2>&1 && eval "$(atuin init bash)"')
    ensure_line(bashrc, 'command -v starship >/dev/null 2>&1 && eval "$(starship init bash)"')
    ensure_line(bashrc, 'command -v fzf >/dev/null 2>&1 && eval "$(fzf --bash)"')
    bashrc.chmod(0o644)

    if ctx.options.install_fish:
        fish_config = Path("/root/.config/fish/config.fish")
        fish_config.parent.mkdir(parents=True, exist_ok=True)
        fish_config.touch(exist_ok=True)
        ensure_line(fish_config, 'set -U fish_greeting ""')
        ensure_line(fish_config, 'command -sq mise; and mise activate fish | source')
        ensure_line(fish_config, 'command -sq atuin; and atuin init fish | source')
        ensure_line(fish_config, 'command -sq starship; and starship init fish | source')
        ensure_line(fish_config, 'command -sq fzf; and fzf --fish | source')
        fish_config.chmod(0o644)

    success(ctx.console, "Shell configuration complete")


def configure_login_fastfetch(ctx: Context) -> None:
    profile = Path("/etc/profile.d/lxc-bootstrap-fastfetch.sh")
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        textwrap.dedent(
            f"""\
            # Managed by {PROJECT_NAME}.
            if [ -z "${{LXC_BOOTSTRAP_FASTFETCH_SHOWN:-}}" ] && [ -t 1 ] && [ "${{TERM:-dumb}}" != "dumb" ]; then
                case "$-" in
                    *i*)
                        if command -v fastfetch >/dev/null 2>&1; then
                            export LXC_BOOTSTRAP_FASTFETCH_SHOWN=1
                            fastfetch
                        fi
                        ;;
                esac
            fi
            """
        )
    )
    profile.chmod(0o644)

    fish_profile = Path("/etc/fish/conf.d/lxc-bootstrap-fastfetch.fish")
    fish_profile.parent.mkdir(parents=True, exist_ok=True)
    fish_profile.write_text(
        textwrap.dedent(
            """\
            if status is-interactive; and status is-login; and not set -q LXC_BOOTSTRAP_FASTFETCH_SHOWN; and test "$TERM" != dumb; and command -sq fastfetch
                set -gx LXC_BOOTSTRAP_FASTFETCH_SHOWN 1
                fastfetch
            end
            """
        )
    )
    fish_profile.chmod(0o644)


def configure_motd(ctx: Context) -> None:
    shells = ["ash" if ctx.os_type == "alpine" else "sh"]
    if ctx.options.install_bash:
        shells.append("bash")
    if ctx.options.install_fish:
        shells.append("fish")
    testing = "enabled" if (ctx.os_type == "alpine" and not ctx.options.disable_testing_repos) else "not in use"
    motd = textwrap.dedent(
        f"""\
        LXC bootstrap host
        ==================

        Bootstrap state
        - Engine: {ctx.engine}
        - Runtime: {ctx.runtime}
        - Runtime environment: {ctx.environment}
        - Container privilege mode: {ctx.privilege_mode}
        - Testing repositories: {testing}
        - Configured shells: {', '.join(shells)}
        - State file: {STATE_FILE}

        Login behavior
        - Interactive logins run fastfetch when it is installed.

        Useful checks
        - {ctx.engine} info
        - cat {STATE_FILE}
        - fastfetch
        """
    )
    Path("/etc/motd").write_text(motd)
    Path("/etc/motd").chmod(0o644)


def configure_guest_agent(ctx: Context, services: ServiceManager) -> None:
    if ctx.environment == "lxc":
        info(ctx.console, "Skipping qemu-guest-agent service enablement in LXC")
        return
    services.enable_and_start_any(["qemu-guest-agent"], "QEMU guest agent")


def summarize_configuration(ctx: Context) -> None:
    info(ctx.console, f"Bootstrap version: {PROJECT_VERSION}")
    info(ctx.console, f"Container engine: {ctx.engine}")
    info(ctx.console, f"OCI runtime: {ctx.runtime}")
    info(ctx.console, f"Runtime environment: {ctx.environment}")
    info(ctx.console, f"UID mapping mode: {ctx.privilege_mode}")
    info(ctx.console, f"GitHub user: {ctx.github_user}")
    info(ctx.console, f"SSH bootstrap: {'disabled' if ctx.options.skip_ssh_keys else 'enabled'}")
    info(
        ctx.console,
        f"Testing repos: {'disabled' if ctx.options.disable_testing_repos else 'enabled for Alpine'}",
    )


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description="Bootstrap Alpine or Debian LXC/VM hosts")
    parser.add_argument("--engine", choices=["docker", "podman"])
    parser.add_argument("--runtime", choices=["runc", "crun", "youki"])
    parser.add_argument("--force-engine-switch", action="store_true")
    parser.add_argument("--force-runtime-switch", action="store_true")
    parser.add_argument("--force-storage-backend-switch", action="store_true")
    parser.add_argument("--disable-testing-repos", action="store_true")
    parser.add_argument("--run-setup-apkrepos", action="store_true")
    parser.add_argument("--skip-setup-apkrepos", action="store_true")
    parser.add_argument("--skip-ssh-keys", action="store_true")
    parser.add_argument("--install-bash", dest="install_bash", action="store_true", default=True)
    parser.add_argument("--no-bash", dest="install_bash", action="store_false")
    parser.add_argument("--install-fish", dest="install_fish", action="store_true", default=True)
    parser.add_argument("--no-fish", dest="install_fish", action="store_false")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)
    return Options(
        engine=args.engine,
        runtime=args.runtime,
        disable_testing_repos=args.disable_testing_repos,
        run_setup_apkrepos=args.run_setup_apkrepos,
        skip_ssh_keys=args.skip_ssh_keys,
        force_engine_switch=args.force_engine_switch,
        force_runtime_switch=args.force_runtime_switch,
        force_storage_backend_switch=args.force_storage_backend_switch,
        install_bash=args.install_bash,
        install_fish=args.install_fish,
        no_color=args.no_color,
        debug=args.debug,
    )


def build_context(options: Options, console: Console) -> Context:
    os_type = detect_os(console)
    state = load_state(console)
    environment = detect_environment(console)
    privilege_mode = detect_container_privilege_mode(console)
    engine = choose_saved_or_override(
        kind="engine",
        saved=state.get("bootstrap_engine"),
        override=options.engine,
        force=options.force_engine_switch,
        default="docker",
    )
    runtime = choose_saved_or_override(
        kind="runtime",
        saved=state.get("bootstrap_runtime"),
        override=options.runtime,
        force=options.force_runtime_switch,
        default="crun",
    )
    return Context(
        options=options,
        console=console,
        os_type=os_type,
        environment=environment,
        privilege_mode=privilege_mode,
        engine=engine,
        runtime=runtime,
        state=state,
        github_user=GITHUB_USER,
    )


def require_root() -> None:
    if os.geteuid() != 0:
        raise BootstrapError("This bootstrap must be run as root")


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    global DEBUG_ENABLED
    DEBUG_ENABLED = options.debug
    console = Console(no_color=options.no_color or bool(os.environ.get("NO_COLOR")))
    try:
        require_root()
        ctx = build_context(options, console)
        summarize_configuration(ctx)
        guard_storage_backend_transition(ctx)
        package_manager = PackageManager(console, ctx.os_type, debug=ctx.options.debug)
        services = ServiceManager(console, ctx.os_type)

        configure_repositories(ctx)
        prepare_system(ctx, package_manager)
        install_base_packages(ctx, package_manager)
        configure_guest_agent(ctx, services)
        install_engine_packages(ctx, package_manager)
        install_runtime(ctx, package_manager)
        user_packages = install_user_packages(ctx, package_manager)
        configure_completions(ctx, package_manager, user_packages)

        if ctx.engine == "docker":
            configure_docker(ctx, services)
        else:
            configure_podman(ctx, services)

        configure_ssh(ctx, services)
        configure_shells(ctx)
        configure_login_fastfetch(ctx)
        save_state(ctx)
        configure_motd(ctx)

        success(console, "Bootstrap complete")
        info(console, f"State file: {STATE_FILE}")
        if ctx.environment == "lxc":
            info(console, "Proxmox reminder: enable nesting=1,keyctl=1 for this CT")
            info(console, "Proxmox reminder: ensure lxc.apparmor.profile: unconfined")
            info(console, "Proxmox reminder: ensure lxc.cap.drop: ")
            info(console, "Proxmox reminder: ensure lxc.cgroup.relative: 0")
        return 0
    except BootstrapError as error:
        fail(console, str(error))
        return 1
    except urllib.error.URLError as error:
        fail(console, f"Network operation failed: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
