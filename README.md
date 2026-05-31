# lxc-bootstrap

`lxc-bootstrap` bootstraps Alpine and Debian container or VM guests for a
modern CLI-centric workflow. It replaces `alpine-bootstrap` with a clean
Python implementation that is shorter, more maintainable, and easier to
extend across distributions.

The tool installs a container engine, configures the OCI runtime, bootstraps
`mise` for user-space tools, configures Bash and Fish, provisions SSH keys from
GitHub, and preserves state to ensure repeated runs are idempotent.

## Quick start

You can run the bootstrap directly from a target host using the shell
entrypoint.

Using `curl`:

```sh
curl -fsSL https://raw.githubusercontent.com/jdries3/lxc-bootstrap/main/lxc-bootstrap.sh -o /tmp/lxc-bootstrap.sh
chmod +x /tmp/lxc-bootstrap.sh
/tmp/lxc-bootstrap.sh
```

Using `wget` (available by default on Alpine):

```sh
wget -qO /tmp/lxc-bootstrap.sh https://raw.githubusercontent.com/jdries3/lxc-bootstrap/main/lxc-bootstrap.sh
chmod +x /tmp/lxc-bootstrap.sh
/tmp/lxc-bootstrap.sh
```

After the first run, the bootstrap installs an `lxc-bootstrap` alias that
re-runs `lxc-bootstrap.py` from GitHub using `uv`.

## Options

The Python bootstrap accepts these command-line flags.

| Flag | Description |
| --- | --- |
| `--engine docker\|podman` | Selects the container engine. Defaults to saved state, then Docker. |
| `--runtime runc\|crun\|youki` | Selects the OCI runtime. Defaults to saved state, then `crun`. |
| `--force-engine-switch` | Lets you switch away from the saved engine. |
| `--force-runtime-switch` | Lets you switch away from the saved runtime. |
| `--force-storage-backend-switch` | Lets you change the storage backend after UID mapping changes. |
| `--disable-testing-repos` | Disables Alpine `@testing` repositories. Ignored on Debian. |
| `--run-setup-apkrepos` | Runs Alpine's `setup-apkrepos` helper before editing repositories. |
| `--skip-ssh-keys` | Skips fetching SSH keys from GitHub. |
| `--no-bash` | Skips Bash completion and shell configuration. |
| `--no-fish` | Skips Fish installation and shell configuration. |
| `--no-color` | Disables Rich color output. |

## What this project does

The bootstrap provides functional parity with the original shell script while
adding Debian support and OCI runtime selection.

- Supports Alpine and Debian.
- Supports Docker and Podman (defaults to Docker).
- Supports `runc`, `crun`, and `youki` (defaults to `crun`).
- Uses system packages for OS components where possible.
- Uses Alpine packages for the CLI stack on Alpine, and `mise` on Debian.
- Fetches `youki` from GitHub releases only when selected.
- Stores engine and runtime preferences in `/etc/lxc-bootstrap/state.toml`.

## Files

This repository contains a shell entrypoint script and a Python script.

- `lxc-bootstrap.sh`: Installs `uv`, configures the re-run alias, and executes
  `lxc-bootstrap.py` from GitHub.
- `lxc-bootstrap.py`: Contains the core bootstrap logic and can be executed
  locally.
- `pct-enable-lxc-bootstrap.sh`: Configures Proxmox host-side LXC settings
  required for nested container workloads.

## Tooling model

The bootstrap separates system dependencies from user-space tools to keep the
base OS lightweight.

### System-managed components

The distro package manager installs the system-level components.

- Docker or Podman
- `runc` or `crun`
- SSH server packages
- `qemu-guest-agent` (when running inside a VM)
- Essential dependencies (like `ca-certificates`, iptables, and shell tools)

### User-facing CLI components

The bootstrap installs user-facing CLI tools from Alpine packages on Alpine when
available. On Debian, it installs these tools using `mise`.

- `atuin`
- `bat`
- `btop`
- `curl`
- `dust`
- `eza`
- `fastfetch`
- `fd`
- `fzf`
- `git`
- `jq`
- `lazydocker`
- `nushell`
- `ripgrep`
- `starship`
- `trippy`
- `wget`
- `yq`
- `zellij`

### Youki handling

The bootstrap downloads the `youki` binary directly from GitHub releases only
when you select `--runtime youki`.

It queries the GitHub API to find the latest release matching the system's
architecture and libc family, verifies the checksum, and installs the binary to
`/usr/local/bin/youki`.

## Shell completions

The bootstrap configures Bash and Fish shell completions on a best-effort basis.

- Installs package-provided completions on Alpine.
- Generates `mise` completions during installation.
- Runs built-in completion generators for tools like `atuin`, `starship`, and
  `yq`.

## Runtime behavior

The bootstrap configures the selected container engine idempotently.

- Docker: Modifies or creates `/etc/docker/daemon.json`.
- Podman: Modifies or creates `/etc/containers/containers.conf` and
  `storage.conf`.
- Preserves existing configurations and merges updates in place.

For storage backends, it uses `overlay2` (Docker) or `overlay` (Podman) for
privileged setups, and falls back to `fuse-overlayfs` for unprivileged setups.

## Environment detection

The bootstrap detects the virtualization environment (LXC or VM) and the
UID mapping mode (privileged or unprivileged).

These facts determine:
- Whether to enable `qemu-guest-agent`.
- Which storage backend to configure for the container engine.
- Whether to block risky storage engine switches when prior state exists.

## Proxmox LXC host-side requirements

When running inside a Proxmox LXC, you must configure the host to support
nesting and keyctl.

Run the helper script on the Proxmox host:

```sh
./pct-enable-lxc-bootstrap.sh <CTID>
./pct-enable-lxc-bootstrap.sh --yes --restart <CTID>
```

This script ensures the host config contains these parameters:

```ini
features: nesting=1,keyctl=1
lxc.apparmor.profile: unconfined
lxc.cap.drop: 
lxc.cgroup.relative: 0
```

<!-- prettier-ignore -->
> [!WARNING]
> Setting `lxc.apparmor.profile: unconfined` reduces container isolation.
> Only use this setting if you accept the security implications.

## State and idempotency

The bootstrap stores configuration choices and system facts in
`/etc/lxc-bootstrap/state.toml` to ensure safety during repeat runs.

The stored state tracks:
- Selected container engine and OCI runtime
- Shell configuration choices
- Virtualization type and privilege mode
- Timestamp of the last run

## Validation

You can run the unit test suite to verify configuration helpers.

```sh
uv run --with rich --with tomlkit python -m unittest discover -s tests -v
```

## Next steps

To extend the bootstrap, we recommend these tasks:

1. Add Debian package fallbacks for engine-adjacent tools.
2. Add integration tests against disposable Alpine and Debian test guests.
3. Configure a release workflow to tag tested bootstrap snapshots.
