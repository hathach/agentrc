# HIL farm setup

Provisions a Debian 13 host as a TinyUSB HIL farm: GitHub Actions runners with pinned flashing and test tools, the
rig's udev rules, board locks, a firewall for the runner users and the Renesas uPD720201/202 firmware. The host keeps
no developer credentials. Up to three runners run CI jobs, each as its own user with the same groups and firewall policy
(internet, no LAN):

| Runner            | User                       | Repository                          | sudo            |
| ----------------- | -------------------------- | ----------------------------------- | --------------- |
| `tinyusb`         | `ghrunner`                 | `hathach/tinyusb`                   | `hil_root` only |
| `tinyuf2`         | `ghrunner-tinyuf2`         | `adafruit/tinyuf2`                  | none            |
| `arduino-tinyusb` | `ghrunner-arduino-tinyusb` | `adafruit/Adafruit_TinyUSB_Arduino` | none            |

Each runner root `/opt/actions-runner/<runner>` and its `_work` and `_diag` belong to that runner's group, which holds
the admin too from its next login. The state roots are `_work/tinyuf2/hil-state` for TinyUF2 and
`_work/Adafruit_TinyUSB_Arduino/hil-state` for Arduino TinyUSB; others, the other runners included, get nothing.
In `_work` and `_diag` the admin only reads: the units' `UMask=0022` keeps what
a job creates group-read-only, so an admin session cannot leave files there the next job cannot remove; hands-on work
runs from the admin's own checkout, never a runner's. TinyUF2 compiles on GitHub-hosted runners, so the farm carries
no toolchain for it.

`scripts/farm.toml` pins every tool: version, URL, sha256 or full git commit, install links and a version check.
`scripts/farm.py` installs from it into `/opt/hil-farm/tools/<name>/` and verifies the result; `scripts/setup.sh`
drives it.

## The host file

Everything that differs between hosts lives in one host file: the admin account, the runners and their GitHub
registration names and labels, and for a host its owner also uses, what stays the owner's. Each farm host's is
committed as [`hosts/<host>.toml`](hosts/): [`ci`](hosts/ci.toml) shows a dedicated host, [`hfp`](hosts/hfp.toml) a
shared one and every optional field.
`setup.sh install --host FILE` takes only a committed file of the checkout's `hosts/`, validates it before any change
and installs it as `/etc/hil-farm/host.toml`; later runs and `verify.sh` read that copy.

A host keeps its identity: `profile`, `admin`, `shared` and existing `runner` names and labels change only by a
reinstall (or, for a runner, its re-registration), and `--host` with a different one refuses. Runner users, repository
URLs and sudo grants are fixed in `common.sh`; host files cannot override them. Adding one runner uses the explicit
`add-runner` operation below, which refuses removal or changes to any existing setting.
The other fields change with `setup.sh update --host FILE`, which keeps the old file as `host.toml.prev`, refuses
while a runner runs, and applies:

| Field          | Takes effect                                                                                 |
|----------------|----------------------------------------------------------------------------------------------|
| `runner_path`  | the runners' `.path` on this update                                                          |
| `iar`          | `verify.sh` from now on                                                                      |

## Install

1. Boot Debian 13's generic cloud image (`debian-13-generic-amd64.qcow2` from <https://cloud.debian.org/images/cloud/>)
   with a cloud-init drive that creates the admin with your public key and passwordless sudo. On Proxmox:

   ```sh
   qm set <vmid> --ide2 <storage>:cloudinit --ciuser <admin> --sshkeys <your-key>.pub --ipconfig0 ip=dhcp
   ```

   (`qm cloudinit dump <vmid> user` shows what the guest receives.) A host installed from the Debian installer, SSH
   server only and creating the admin, has no cloud-init: copy the key from your PC with `ssh-copy-id <admin>@<host>`.
   Prove a new key-only session (`ssh -o PasswordAuthentication=no <admin>@<host>`) before going on: setup refuses to
   start unless `~<admin>/.ssh/authorized_keys` holds a key sshd will use, then allows keys only.
