# lxc-bootstrap

`lxc-bootstrap` bootstraps Alpine and Debian container or VM guests for a
modern CLI-centric workflow. It keeps the original `alpine-bootstrap`
behavioral goals, but rewrites the implementation in Python so the bootstrap is
shorter, more maintainable, and easier to extend across distributions.

The project installs a container engine, configures the selected OCI runtime,
bootstraps `mise` for user-space tools, configures Bash and Fish, provisions
SSH keys from GitHub, and preserves state so repeat runs stay idempotent.

## What this project does

The bootstrap focuses on functional parity with the original shell script while
adding Debian support and runtime selection.

- Supports Alpine and Debian.
- Supports Docker and Podman.
- Defaults to Docker when no engine was previously selected.
- Supports `runc`, `crun`, and `youki`.
- Defaults to `crun` when no runtime was previously selected.
- Uses distro packages for system components where possible.
- Uses Alpine packages for the modern CLI stack on Alpine when available.
- Uses `mise` for the modern CLI stack on Debian.
- Fetches `youki` from GitHub releases only when you select `youki`.
- Preserves engine and runtime choices in `/etc/lxc-bootstrap/state.toml`.

## Files

This repository exposes a small entrypoint shell script and a self-contained
Python implementation.

- `lxc-bootstrap.sh` installs `uv` if needed, installs a re-run alias, and then
  runs `lxc-bootstrap.py` directly from GitHub.
- `lxc-bootstrap.py` contains the full bootstrap logic and can also be run
  locally.
- `pct-enable-lxc-bootstrap.sh` applies the required Proxmox host-side LXC
  settings for nested container workloads.

## Quick start

You can run the bootstrap directly from a target host with the shell entrypoint.

```sh
curl -fsSL https://raw.githubusercontent.com/jdries3/lxc-bootstrap/main/lxc-bootstrap.sh -o /tmp/lxc-bootstrap.sh
chmod +x /tmp/lxc-bootstrap.sh
/tmp/lxc-bootstrap.sh
```

After the first run, the bootstrap installs an `lxc-bootstrap` alias that re-runs
`lxc-bootstrap.py` from GitHub through `uv`.

## Options

The Python bootstrap accepts the following flags.

| Flag | Description |
| --- | --- |
| `--engine docker\|podman` | Select the container engine. Defaults to saved state, then Docker. |
| `--runtime runc\|crun\|youki` | Select the OCI runtime. Defaults to saved state, then `crun`. |
| `--force-engine-switch` | Allow switching away from the saved engine. |
| `--force-runtime-switch` | Allow switching away from the saved runtime. |
| `--force-storage-backend-switch` | Allow a risky storage backend change after UID mapping changes. |
| `--disable-testing-repos` | Disable Alpine `@testing` repositories. This is ignored on Debian. |
| `--run-setup-apkrepos` | Run Alpine's `setup-apkrepos` helper before repository edits. |
| `--skip-ssh-keys` | Skip GitHub SSH key bootstrap. |
| `--no-bash` | Skip Bash completion and Bash shell wiring. |
| `--no-fish` | Skip Fish installation and Fish shell wiring. |
| `--no-color` | Disable Rich color output. |

## Tooling model

The bootstrap separates system tooling from user-space tooling so the base OS
stays thin.

### System-managed components

The bootstrap installs these through the distro package manager when possible.

- Docker or Podman
- `runc` or `crun`
- SSH server packages
- `qemu-guest-agent` when the guest is a VM
- foundational packages such as `ca-certificates`, shell support, and engine-
  adjacent dependencies

### User-facing CLI components

The bootstrap installs user-facing CLI tools from Alpine packages on Alpine when
those packages exist. On Debian, it uses `mise` for the modern CLI stack and
uses distro packages for foundational tools such as `curl`, `git`, and `wget`.

- `atuin`
- `bat`
- `btop`
- `curl`
- `dust`
- `eza`
- `fastfetch`
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

`youki` is the one exception to the package-first rule. The bootstrap only
fetches it from GitHub releases when you explicitly select `--runtime youki`.
It uses the GitHub releases API to select the matching archive for the current
architecture and libc family, and it verifies the release digest before
installing `/usr/local/bin/youki`.

## Shell completions

The bootstrap enables Bash and Fish completions on a best-effort basis.

- Alpine installs repo-provided completion packages when they exist.
- `mise` completion scripts are generated explicitly when `mise` is installed.
- Built-in completion generators are also used for selected tools such as
  `atuin`, `starship`, and `yq` when available.

## Runtime behavior

The bootstrap updates the engine configuration idempotently.

- Docker mutates or creates `/etc/docker/daemon.json`.
- Podman mutates or creates `/etc/containers/containers.conf`.
- Podman storage is configured through `/etc/containers/storage.conf`.
- Missing config files are created from scratch.
- Existing config files are preserved and updated in place.

For storage, the bootstrap keeps the same safety model as the original script.
It chooses `overlay2` or native overlay for privileged mappings, and it chooses
`fuse-overlayfs` when the mapping is unprivileged or unknown.

## Environment detection

The bootstrap detects whether the guest is an LXC or a VM, and whether the
current UID mapping looks privileged or unprivileged.

That drives three behaviors:

- whether `qemu-guest-agent` is enabled
- which storage backend is configured for Docker or Podman
- whether the script blocks a risky storage backend switch when prior engine
  state exists

## Proxmox LXC host-side requirements

If you run this bootstrap inside a Proxmox LXC, you must also update the host-
side container configuration.

Run the helper on the Proxmox host.

```sh
./pct-enable-lxc-bootstrap.sh <CTID>
./pct-enable-lxc-bootstrap.sh --yes --restart <CTID>
```

The helper ensures these settings exist.

```ini
features: nesting=1,keyctl=1
lxc.apparmor.profile: unconfined
lxc.cap.drop: 
lxc.cgroup.relative: 0
```

<!-- prettier-ignore -->
> [!WARNING]
> `lxc.apparmor.profile: unconfined` weakens container isolation.
> Use it only when you accept that tradeoff for this guest.

## State and idempotency

The bootstrap is safe to re-run. It stores the current choices and host facts in
`/etc/lxc-bootstrap/state.toml`.

That state includes:

- selected engine
- selected runtime
- Bash and Fish installation choices
- detected runtime environment
- detected privilege mode
- last run time

## Validation

The repository includes unit tests for the pure configuration helpers.

```sh
uv run --with rich --with tomlkit python -m unittest discover -s tests -v
```

## Next steps

If you want to extend the bootstrap further, the most natural follow-ups are:

1. add more Debian package fallbacks for engine-adjacent tools,
2. add integration tests against disposable Alpine and Debian guests, and
3. add a release workflow that tags tested bootstrap snapshots.
