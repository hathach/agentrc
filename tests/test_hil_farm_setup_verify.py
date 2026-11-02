"""verify.sh's own decisions, with the system probes it branches on stubbed; the probes themselves need the farm."""
import os
import unittest

if os.name == 'nt':
    raise unittest.SkipTest('the HIL farm setup runs on Debian: bash, fcntl, Linux paths')

import getpass
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import guard_survivors  # noqa: E402

FARM = Path(__file__).resolve().parent.parent / 'skills' / 'hil-farm-setup' / 'scripts'
HOSTS = Path(__file__).resolve().parent / 'fixtures' / 'hil_farm_setup'
# commands verify.sh calls that must not touch this machine, or need root, in a test
INERT = ['sudo', 'runuser', 'curl', 'nft', 'sshd', 'passwd', 'lsusb', 'dfu-util', 'uhubctl']


@unittest.skipIf('PATH=' in Path('/etc/environment').read_text(), '/etc/environment would replace the stub PATH')
class TestVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dir = self.tmp / 'farm'
        self.dir.mkdir()
        (self.dir / 'fw').mkdir()
        self.stub_check('echo "ok    check ran"')
        for f in ['verify.sh', 'common.sh', 'farm.py', 'farm.toml', 'requirements.lock', 'pyocd.lock']:
            shutil.copy(FARM / f, self.dir)
        self.runners = self.tmp / 'runners'
        common = self.dir / 'common.sh'
        common.write_text(common.read_text().replace('/opt/actions-runner/', f'{self.runners}/'))
        self.sysfs = self.tmp / 'sysfs'
        self.sysfs.mkdir()
        v = self.dir / 'verify.sh'
        self.devusb = self.tmp / 'dev-bus-usb'
        v.write_text(v.read_text().replace('/sys/bus/usb/devices/', f'{self.sysfs}/')
                     .replace('/dev/bus/usb/', f'{self.devusb}/'))
        self.stubs = self.tmp / 'stubs'
        self.stubs.mkdir()
        self.stub('id', 'if [ "$1" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi')
        self.stub('systemctl', 'echo inactive; exit 3')
        self.stub('pgrep', 'exit 1')
        self.stub('getent', '[ "$1" = passwd ] && echo "$2:x:1000:1000::/home/$2:/bin/bash"')
        self.host = HOSTS / 'dedicated.toml'
        for c in INERT:
            self.stub(c, 'exit 1')

    def stub(self, name, body):
        p = self.stubs / name
        p.write_text(f'#!/bin/sh\n{body}\n')
        p.chmod(0o755)

    def verify(self, *args, cwd=None):
        env = {**os.environ, 'PATH': f'{self.stubs}:{os.environ["PATH"]}', 'HIL_FARM_HOST': str(self.host)}
        return subprocess.run(['bash', str(self.dir / 'verify.sh'), *args], capture_output=True, text=True,
                              env=env, timeout=300, cwd=cwd)

    def stub_check(self, body):
        g = self.dir / 'fw' / 'renesas_fw_check.sh'
        g.write_text(f'#!/bin/sh\n{body}\n')
        g.chmod(0o755)

    def test_a_passing_check_adds_no_failure(self):
        out = self.verify().stdout
        self.assertIn('ok    check ran\n', out)
        self.assertNotIn('Renesas firmware', out)

    def test_the_check_output_passes_through_and_its_exit_fails(self):
        for body in ('echo "ok    x"; echo "FAIL  a"; exit 1', 'echo "ok    x"; exit 3'):
            with self.subTest(body=body):
                self.stub_check(body)
                r = self.verify()
                self.assertIn('ok    x\n', r.stdout)
                self.assertEqual('FAIL  a\n' in r.stdout, 'FAIL  a' in body)
                self.assertIn('FAIL  Renesas firmware (fw/renesas_fw_check.sh)', r.stdout)
                self.assertNotEqual(r.returncode, 0)

    def test_a_missing_check_fails(self):
        (self.dir / 'fw' / 'renesas_fw_check.sh').unlink()
        self.assertIn('FAIL  Renesas firmware (fw/renesas_fw_check.sh)', self.verify().stdout)

    def test_checks_run_from_root_not_the_callers_directory(self):
        home = self.tmp / 'home'
        home.mkdir()
        (home / 'hid.py').write_text('raise ImportError("the admin\'s hid.py")\n')
        self.stub('dfu-util', 'pwd > "$0.log"')
        self.verify(cwd=home)
        self.assertEqual((self.stubs / 'dfu-util.log').read_text(), '/\n')

    def test_each_runner_must_open_every_probe_node(self):
        for name, vid, bus, dev in (('3-1', '1366', '3', '7'), ('13-1.3', '0483', '13', '84'), ('1-2', '046d', '1', '2')):
            d = self.sysfs / name
            d.mkdir()
            for f, val in (('idVendor', vid), ('busnum', bus), ('devnum', dev)):
                (d / f).write_text(val + '\n')
            node = self.devusb / f'{int(bus):03d}' / f'{int(dev):03d}'
            node.parent.mkdir(parents=True, exist_ok=True)
            node.touch(mode=0o666)
        self.stub('runuser', 'shift 3; exec "$@"')  # runuser -u USER -- CMD: as this user
        out = self.verify().stdout
        self.assertIn('ok    ghrunner opens all 2 probes\n', out)
        self.assertIn('ok    ghrunner-tinyuf2 opens all 2 probes\n', out)
        (self.devusb / '013' / '084').chmod(0o444)  # a group 0664 node seen by another user
        out = self.verify().stdout
        self.assertIn(f'FAIL  ghrunner opens all 2 probes: cannot open: {self.devusb}/013/084\n', out)
        self.assertNotIn('001/002', out)

    def test_unreadable_config_stops_verify(self):
        (self.dir / 'farm.toml').write_text('[tool.x]\nkind = "nope"\n')
        r = self.verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn("x: unknown kind 'nope'", r.stderr)

    def test_no_host_file_stops_verify(self):
        self.host = self.tmp / 'none.toml'
        r = self.verify()
        self.assertEqual(r.returncode, 2)
        self.assertIn(f'no host file {self.host}', r.stderr)
        self.assertEqual(r.stdout, '')

    def register(self, name, hooks_of=None):
        """A registered runner root whose .env names `hooks_of`'s job hooks (its own by default)."""
        d = self.runners / name
        for sub in ('_work', '_diag'):
            (d / sub).mkdir(parents=True)
        (d / '.runner').write_text('{}')
        (d / '.credentials').write_text('{}')
        h = hooks_of or name
        (d / '.env').write_text(f'ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/libexec/hil-farm/job-started-{h}.sh\n'
                                f'ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/usr/local/libexec/hil-farm/job-completed-{h}.sh\n')
        path = subprocess.run(['python3', '-I', str(self.dir / 'farm.py'), 'path'], capture_output=True, text=True)
        (d / '.path').write_text(path.stdout)
        return d

    def test_maintenance_requires_a_stopped_runner(self):
        self.assertIn('ok    runners stopped, no job (maintenance)', self.verify('--maintenance').stdout)
        self.stub('systemctl', 'echo active')
        self.assertIn('FAIL  runners stopped, no job (maintenance)', self.verify('--maintenance').stdout)
        self.stub('systemctl', '[ "$2" = hil-runner-tinyuf2 ] && echo active || { echo inactive; exit 3; }')
        self.assertIn('FAIL  runners stopped, no job (maintenance)', self.verify('--maintenance').stdout)
        self.stub('systemctl', 'echo inactive; exit 3')
        self.stub('pgrep', 'echo 4242')
        self.assertIn('FAIL  runners stopped, no job (maintenance)', self.verify('--maintenance').stdout)

    def test_sudo_is_hil_root_for_ghrunner_and_nothing_for_tinyuf2(self):
        allowed = 'User ghrunner may run the following commands on ci:\n    (root) NOPASSWD: /usr/local/sbin/hil_root'
        denied = 'User ghrunner-tinyuf2 is not allowed to run sudo on ci.'
        self.stub('sudo', f'[ "$3" = ghrunner ] && printf "{allowed}\\n" || echo "{denied}"')
        out = self.verify().stdout
        self.assertIn('ok    ghrunner sudo is /usr/local/sbin/hil_root only\n', out)
        self.assertIn('ok    ghrunner-tinyuf2 has no sudo\n', out)
        self.stub('sudo', f'printf "{allowed}\\n"')
        out = self.verify().stdout
        self.assertIn('FAIL  ghrunner-tinyuf2 sudo: ', out)
        self.stub('sudo', 'echo "sudo: unknown user ghrunner-tinyuf2"; exit 1')
        self.assertIn('FAIL  ghrunner-tinyuf2 sudo: sudo: unknown user', self.verify().stdout)

    def test_password_check_fails_when_passwd_says_nothing(self):
        out = self.verify().stdout
        self.assertIn('FAIL  ghrunner password locked', out)
        self.stub('passwd', 'echo "$2 L 2026-10-28 0 99999 7 -1"')
        self.assertIn('ok    ghrunner-tinyuf2 password locked', self.verify().stdout)

    def test_each_user_is_checked_against_the_firewall(self):
        out = self.verify().stdout
        for u in ('ghrunner', 'ghrunner-tinyuf2'):
            self.assertIn(f'FAIL  {u} reaches github', out)
            self.assertRegex(out, f'FAIL  {u} to \\S* not rejected by the hil_farm table')

    def test_unregistered_runners_are_skipped(self):
        out = self.verify().stdout
        self.assertIn('skip  tinyusb runner not registered yet (setup.sh runner tinyusb <token>)', out)
        self.assertIn('skip  tinyuf2 runner not registered yet (setup.sh runner tinyuf2 <token>)', out)

    def test_a_registered_runner_is_checked_with_its_own_unit_and_hooks(self):
        self.register('tinyusb')
        self.register('tinyuf2', hooks_of='tinyusb')
        self.stub('systemctl', '[ "$3" = hil-runner-tinyusb ]')
        out = self.verify().stdout
        self.assertIn('ok    tinyusb runner service active\n', out)
        self.assertIn('FAIL  tinyuf2 runner service active', out)
        self.assertIn('ok    tinyusb runner .env sets ACTIONS_RUNNER_HOOK_JOB_STARTED\n', out)
        self.assertIn('ok    tinyusb runner .env sets ACTIONS_RUNNER_HOOK_JOB_COMPLETED\n', out)
        self.assertIn('FAIL  tinyuf2 runner .env sets ACTIONS_RUNNER_HOOK_JOB_STARTED', out)
        self.assertIn('FAIL  tinyuf2 runner .env sets ACTIONS_RUNNER_HOOK_JOB_COMPLETED', out)
        self.assertIn('FAIL  tinyusb runner .env sets LANG', out)   # register() writes the hook lines only
        self.assertIn('ok    tinyusb runner .path is farm.toml\'s\n', out)
        # this test's files are not root-owned
        self.assertIn('FAIL  tinyusb runner root sticky root:ghrunner 1770', out)
        self.assertIn('FAIL  tinyuf2 runner _work ghrunner-tinyuf2:ghrunner-tinyuf2 0750', out)
        self.assertNotIn('runner service active', self.verify('--maintenance').stdout)

    def test_nothing_in_a_runner_tree_may_be_the_admins(self):
        d = self.register('tinyusb')
        for admin, want in (('root', 'ok   '), (getpass.getuser(), 'FAIL ')):
            with self.subTest(admin=admin):
                self.host = self.tmp / f'{admin}.toml'
                self.host.write_text((HOSTS / 'dedicated.toml').read_text()
                                     .replace('admin = "admin"', f'admin = "{admin}"'))
                out = self.verify().stdout
                self.assertIn(f'{want} tinyusb runner _work and _diag hold nothing of {admin}\'s', out)
        self.assertRegex(out, f'hold nothing of {getpass.getuser()}\'s: {d}/_(work|diag)')

    def test_a_runner_unit_must_keep_umask_0022(self):
        self.register('tinyusb')
        for umask, want in (('0022', 'ok   '), ('0002', 'FAIL ')):
            with self.subTest(umask=umask):
                self.stub('systemctl', f'[ "$1" = show ] && [ "$3" = UMask ] && echo {umask}')
                self.assertIn(f'{want} tinyusb runner unit UMask 0022', self.verify().stdout)

    def test_env_lines_that_cannot_be_generated_fail(self):
        self.register('tinyusb')
        with open(self.dir / 'common.sh', 'a') as f:
            f.write('runner_env() { echo LANG=C.UTF-8; return 1; }\n')
        out = self.verify().stdout
        self.assertIn('FAIL  tinyusb runner .env: cannot generate its lines', out)
        self.assertNotIn('tinyusb runner .env sets', out)
        with open(self.dir / 'common.sh', 'a') as f:
            f.write('runner_env() { :; }\n')
        self.assertIn('FAIL  tinyusb runner .env: cannot generate its lines', self.verify().stdout)

    def test_cross_runner_reads_fail_and_the_admins_pass(self):
        tinyusb = self.register('tinyusb')
        tinyuf2 = self.register('tinyuf2')
        self.stub('runuser', 'shift 3; exec "$@"')  # this test's user reads everything
        out = self.verify().stdout
        self.assertIn('ok    admin reads tinyusb runner _work\n', out)
        self.assertIn('ok    admin reads tinyuf2 runner _work\n', out)
        for user, d in (('ghrunner-tinyuf2', tinyusb), ('ghrunner', tinyuf2)):
            for f in (d, d / '.credentials', d / '_work', d / '_diag'):
                self.assertIn(f'FAIL  {user} cannot read {f}', out)
        self.stub('runuser', 'exit 1')  # nobody reads anything
        out = self.verify().stdout
        self.assertIn('FAIL  admin reads tinyuf2 runner _work', out)
        self.assertIn(f'ok    ghrunner cannot read {tinyuf2 / ".credentials"}\n', out)
        self.assertIn(f'ok    ghrunner-tinyuf2 cannot read {tinyusb}\n', out)

    def use_shared(self, trusted=True):
        """The shared host, with the farm's PATH (which verify.sh then uses) led by this test's stubs."""
        self.host = HOSTS / 'shared.toml'
        toml = self.dir / 'farm.toml'
        toml.write_text(toml.read_text().replace('path = "/opt/hil/venv/bin:', f'path = "{self.stubs}:/opt/hil/venv/bin:'))
        # the stubs are not root's, so farm trusted refuses them: stand in for its verdict on the runner PATH
        v = self.dir / 'verify.sh'
        v.write_text(v.read_text().replace('out=$(farm trusted "${runner_dirs[@]}" 2>&1)',
                                           'out=$(true)' if trusted else 'out=$(echo "x is writable"; false)'))

    def test_a_shared_host_checks_the_owner_boundary_instead_of_the_farm_host_policy(self):
        self.use_shared()
        self.stub_check('echo "FAIL  renesas"; exit 1')
        self.stub('iccarm', 'exit 1')  # whatever this machine has installed
        d = self.register('tinyusb')
        path = subprocess.run(['python3', '-I', str(self.dir / 'farm.py'), 'runner-path'], capture_output=True, text=True,
                              env={**os.environ, 'HIL_FARM_HOST': str(self.host)})
        (d / '.path').write_text(path.stdout)
        out = self.verify().stdout
        self.assertNotIn('enesas', out)
        self.assertNotIn('sshd: want', out)
        self.assertNotIn('ok    sshd ', out)
        self.assertNotIn('/etc/environment', out)
        self.assertNotIn('tinyuf2', out)
        self.assertIn("FAIL  nftables loads the owner's rules with hil_farm (drop-in only)", out)
        self.assertIn('FAIL  sshd denies ghrunner', out)
        self.assertIn('FAIL  iccarm on the runner PATH', out)
        self.assertIn("ok    tinyusb runner .path is farm.toml's\n", out)
        self.assertIn('FAIL  tinyusb runner unit hides /home/owner', out)
        self.assertIn('owner reads tinyusb runner _work', out)

    def test_a_shared_host_drop_in_and_hidden_home_pass_when_systemd_reports_them(self):
        self.use_shared()
        self.register('tinyusb')
        self.stub('systemctl', '[ "$1" = show ] || { echo inactive; exit 3; }; case "$5" in '
                               'nftables) echo /etc/systemd/system/nftables.service.d/hil-farm.conf ;; '
                               '*) echo /home/owner ;; esac')
        out = self.verify().stdout
        self.assertIn("ok    nftables loads the owner's rules with hil_farm (drop-in only)\n", out)
        self.assertIn('ok    tinyusb runner unit hides /home/owner\n', out)
        self.stub('sshd', 'echo "denyusers ghrunner"')
        # the licence check fails here as it does outside a job: no IAR_LMS_BEARER_TOKEN
        self.stub('iccarm', 'echo "IAR ANSI C/C++ Compiler V9.60.4"; echo "Fatal error detected, aborting."; exit 1')
        for v, want in (('3.31.6', 'ok   '), ('3.20.0', 'ok   '), ('3.19.8', 'FAIL ')):
            with self.subTest(cmake=v):
                self.stub('cmake', f'echo "cmake version {v}"')
                out = self.verify().stdout
                self.assertIn(f'{want} cmake >= 3.20 on the runner PATH', out)
        self.assertIn('ok    sshd denies ghrunner\n', out)
        self.assertIn('ok    iccarm on the runner PATH\n', out)

    def test_an_untrusted_runner_path_runs_none_of_its_tools(self):
        self.use_shared(trusted=False)
        self.stub('iccarm', 'echo ran >> "$0.log"')
        out = self.verify().stdout
        self.assertIn('FAIL  runner PATH writable by root only, so none of its tools run here: x is writable', out)
        self.assertNotIn('iccarm', out)
        self.assertFalse((self.stubs / 'iccarm.log').exists())

    def test_normal_mode_skips_the_maintenance_check_and_a_dedicated_host_the_shared_host_checks(self):
        out = self.verify().stdout
        self.assertNotIn('maintenance', out)
        self.assertNotIn('drop-in', out)
        self.assertNotIn('sshd denies', out)
        self.assertNotIn('iccarm', out)
        self.assertNotIn('hides /home', out)

    def test_bad_argument(self):
        self.assertEqual(self.verify('--nope').returncode, 2)


def setUpModule():
    guard_survivors(__name__)


if __name__ == '__main__':
    unittest.main()