2. Prepare STM32CubeProgrammer, whose download needs an ST login, on a PC with a browser: download the Linux installer
   of the version `farm.toml` pins from <https://www.st.com/en/development-tools/stm32cubeprog.html>, install it
   (default `~/STMicroelectronics/STM32Cube/STM32CubeProgrammer`), then pack it with this checkout's script and copy it
   over:

   ```sh
   skills/hil-farm-setup/scripts/prepare-stm32cubeprogrammer.sh ~/STMicroelectronics/STM32Cube/STM32CubeProgrammer <dir>
   scp <dir>/stm32cubeprogrammer.tar <admin>@<host>:
   ```

   The script packs the tree reproducibly and refuses one that does not hash to the pin. The farm fetches every other
   input itself.
3. On the host, as the admin, clone this repository as root at the commit to deploy, so no other user can change what
   root runs:

   ```sh
   sudo apt-get install -y git
   sudo git clone https://github.com/hathach/agentrc.git /usr/local/src/agentrc
   sudo git -C /usr/local/src/agentrc checkout --detach <commit>
   mkdir -p ~/hil-inputs && mv ~/stm32cubeprogrammer.tar ~/hil-inputs/
   S=/usr/local/src/agentrc/skills/hil-farm-setup
   sudo $S/scripts/setup.sh install --host $S/hosts/<host>.toml --inputs ~/hil-inputs
   ```

   Setup refuses a checkout a non-root user could change or one with changes its commit does not hold, and records the
   commit in `/opt/hil-farm/SOURCE`. On a hand install sudo asks for the installer password this one time; cloud-init
   gives the admin passwordless sudo already. Keep that session open until a new `ssh <admin>@<host> command -v
   python3` prints `/opt/hil/venv/bin/python3`. Reboot so the Renesas firmware loads, then run
   `sudo /opt/hil-farm/verify.sh`.
