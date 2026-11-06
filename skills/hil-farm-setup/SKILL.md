---
name: hil-farm-setup
description: >-
  Provision, update or check a Debian 13 HIL farm host for TinyUSB, TinyUF2, Arduino TinyUSB and nRF52 bootloader CI -
  GitHub Actions runners with pinned flashing tools, udev rules, board locks, a runner firewall and the Renesas
  uPD720201 firmware - from a root-owned checkout and the host's file in `hosts/`. Use when rebuilding a farm host,
  adding or registering a runner, bumping a pinned tool (openocd, J-Link, picotool, STM32CubeProgrammer, the actions
  runner), or when verify.sh or a runner hold needs judging.
---

# HIL farm setup

[README.md](README.md) is the procedure; `scripts/setup.sh`, `scripts/farm.py` and `scripts/verify.sh` own the
mechanics, each with its usage at the top. This page is what to decide around them.

## Before touching a host

- **A host in service runs CI.** Every `install`, `update` or `add-runner` needs all runner units stopped with no
  `Runner.Worker`: wait until `pgrep -x Runner.Worker` exits 1, then stop them, and say which job, if any, was
  interrupted. For an addition, the guard covers the installed and proposed runners. A host reboot, which a firmware
  change needs, is the user's call.
- **A shared host is its owner's machine.** Install or update there only with the owner's agreement, and never change
  what the README says stays the owner's.
- **A host's settings are its file in `hosts/`.** Change them, or add a new host, by a commit; setup takes no other
  file. Check a draft with `python3 scripts/farm.py validate --host FILE`.
- **Root runs the checkout.** Deploy from a root-owned clone at a reviewed commit, never from a user's checkout or a
  path a runner user can write; setup refuses both, and its `SOURCE` receipt names the commit that is deployed.

## Judging a result

- `verify.sh` checks the install, not the boards. A clean run says nothing about a probe or a DUT; the hardware proof
  in the README does, and a green master HIL job proves the CI path.
- A refused `host-install` names the identity field that changed: that is a reinstall or a runner re-registration,
  not a file edit to force through. `setup.sh add-runner NAME --host FILE` admits only one new runner and no other
  settings changes; the existing runner package must match the pin, and the checkout's `farm.toml` and locks the
  installed ones. Other pin or lock changes clear with `setup.sh update` without `--host` (its host-install refuses a
  file that adds a runner), then the same `add-runner`. A changed actions-runner pin does not: `update` skips the
  runner yet publishes the new pin, so add the runner from a commit with the installed one, or migrate the runners
  outside this procedure first. It leaves the new unit disabled, including after registration, until its hardware
  proof. A failed addition keeps the installed host file, so retry the same command.
- A runner held by `job_hook.py` stays held while a failed job's processes are left; clear it by the README's steps,
  not by restarting the unit.
- A tool whose new install fails its check is not switched; the old one keeps serving. Report the check's output
  rather than retrying with `--force`.
