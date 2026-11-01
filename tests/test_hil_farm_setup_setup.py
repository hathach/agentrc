"""setup.sh's argument handling and the per-runner files it generates, from a copy whose paths point into a temp dir."""
import getpass
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the HIL farm setup runs on Debian: bash, fcntl, Linux paths')

import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import git_env, guard_survivors  # noqa: E402

FARM = Path(__file__).resolve().parent.parent / 'skills' / 'hil-farm-setup' / 'scripts'
HOSTS = Path(__file__).resolve().parent / 'fixtures' / 'hil_farm_setup'


class TestSetup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = self.tmp / 'checkout'
        self.dir.mkdir()
        for f in ['setup.sh', 'common.sh', 'farm.py', 'farm.toml', 'job_hook.py', 'requirements.lock', 'pyocd.lock']:
            shutil.copy(FARM / f, self.dir)
        self.os_release('debian', '13', 'Debian GNU/Linux 13 (trixie)')
        self.hooks = self.tmp / 'hooks'
        common = self.dir / 'common.sh'
        common.write_text(common.read_text().replace('/usr/local/libexec/hil-farm', str(self.hooks)))
        self.stubs = self.tmp / 'stubs'
        self.stubs.mkdir()
        self.stub('id', 'if [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi')
        self.stub('getent', '[ "$1" = passwd ] && echo "$2:x:1000:1000::/home/$2:/bin/bash"')
        self.use_host('dedicated')

    def os_release(self, os_id, version, pretty):
        f = self.tmp / 'os-release'
        f.write_text(f'ID={os_id}\nVERSION_ID="{version}"\nPRETTY_NAME="{pretty}"\n')
        setup = self.dir / 'setup.sh'
        setup.write_text(setup.read_text().replace('OS_RELEASE=/etc/os-release', f'OS_RELEASE={f}'))

    def stub(self, name, body):
        p = self.stubs / name
        p.write_text(f'#!/bin/sh\n{body}\n')
        p.chmod(0o755)

    def env(self):
        return {**os.environ, 'PATH': f'{self.stubs}:{os.environ["PATH"]}', 'HIL_FARM_LOCK_HELD': '1',
                'HIL_FARM_HOST': str(HOSTS / 'shared.toml')}   # an inherited one: setup.sh must ignore it

    def point_host(self):
        """The copy's installed host file is self.host."""
        f = self.dir / 'farm.py'
        text = (FARM / 'farm.py').read_text().replace("INSTALLED_HOST = Path('/etc/hil-farm/host.toml')",
                                                      f"INSTALLED_HOST = Path({str(self.host)!r})")
        if f.read_text() != text:   # unchanged otherwise: registration compares every file around one call
            f.write_text(text)

    def setup(self, *args):
        self.point_host()
        return subprocess.run(['bash', str(self.dir / 'setup.sh'), *args], capture_output=True, text=True,
                              env=self.env(), timeout=60)

    def use_host(self, name):
        self.host = HOSTS / f'{name}.toml'
        self.point_host()

    def run_sourced(self, script):
        self.point_host()
        return subprocess.run(['bash', '-c', f'. "$1/setup.sh"\n{script}', '_', str(self.dir)], capture_output=True,
                              text=True, env=self.env(), timeout=60)

    def sourced(self, script):
        r = self.run_sourced(script)
        r.check_returncode()
        return r.stdout

    def test_runner_needs_a_known_name_and_a_token(self):
        for args in ([], ['tok'], ['tinyusb'], ['tinyusb', ''], ['nope', 'tok'], ['tinyusb', 'tok', 'extra']):
            with self.subTest(args=args):
                r = self.setup('runner', *args)
                self.assertEqual(r.returncode, 1)
                self.assertIn('usage: setup.sh runner <tinyusb|tinyuf2> <token>', r.stderr)

    def test_a_valid_runner_call_gets_past_the_arguments(self):
        for name in ('tinyusb', 'tinyuf2'):
            with self.subTest(name=name):
                r = self.setup('runner', name, 'tok')
                self.assertEqual(r.returncode, 1)   # stopped by preflight, the next step
                self.assertRegex(r.stderr, 'could replace|not root-owned')   # this checkout is not root's

    def test_only_debian_13_as_root(self):
        self.os_release('ubuntu', '24.04', 'Ubuntu 24.04 LTS')
        r = self.setup('install', '--host', str(self.host))
        self.assertEqual(r.returncode, 1)
        self.assertIn('expects Debian 13, found Ubuntu 24.04 LTS', r.stderr)
        self.stub('id', 'if [ "$1" = -u ]; then echo 1000; else exec /usr/bin/id "$@"; fi')
        self.assertIn('run as root', self.setup('install').stderr)

    def test_nft_rule_covers_both_runner_users(self):
        rules = self.sourced('nft_ruleset 192.0.2.53')
        self.assertIn('    meta skuid != { "ghrunner", "ghrunner-tinyuf2" } accept\n', rules)
        self.assertIn('ip daddr { 192.0.2.53 } meta l4proto { tcp, udp } th dport 53 accept', rules)

    def test_each_runner_gets_its_own_hooks_naming_its_own_unit(self):
        self.hooks.mkdir()
        self.sourced('install_hooks tinyusb; install_hooks tinyuf2')
        for name, other in (('tinyusb', 'tinyuf2'), ('tinyuf2', 'tinyusb')):
            started = (self.hooks / f'job-started-{name}.sh').read_text()
            completed = (self.hooks / f'job-completed-{name}.sh').read_text()
            self.assertIn(f'{self.hooks}/job_hook.py start hil-runner-{name}\n', started)
            self.assertIn(f'{self.hooks}/job_hook.py complete hil-runner-{name}\n', completed)
            self.assertNotIn(other, started + completed)
            for f in (f'job-started-{name}.sh', f'job-completed-{name}.sh'):
                subprocess.run(['sh', '-n', str(self.hooks / f)], check=True)
                self.assertEqual((self.hooks / f).stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.hooks / 'job-started.sh').exists())

    def test_each_runner_env_points_at_its_own_hooks(self):
        for name in ('tinyusb', 'tinyuf2'):
            with self.subTest(name=name):
                env = self.sourced(f'runner_conf {name}; runner_env').splitlines()
                self.assertIn(f'ACTIONS_RUNNER_HOOK_JOB_STARTED={self.hooks}/job-started-{name}.sh', env)
                self.assertIn(f'ACTIONS_RUNNER_HOOK_JOB_COMPLETED={self.hooks}/job-completed-{name}.sh', env)
                self.assertIn('PYTHONNOUSERSITE=1', env)

    def register(self, active, disable):
        """register_runner tinyuf2 until its first log line, after the disable: (exit, stderr, systemctl calls,
        whether setup_runner_service ran)."""
        common = self.dir / 'common.sh'
        common.write_text(common.read_text().replace('/opt/actions-runner', str(self.tmp / 'runners')))
        (self.tmp / 'runners' / 'tinyuf2' / 'bin').mkdir(parents=True, exist_ok=True)
        log = self.tmp / 'systemctl.log'
        log.write_text('')
        self.stub('systemctl', f'echo "$*" >> {log}; [ "$1" != is-active ] || exit {active}; '
                               f'[ "$1" != disable ] || exit {disable}')
        r = subprocess.run(['bash', '-c', '. "$1/setup.sh"; setup_runner_service() { echo SETUP; }; '
                            'log() { exit 0; }; register_runner tinyuf2 tok', '_', str(self.dir)],
                           capture_output=True, text=True, env=self.env(), timeout=60)
        return r.returncode, r.stderr, log.read_text().splitlines(), 'SETUP' in r.stdout

    def test_registration_refuses_a_running_unit_before_any_setup(self):
        rc, err, calls, setup = self.register(active=0, disable=0)
        self.assertEqual(rc, 1)
        self.assertIn('hil-runner-tinyuf2 is running', err)
        self.assertEqual(calls, ['is-active -q hil-runner-tinyuf2'])
        self.assertFalse(setup)

    def test_registration_disables_the_unit_once_written(self):
        for disable, want in ((0, 0), (1, 1)):
            with self.subTest(disable=disable):
                rc, err, calls, setup = self.register(active=1, disable=disable)
                self.assertEqual(rc, want)
                self.assertTrue(setup)
                self.assertEqual(calls, ['is-active -q hil-runner-tinyuf2', 'disable -q hil-runner-tinyuf2'])
                if want:
                    self.assertIn('cannot disable hil-runner-tinyuf2', err)

    def registration(self, name, other):
        """Register `name` end to end with root-only commands stubbed, `other` already registered; return the files
        registration must leave alone, before and after."""
        common = self.dir / 'common.sh'
        common.write_text(common.read_text().replace('/opt/actions-runner', str(self.tmp / 'runners'))
                          .replace('/usr/local/sbin/hil_root', str(self.tmp / 'hil_root')))
        setup = self.dir / 'setup.sh'
        etc = self.tmp / 'etc'
        setup.write_text(setup.read_text().replace('/etc/systemd/system', str(etc / 'units'))
                         .replace('/etc/environment', str(etc / 'environment'))
                         .replace('/etc/profile.d', str(etc / 'profile.d')))
        (self.tmp / 'hil_root').write_text('#!/bin/sh\n')
        (self.tmp / 'hil_root').chmod(0o755)
        (etc / 'units').mkdir(parents=True)
        self.hooks.mkdir()
        (self.hooks / 'job_hook.py').write_text('installed by setup.sh install\n')
        for n in (name, other):
            d = self.tmp / 'runners' / n
            (d / 'bin').mkdir(parents=True)
            for f in ('runsvc.sh', 'installdependencies.sh'):
                (d / 'bin' / f).write_text(f'#!/bin/sh\ntouch {self.tmp}/deps-ran\n')
                (d / 'bin' / f).chmod(0o755)
        od = self.tmp / 'runners' / other
        for f in ('.runner', '.credentials', '.env', '.path', 'runsvc.sh'):
            (od / f).write_text(f'{other} {f}\n')
        for f in (self.hooks / f'job-started-{other}.sh', self.hooks / f'job-completed-{other}.sh',
                  etc / 'units' / f'hil-runner-{other}.service'):
            f.write_text(f'{other}\n')
        self.stub('systemctl', '[ "$1" != is-active ] || exit 3')
        self.stub('chown', ':')
        # runuser -u U -- env -C DIR ...: config.sh registers into DIR
        self.stub('runuser', '[ "$4 $5" = "env -C" ] && echo "{}" > "$6/.runner" && echo "{}" > "$6/.credentials" '
                             f'&& echo "$@" > {self.tmp}/config-args')
        # install -d -o U -g U ...: owners need root
        self.stub('install', 'n=$#; while [ $n -gt 0 ]; do a=$1; shift; n=$((n - 1)); case $a in '
                             '-o|-g) shift; n=$((n - 1)) ;; *) set -- "$@" "$a" ;; esac; done; '
                             'exec /usr/bin/install "$@"')
        kept = [p for p in self.tmp.rglob('*') if p.is_file() and name not in p.relative_to(self.tmp).parts
                and not p.name.endswith(f'-{name}.sh') and p.parent != self.stubs]
        before = {p: p.read_bytes() for p in kept}
        self.sourced(f'register_runner {name} tok')
        return before, {p: p.read_bytes() for p in kept}

    def test_registration_touches_only_the_selected_runner(self):
        for name, other in (('tinyusb', 'tinyuf2'), ('tinyuf2', 'tinyusb')):
            with self.subTest(name=name):
                self.setUp()
                before, after = self.registration(name, other)
                self.assertIn(self.hooks / 'job_hook.py', before)
                self.assertIn(self.tmp / 'runners' / other / '.env', before)
                self.assertEqual(before, after)
                self.assertFalse((self.tmp / 'etc' / 'environment').exists())
                self.assertFalse((self.tmp / 'etc' / 'profile.d').exists())
                self.assertFalse((self.tmp / 'deps-ran').exists(), 'installdependencies.sh is install-only')
                d = self.tmp / 'runners' / name
                self.assertIn(f'ACTIONS_RUNNER_HOOK_JOB_STARTED={self.hooks}/job-started-{name}.sh\n',
                              (d / '.env').read_text())
                self.assertTrue((d / '.path').read_text().strip())
                self.assertEqual((d / '_work' / 'tinyuf2' / 'hil-state').is_dir(), name == 'tinyuf2')
                self.assertIn(f'start hil-runner-{name}\n', (self.hooks / f'job-started-{name}.sh').read_text())
                unit = (self.tmp / 'etc' / 'units' / f'hil-runner-{name}.service').read_text()
                self.assertIn(f'User={("ghrunner", "ghrunner-tinyuf2")[name == "tinyuf2"]}\n', unit)
                self.assertIn('Requires=nftables.service\nConditionPathExists', unit)
                self.assertNotIn('BindsTo', unit)
                self.assertNotIn('InaccessiblePaths', unit)
                self.assertIn(f'--name lab-{name} --labels ', (self.tmp / 'config-args').read_text())

    def test_a_shared_host_registers_its_runner_bound_to_the_firewall_and_blind_to_the_owner(self):
        self.use_host('shared')
        before, after = self.registration('tinyusb', 'tinyusb-none')
        self.assertEqual(before, after)
        unit = (self.tmp / 'etc' / 'units' / 'hil-runner-tinyusb.service').read_text()
        self.assertIn('Wants=network-online.target\nBindsTo=nftables.service\nConditionPathExists', unit)
        self.assertNotIn('Requires=', unit)
        self.assertIn('TimeoutStopSec=5min\nInaccessiblePaths=/home/owner\n', unit)
        self.assertIn('--name shared-tinyusb --labels shared ', (self.tmp / 'config-args').read_text())
        path = (self.tmp / 'runners' / 'tinyusb' / '.path').read_text().strip()
        self.assertTrue(path.endswith(':/opt/vendor/arm/bin'))

    def test_a_shared_host_runs_only_its_runners(self):
        self.use_host('shared')
        out = self.sourced('echo "$ADMIN ${RUNNERS[*]} $SHARED_HOST $IAR_CHECKS $ADMIN_HOME"; '
                           'runner_conf tinyusb; echo "$RUNNER_USER $RUNNER_REG_NAME $RUNNER_LABELS"; '
                           'runner_conf tinyuf2 || echo refused')
        self.assertEqual(out.splitlines(), ['owner tinyusb 1 1 /home/owner', 'ghrunner shared-tinyusb shared', 'refused'])
        r = self.setup('runner', 'tinyuf2', 'tok')
        self.assertIn('usage: setup.sh runner <tinyusb> <token>', r.stderr)

    def test_a_bad_host_file_stops_everything(self):
        bad = self.tmp / 'bad.toml'
        bad.write_text((HOSTS / 'dedicated.toml').read_text().replace('admin = "admin"', 'admin = "Bad Name"'))
        self.host = bad
        for args in (['install'], ['update'], ['runner', 'tinyusb', 'tok']):
            with self.subTest(args=args):
                r = self.setup(*args)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('admin must be a user name', r.stderr)
        self.host = self.tmp / 'none.toml'
        r = self.setup('install')
        self.assertIn(f'no host file {self.host}', r.stderr)

    def test_host_option(self):
        log = self.tmp / 'farm.log'
        setup = self.dir / 'setup.sh'   # past the argument handling: record what install_host and the tools get
        setup.write_text(setup.read_text().replace('preflight() {', f'preflight() {{ return 0; }}\nunused() {{')
                         .replace('  farm "$@" ${INPUTS:+--inputs "$INPUTS"}\n', '  farm "$@" ${INPUTS:+--inputs "$INPUTS"}\n  return 0\n')
                         .replace('farm() { python3 -I "$B/farm.py" "$@"; }',
                                  f'farm() {{ echo "${{HIL_FARM_HOST:-installed}} $*" >> {log}; '
                                  f'[ "$1" = host-sh ] && python3 -I "$B/farm.py" "$@"; return 0; }}')
                         .replace('    install_rules\n    install_job_hook\n', '    exit 0\n'))
        r = self.setup('update', '--host', str(HOSTS / 'shared.toml'), 'openocd', '--inputs', str(self.tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = log.read_text().splitlines()
        self.assertIn(f'{HOSTS / "shared.toml"} host-install {HOSTS / "shared.toml"}', calls)
        self.assertIn(f'{HOSTS / "shared.toml"} update openocd --inputs {self.tmp}', calls)
        log.unlink()
        r = self.setup('update', 'openocd')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('installed update openocd', log.read_text().splitlines())   # not the inherited one
        self.assertFalse([c for c in log.read_text().splitlines() if 'host-install' in c])
        for args in (['install', 'x'], ['install', '--host'], ['install', '--inputs', str(self.tmp / 'none')],
                     ['runner', 'tinyusb', 'tok', '--host', str(self.host)],
                     ['runner', 'tinyusb', 'tok', '--inputs', str(self.tmp)]):
            with self.subTest(args=args):
                r = self.setup(*args)
                self.assertEqual(r.returncode, 1)
                self.assertRegex(r.stderr, 'usage|--host needs a file|--inputs needs a directory')

    def trust_copy(self):
        """source_rev's ownership checks name this test's user instead of root; the copy as a root clone's modes."""
        setup = self.dir / 'setup.sh'
        setup.write_text(setup.read_text().replace('farm trusted "$B" ${HOST_FILE:+"$HOST_FILE"} >&2 \\\n', 'true \\\n')
                         .replace('! -user root', f'! -user {getpass.getuser()}'))
        subprocess.run(['chmod', '-R', 'go-w', str(self.dir)], check=True)   # umask 022

    def checkout(self):
        """The trusted copy as a git checkout; returns its commit."""
        self.trust_copy()
        # the scripts sit in a subdirectory of the checkout, as in agentrc, so .git is not under them
        for argv in (['init', '-q'], ['add', self.dir.name], ['commit', '-qm', 'farm']):
            subprocess.run(['git', '-C', str(self.tmp), *argv], check=True, env=git_env(), capture_output=True)
        subprocess.run(['chmod', '-R', 'go-w', str(self.dir)], check=True)
        return subprocess.run(['git', '-C', str(self.dir), 'rev-parse', 'HEAD'], check=True, capture_output=True,
                              text=True, env=git_env()).stdout.strip()

    def test_source_rev_is_the_clean_checkout_commit(self):
        head = self.checkout()
        r = self.run_sourced('source_rev')
        self.assertEqual(r.stdout.strip(), head, r.stderr)
        (self.dir / 'farm.toml').write_text((self.dir / 'farm.toml').read_text() + '\n')
        r = self.run_sourced('source_rev')
        self.assertEqual(r.returncode, 1)
        self.assertIn('holds changes no commit has', r.stderr)
        self.assertIn('farm.toml', r.stderr)
        subprocess.run(['git', '-C', str(self.dir), 'checkout', '-q', 'farm.toml'], check=True, env=git_env())
        os.chmod(self.dir / 'farm.toml', 0o644)
        (self.dir / 'extra.sh').write_text('')
        os.chmod(self.dir / 'extra.sh', 0o644)
        self.assertIn('?? checkout/extra.sh', self.run_sourced('source_rev').stderr)
        (self.dir / 'extra.sh').unlink()
        os.chmod(self.dir / 'farm.py', 0o664)
        r = self.run_sourced('source_rev')
        self.assertIn(f'{self.dir / "farm.py"} is a symlink, not root-owned or group/world-writable', r.stderr)
        os.chmod(self.dir / 'farm.py', 0o644)
        outside = self.tmp / 'outside.py'
        outside.write_text('')
        (self.dir / 'job_hook.py').unlink()
        (self.dir / 'job_hook.py').symlink_to(outside)   # committed as a link, it would pass git status
        subprocess.run(['git', '-C', str(self.tmp), 'commit', '-qam', 'l'], check=True, env=git_env())
        r = self.run_sourced('source_rev')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'{self.dir / "job_hook.py"} is a symlink', r.stderr)

    def test_source_rev_covers_the_host_file(self):
        hosts = self.tmp / 'hosts'
        hosts.mkdir()
        shutil.copy(HOSTS / 'dedicated.toml', hosts / 'lab.toml')
        head = self.checkout()
        subprocess.run(['git', '-C', str(self.tmp), 'add', 'hosts'], check=True, env=git_env())
        subprocess.run(['git', '-C', str(self.tmp), 'commit', '-qm', 'host'], check=True, env=git_env())
        subprocess.run(['chmod', '-R', 'go-w', str(hosts)], check=True)
        rev = lambda f: self.run_sourced(f'HOST_FILE={f}; source_rev')
        r = rev(hosts / 'lab.toml')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(r.stdout.strip(), head)
        (hosts / 'lab.toml').write_text((hosts / 'lab.toml').read_text() + '\n')
        r = rev(hosts / 'lab.toml')
        self.assertEqual(r.returncode, 1)
        self.assertIn('holds changes no commit has', r.stderr)
        self.assertIn('hosts/lab.toml', r.stderr)
        (hosts / 'new.toml').write_text('')
        os.chmod(hosts / 'new.toml', 0o644)
        self.assertIn('?? hosts/new.toml', rev(hosts / 'new.toml').stderr)
        os.chmod(hosts / 'new.toml', 0o666)
        self.assertIn(f'{hosts / "new.toml"} is a symlink, not root-owned or group/world-writable',
                      rev(hosts / 'new.toml').stderr)
        os.chmod(hosts / 'new.toml', 0o644)
        (self.tmp / '.git' / 'info' / 'exclude').write_text('hosts/new.toml\n')
        r = rev(hosts / 'new.toml')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'{hosts / "new.toml"} is not committed', r.stderr)
        r = rev(HOSTS / 'dedicated.toml')
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'--host {HOSTS / "dedicated.toml"} is not in {hosts}', r.stderr)

    def test_source_rev_needs_a_git_checkout_root_owns(self):
        r = self.run_sourced('source_rev')   # not a checkout, and this user's
        self.assertEqual(r.returncode, 1)
        self.assertIn(f'a non-root user could replace {self.dir}', r.stderr)
        self.trust_copy()
        self.assertIn(f'{self.dir} is not a git checkout', self.run_sourced('source_rev').stderr)

    def admin_key(self):
        """An admin, this test's user, whose home is under the temp dir; returns its home."""
        home = self.tmp / 'home' / 'admin'
        (home / '.ssh').mkdir(parents=True, mode=0o700)
        os.chmod(self.tmp / 'home', 0o755)
        os.chmod(home, 0o750)
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.tmp / 'k')], check=True)
        shutil.copy(self.tmp / 'k.pub', home / '.ssh' / 'authorized_keys')
        os.chmod(home / '.ssh' / 'authorized_keys', 0o600)
        host = self.tmp / 'admin.toml'
        host.write_text((HOSTS / 'dedicated.toml').read_text().replace('admin = "admin"',
                                                                       f'admin = "{getpass.getuser()}"'))
        self.host = host
        self.stub('getent', f'echo "$2:x:1000:1000::{home}:/bin/bash"')
        setup = self.dir / 'setup.sh'   # sshd's check stops at /; this one at the temp dir, which this user owns
        setup.write_text(setup.read_text().replace('[ "$d" != / ] || break', f'[ "$d" != {self.tmp} ] || break')
                         .replace('[ "$(stat -c %U "$d")" = root ]', f'[ "$(stat -c %U "$d")" = {getpass.getuser()} ]'))
        return home

    def test_admin_key_must_be_one_sshd_uses(self):
        home = self.admin_key()
        keys = home / '.ssh' / 'authorized_keys'
        self.assertEqual(self.run_sourced('check_admin_key').returncode, 0, self.run_sourced('check_admin_key').stderr)
        for change, undo, msg in (
                (lambda: os.chmod(home / '.ssh', 0o770), lambda: os.chmod(home / '.ssh', 0o700),
                 f'{home / ".ssh"} is group/world-writable'),
                (lambda: os.chmod(keys, 0o622), lambda: os.chmod(keys, 0o600), f'{keys} is group/world-writable'),
                (lambda: os.chmod(self.tmp / 'home', 0o775), lambda: os.chmod(self.tmp / 'home', 0o755),
                 f'{self.tmp / "home"}, above {home}, is not root-owned'),
                (lambda: keys.write_text('not a key\n'), lambda: shutil.copy(self.tmp / 'k.pub', keys),
                 'holds no valid key'),
                (lambda: shutil.copy(self.tmp / 'k', keys), lambda: shutil.copy(self.tmp / 'k.pub', keys),
                 'holds no valid key'),   # ssh-keygen -lf alone accepts a private key
                (lambda: keys.write_text('# only a comment\n\n'), lambda: shutil.copy(self.tmp / 'k.pub', keys),
                 'holds no valid key'),
                (lambda: keys.write_text((self.tmp / 'k.pub').read_text() + 'junk\n'),
                 lambda: shutil.copy(self.tmp / 'k.pub', keys), 'a line that is not one'),
                (lambda: keys.rename(home / 'k'), lambda: (home / 'k').rename(keys),
                 'missing; give'),
                (lambda: (keys.rename(home / 'k'), keys.symlink_to(home / 'k')),
                 lambda: (keys.unlink(), (home / 'k').rename(keys)), 'missing; give')):
            with self.subTest(msg=msg):
                change()
                r = self.run_sourced('check_admin_key')
                undo()
                self.assertEqual(r.returncode, 1)
                self.assertIn(msg, r.stderr)
        self.assertEqual(self.run_sourced('check_admin_key').returncode, 0)

    def test_install_checks_the_admin_key_before_any_change_on_a_dedicated_host_only(self):
        log = self.tmp / 'order.log'
        setup = self.dir / 'setup.sh'
        setup.write_text(setup.read_text().replace('preflight() {', 'preflight() { return 0; }\nunused() {')
                         .replace('check_admin_key() {', f'check_admin_key() {{ echo key >> {log}; die nokey; }}\nx() {{')
                         .replace('farm() { python3 -I "$B/farm.py" "$@"; }',
                                  f'farm() {{ echo "farm $1" >> {log}; return 0; }}'))
        r = self.setup('install')
        self.assertIn('nokey', r.stderr)
        self.assertEqual(log.read_text().splitlines(), ['key'])
        log.unlink()
        self.use_host('shared')
        setup.write_text(setup.read_text().replace('    install_host\n', '    install_host\n    exit 0\n', 1))
        self.setup('install')
        self.assertEqual(log.read_text().splitlines(), ['farm guard'])

    def install_packages(self, host):
        """install_packages with apt stubbed; whether it wrote the farm's 20auto-upgrades."""
        self.use_host(host)
        setup = self.dir / 'setup.sh'
        setup.write_text(setup.read_text().replace('/etc/apt/apt.conf.d', str(self.tmp)))
        for c in ('apt-get', 'debconf-set-selections'):
            self.stub(c, ':')
        self.sourced('install_packages')
        return (self.tmp / '20auto-upgrades').exists()

    def test_auto_upgrades_follow_the_package_list(self):
        self.assertTrue(self.install_packages('dedicated'))

    def test_a_shared_host_keeps_the_owner_update_policy(self):
        self.assertFalse(self.install_packages('shared'))

    def shared_firewall(self, dropins='', frag='/usr/lib/systemd/system/nftables.service', dpkg='', dpkg_rc=1,
                        enabled=0):
        etc = self.tmp / 'etc'
        common = self.dir / 'common.sh'
        common.write_text(common.read_text().replace('/etc/systemd/system/nftables.service.d', str(etc / 'nft.d')))
        setup = self.dir / 'setup.sh'
        setup.write_text(setup.read_text().replace('/etc/hil-farm', str(etc / 'hil-farm')))
        log = self.tmp / 'calls.log'
        self.stub('systemctl', f'echo "systemctl $*" >> {log}; [ "$1" != is-enabled ] || exit {enabled}; '
                               f'[ "$1 $2 $3" != "show -p DropInPaths" ] || echo "{dropins}"; '
                               f'[ "$1 $2 $3" != "show -p FragmentPath" ] || echo "{frag}"')
        self.stub('nft', f'echo "nft $*" >> {log}')
        self.stub('dpkg', f'printf "{dpkg}"; [ -z "{dpkg}" ] || exit {dpkg_rc}')
        self.use_host('shared')
        r = self.run_sourced('shared_firewall 192.0.2.53')
        return r, etc, log.read_text().splitlines() if log.exists() else []

    def assert_firewall_refused(self, msg, **kw):
        r, etc, calls = self.shared_firewall(**kw)
        self.assertEqual(r.returncode, 1)
        self.assertIn(msg, r.stderr)
        self.assertFalse((etc / 'nft.d' / 'hil-farm.conf').exists())
        self.assertFalse([c for c in calls if c.startswith('nft')])

    def test_shared_firewall_loads_the_owner_rules_and_ours_together(self):
        r, etc, calls = self.shared_firewall()
        self.assertEqual(r.returncode, 0, r.stderr)
        ours = (etc / 'hil-farm' / 'hil_farm.nft').read_text()
        self.assertTrue(ours.startswith('table inet hil_farm {}\ndelete table inet hil_farm\ntable inet hil_farm {\n'))
        self.assertNotIn('flush', ours)
        self.assertIn('    meta skuid != { "ghrunner" } accept\n', ours)
        self.assertEqual((etc / 'hil-farm' / 'load.nft').read_text(),
                         f'include "/etc/nftables.conf"\ninclude "{etc}/hil-farm/hil_farm.nft"\n')
        self.assertEqual((etc / 'nft.d' / 'hil-farm.conf').read_text(),
                         f'[Service]\nExecStart=\nExecStart=/usr/sbin/nft -f {etc}/hil-farm/load.nft\n'
                         f'ExecReload=\nExecReload=/usr/sbin/nft -f {etc}/hil-farm/load.nft\n')
        self.assertIn(f'nft -c -f {etc}/hil-farm/load.nft', calls)
        self.assertNotIn('systemctl enable -q nftables', calls)
        self.assertEqual(calls[-1], 'systemctl reload-or-restart nftables')

    def test_shared_firewall_tolerates_the_owners_edited_conf(self):
        r, etc, calls = self.shared_firewall(dpkg='??5?????? c /etc/nftables.conf\\n')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_shared_firewall_refuses_a_disabled_firewall(self):
        self.assert_firewall_refused('nftables.service is not enabled', enabled=1)

    def test_shared_firewall_refuses_foreign_drop_ins(self):
        self.assert_firewall_refused('drop-ins this setup would override: /etc/systemd/system/nftables.service.d/owner.conf',
                                     dropins='/etc/systemd/system/nftables.service.d/owner.conf')

    def test_shared_firewall_refuses_a_replaced_unit(self):
        self.assert_firewall_refused('comes from /etc/systemd/system', frag='/etc/systemd/system/nftables.service')

    def test_shared_firewall_refuses_an_edited_unit(self):
        self.assert_firewall_refused("Debian's nftables.service was edited",
                                     dpkg='??5?????? c /etc/nftables.conf\\n??5??????   /usr/lib/systemd/system/nftables.service\\n')

    def test_shared_firewall_refuses_an_unverifiable_package(self):
        self.assert_firewall_refused('cannot verify the nftables package (dpkg -V exited 2)',
                                     dpkg='dpkg: error: database locked\\n', dpkg_rc=2)

    def test_a_shared_host_ssh_denies_the_runner_users_only(self):
        self.use_host('shared')
        conf = self.tmp / 'sshd.d'
        conf.mkdir()
        setup = self.dir / 'setup.sh'
        setup.write_text(setup.read_text().replace('/etc/ssh/sshd_config.d', str(conf)))
        self.stub('systemctl', ':')
        for denies, rc in (('denyusers ghrunner', 0), ('denyusers other', 1)):
            with self.subTest(denies=denies):
                self.stub('sshd', f'[ "$1" = -t ] || echo "{denies}"')
                r = self.run_sourced('setup_ssh')
                self.assertEqual(r.returncode, rc, r.stderr)
                self.assertEqual((conf / '10-hil-farm.conf').read_text(), 'DenyUsers ghrunner\n')

    def test_attached_devices_get_the_rules_or_setup_fails(self):
        self.stub('timeout', 'shift; exec "$@"')
        self.stub('udevadm', 'echo "$*" > "$0.log"')
        self.sourced('apply_rules')
        self.assertEqual((self.stubs / 'udevadm.log').read_text(),
                         'trigger --settle --action=change --subsystem-match=usb --subsystem-match=hidraw '
                         '--subsystem-match=tty --subsystem-match=usbmon\n')
        self.stub('udevadm', 'exit 1')
        r = self.run_sourced('apply_rules; echo after')
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn('after', r.stdout)
        self.assertIn('udev did not apply the rules to the attached devices', r.stderr)

    def test_runner_conf(self):
        out = self.sourced('for n in tinyusb tinyuf2; do runner_conf $n; '
                           'echo "$RUNNER_USER $RUNNER_DIR $RUNNER_UNIT $RUNNER_URL $RUNNER_LABELS [$RUNNER_SUDO] '
                           '[$RUNNER_STATE]"; '
                           'done; runner_conf nope || echo refused')
        self.assertEqual(out.splitlines(), [
            'ghrunner /opt/actions-runner/tinyusb hil-runner-tinyusb https://github.com/hathach/tinyusb '
            'hardware-in-the-loop,lab [/usr/local/sbin/hil_root] []',
            'ghrunner-tinyuf2 /opt/actions-runner/tinyuf2 hil-runner-tinyuf2 https://github.com/adafruit/tinyuf2 '
            'tinyuf2 [] [_work/tinyuf2/hil-state]',
            'refused'])


def setUpModule():
    guard_survivors(__name__)


if __name__ == '__main__':
    unittest.main()