4. Register the tinyusb runner only once tinyusb's `lib/agentrc` pin carries `hil_root.py` and `farm.toml`'s `agentrc`
   commit is that pin: every root operation of the HIL scripts then goes through `/usr/local/sbin/hil_root`
   (`hil_root.py`), the only sudo `ghrunner` has, and `hil_reserve.py` prefers the installed
   `/usr/local/sbin/usb_recover`. HIL jobs run on `contents: read` with `persist-credentials: false`, and fork runs
   stay trusted ones (the repository's approval setting). Never grant sudo on a checkout path. From a PC whose `gh`
   may administer the repository, which mints the one-hour tokens:

   ```sh
   S=/usr/local/src/agentrc/skills/hil-farm-setup/scripts/setup.sh
   ssh <admin>@<host> sudo $S runner tinyusb "$(gh api -X POST repos/hathach/tinyusb/actions/runners/registration-token --jq .token)"
   ssh <admin>@<host> sudo $S runner tinyuf2 "$(gh api -X POST repos/adafruit/tinyuf2/actions/runners/registration-token --jq .token)"
   ssh <admin>@<host> sudo $S runner arduino-tinyusb "$(gh api -X POST repos/adafruit/Adafruit_TinyUSB_Arduino/actions/runners/registration-token --jq .token)"
   ```

   Registration leaves the unit disabled and stopped, so no job is scheduled yet, not even after a reboot.
5. Run the hardware proof below for each runner's boards, then `sudo systemctl enable --now hil-runner-<runner>` and
   `sudo /opt/hil-farm/verify.sh`.
6. Delete `~/hil-inputs` from the host.

After every reboot, run `sudo /opt/hil-farm/fw/renesas_fw_check.sh` (`verify.sh` runs it too): every uPD720201/202
must be bound to `xhci-pci-renesas` and run firmware 2.0.2.6 or later ([`scripts/fw/README.md`](scripts/fw/README.md)).
Nothing else checks it, and on older firmware a usbtest battery kills the controller until the host is power cycled.

## A host its owner also uses (`shared = true`)

The farm installs beside the owner's setup instead of rebuilding the host: usually one runner, `tinyusb`, as
`ghrunner` with `hil_root` as its only sudo, the same pinned tools and the owner's toolchains on the runner's PATH
only (`runner_path`). The Renesas firmware is one of those tools: setup writes `/lib/firmware/renesas_usb_fw.mem` and
rebuilds the initramfs there too.

What stays the owner's: the admin account (it joins `hil`) with its sudo, ssh keys and home, sshd's policy (the farm
adds only `DenyUsers ghrunner`, as a runner user's ssh session would run outside its unit), the apt update and time
policy (setup installs none of `farm.toml`'s `apt.policy` there), `/etc/environment`, and `/etc/nftables.conf` with
Debian's unmodified `nftables.service` (setup refuses another). The runner unit cannot see the admin's home
(`InaccessiblePaths`). A drop-in on `nftables.service`
loads the owner's file and `/etc/hil-farm/hil_farm.nft` in one `nft -f`, on start and reload. Setup needs
`nftables.service` already enabled, and never enables the owner's firewall. Stopping it flushes every table, hil_farm
included, and the runner stops with it (`BindsTo`). The owner changes rules with `systemctl reload nftables`. A rules
file that fails to load on a start or restart leaves no rules at all, the owner's included, as on stock Debian; setup
checks both files with `nft -c` first. A direct `nft -f` or `flush ruleset` drops the hil_farm table: stop
`hil-runner-tinyusb` first, `systemctl reload nftables` to load both again, run
`sudo /opt/hil-farm/verify.sh --maintenance`, then start the runner. Setup refuses an owner's plain file at a farm link
(such as `/usr/local/bin/openocd`); move it aside first.

Install only after the owner agreed, with the host's earlier runner service, if any, stopped and disabled.

## Updating a tool

1. Edit its table in `farm.toml` (for a release: `url`, `sha256`, `version`, and `expect` when the tool prints its
   version differently), commit, and run `python3 skills/hil-farm-setup/scripts/farm.py fetch <dir> <tool>` on your PC
   to check the new pin.
2. On the host, move the root checkout to that commit: `sudo git -C /usr/local/src/agentrc fetch` and
   `sudo git -C /usr/local/src/agentrc checkout --detach <commit>`.
3. Stop the runners: when `ssh <admin>@<host> pgrep -x Runner.Worker` exits 1, `sudo systemctl stop
   hil-runner-tinyusb hil-runner-tinyuf2 hil-runner-arduino-tinyusb`. GitHub has no way to drain a runner, so a job
   assigned between those two commands is interrupted; check it for surviving processes and held boards, then re-run it.
4. `S=/usr/local/src/agentrc/skills/hil-farm-setup; sudo $S/scripts/setup.sh update --host $S/hosts/<host>.toml
   <tool>` (or no tool for all of them; add `--inputs DIR` when STM32CubeProgrammer changed). `--host` every time
   applies any change to the host's file, and is a no-op otherwise. It refuses while a runner runs and touches only
   tools, firmware, udev rules, the environment files and the runner units; a tool is switched only after its new
   install passes its check, and old installs stay in `/opt/hil-farm/tools/<name>/`.
5. `sudo /opt/hil-farm/verify.sh --maintenance`, then `sudo systemctl start` the registered runner units (reboot first
   if the firmware changed) and `sudo /opt/hil-farm/verify.sh`.

The actions runner is installed once into each runner root and not updated this way.

## Adding a runner to a host in service

Commit the host file with exactly one new runner table; keep all existing settings unchanged. A runner with a sudo
grant (`tinyusb`) comes only with `install`. The new runner uses the installed runner package pin, shared device groups
(`hil`, `dialout`, `plugdev`, `audio`), udev rules, pinned tools and `/tmp/tinyusb-hil-locks`. Addition changes the
firewall's user set (and shared hosts' SSH deny list), creates only the new account, runner tree, environment, hook
pair and unit, and gives the admin read access through the new runner's group; registration makes its `_work`,
`_diag` and state directories. Existing registrations, accounts, hooks, units and job state stay untouched. It
publishes the host file last and saves the previous one as `host.toml.prev`; retry the same command after a failure.
It does not update tools or udev rules: it refuses while the checkout's `farm.toml` or a lock file differs from
`/opt/hil-farm`'s copy, and udev rules need `update` first if changed.

1. Move the root checkout to the reviewed commit, as for an update.
2. When `pgrep -x Runner.Worker` exits 1, stop every installed runner unit (and the new one if a prior attempt
   created it), then `sudo $S/scripts/setup.sh add-runner <runner> --host $S/hosts/<host>.toml` and
   `sudo /opt/hil-farm/verify.sh --maintenance`. Setup refuses if any old or new unit is active, or any
   `Runner.Worker` remains; it never stops a job itself. A job assigned between the idle check and the stop can be
   interrupted: inspect surviving processes and held boards before continuing.
3. Before registration, check as the new runner user (`sudo -u <user>`) read/write access to its boards'
   `/dev/bus/usb`, tty, hidraw and USB block device nodes, the shared lock directory, and the pinned tools on its
   `.path` with `farm.toml`'s environment. Use a fresh admin login for the new group membership.
4. Register it as in Registration above; the unit stays disabled. Run the repository's offline hardware proof under
   its runner user, `verify.sh --maintenance` again, restart the other runners, `systemctl enable --now` the new
   unit, and run `verify.sh`, which checks every runner of the installed host file.

Before a reinstall or update, sync with tinyusb: the `agentrc` tool's commit follows its `lib/agentrc` pin, and
`scripts/udev/88-tinyusb.rules` is its file with the `adm` group as `hil` and without its catch-all hidraw rule, which
gives every local user every HID device, a shared host owner's too. Likewise `scripts/udev/*-tinyuf2-*.rules`
are tinyuf2's commissioning rules (`.claude/skills/hil-contract/commissioning.md`) with `adm` as `hil`; its harness
matches the exclusion lines as written. `common.sh`'s tinyuf2 `RUNNER_STATE` is its `hil_remote.py` `ciStateRoot`.
Arduino TinyUSB's `RUNNER_STATE` is its workflow's `$(dirname "$GITHUB_WORKSPACE")/hil-state`.

## Jobs, holds and locks

A job that may have left processes behind holds its runner (`job_hook.py`, one hook pair per runner): the next job's
start hook refuses while the previous job never reached its completed hook (a killed worker, a host crash) or while
that runner unit's cgroup still holds anything besides the runner itself, root helpers included. A cancelled or
timed-out job that leaves nothing running releases on its own; that says no job process survived, not that its boards
were restored. To clear a hold, check the processes the failed job names and the boards, then remove that runner
user's `~/.hil-job-active`.

Board locks live in `/tmp/tinyusb-hil-locks`, root-owned and sticky (1777) so the admin and `ghrunner` share them.
They are cooperative: `ghrunner` owns the lock files it creates and can unlink one under a holder, and it opens the
boards' devices directly anyway, so they keep honest jobs apart and do nothing against a hostile one.

Trusted runs only: state written by one job survives into the next. The runner root stays runner-writable (v2.337
rewrites its migration files there). The workflow wipes only its own workspace, so the rest of `_work`, including
`hil-report` for re-run attempts, persists, as does `~ghrunner` apart from `.cache`.

Network HIL tests (`device/net_lwip_*`, disabled in tinyusb's `hil_util.device_tests`) need `iperf` and a firewall
exception for the DUT's USB interface before they are re-enabled; the runner users are refused 192.168.0.0/16 and the
other private ranges today.

## Proof

`verify.sh` checks only the install. Hardware proof comes in two parts. Before enabling a runner's unit, prove its
boards offline: for tinyusb a `hil_remote` smoke run per flasher type and one usbtest battery, for tinyuf2 its own
commissioning proof, and for Arduino TinyUSB its offline HIL proof under `ghrunner-arduino-tinyusb`. After enabling
it, a green master HIL job on that repository proves the CI path.
