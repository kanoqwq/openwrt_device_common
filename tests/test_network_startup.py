#!/usr/bin/env python3
"""Check startup patching and actual netifd EPS branches without modem access.

Run on Linux: test_network_startup.py <original modemmanager.sh> <original modemmanager.js>
"""
from pathlib import Path
import os
import runpy
import subprocess
import sys
import tempfile
import unittest

PROTO, LUCI = [Path(p).read_text() for p in sys.argv[1:3]]
del sys.argv[1:3]
HERE = Path(__file__).resolve().parent
patch_root = runpy.run_path(str(HERE / '../shared/network-startup.py'))['patch_root']
LOADER = (HERE.parents[0] / 'overlay/etc/init.d/e5-sipc-wwan').read_text()


class Startup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='t551m-network-test-')
        self.root = Path(self.tmp.name)
        policy = self.root / 'etc/e5/radio-defaults.json'
        policy.parent.mkdir(parents=True)
        policy.write_text('{"auto_work_mode":"152","lte_supported_bands":"1,3,5,8,34,39,40,41"}')
        for name, text in [('etc/init.d/e5-sipc-wwan', LOADER),
                           ('lib/netifd/proto/modemmanager.sh', PROTO),
                           ('www/luci-static/resources/protocol/modemmanager.js', LUCI)]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        patch_root(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_and_loader_preserves_sim_selection(self):
        files = [p for p in self.root.rglob('*') if p.is_file()]
        before = {p: p.read_bytes() for p in files}
        patch_root(self.root)
        self.assertEqual(before, {p: p.read_bytes() for p in files})
        text = (self.root / 'etc/init.d/e5-sipc-wwan').read_text()
        self.assertIn('card=$C tx_gap_ms=100', text)
        self.assertIn('e5-sim-probe /dev/stty_nr1 "$C"', text)
        self.assertIn('[ $C = 0 ] && insmod $M/sipc_wwan.ko tx_gap_ms=100;', text)
        self.assertIn("o.value('keep'", (self.root / 'www/luci-static/resources/protocol/modemmanager.js').read_text())
        subprocess.run(['busybox', 'sh', '-n', str(self.root / 'etc/init.d/e5-sipc-wwan')], check=True)

    def test_keep_skips_radio_cycle_and_existing_modes_still_apply(self):
        text = (self.root / 'lib/netifd/proto/modemmanager.sh').read_text()
        subprocess.run(['busybox', 'sh', '-n', str(self.root / 'lib/netifd/proto/modemmanager.sh')], check=True)
        start = text.index('\t# set initial eps bearer settings')
        end = text.index('\tif [ -z "${allowedmode}" ]', start)
        harness = '''
modemmanager_init_epsbearer() { printf 'EPS:%s:%s\\n' "$1" "$3"; }
append_param() { [ -z "$connectargs" ] || connectargs="$connectargs,"; connectargs="$connectargs$1"; }
run_eps() {
''' + text[start:end] + '\n: # normal setup continues to mode/PLMN and simple-connect\n}\nrun_eps\n'
        for mode in ('keep', '', 'default', 'custom'):
            with self.subTest(mode=mode):
                env = dict(os.environ, init_epsbearer=mode, device='fake', apn='internet',
                           init_apn='private', iptype='ipv4', init_iptype='ipv4v6',
                           allowedauth='pap', init_allowedauth='chap', username='user', password='pass')
                out = subprocess.run(['busybox', 'sh', '-c', harness], env=env,
                                     capture_output=True, text=True, check=True).stdout
                if mode == 'keep':
                    self.assertNotIn('EPS:', out)
                elif not mode:
                    self.assertEqual(out, 'EPS:none:\n')
                elif mode == 'default':
                    self.assertIn('EPS:default:apn=internet,ip-type=ipv4,allowed-auth=pap,user=user,password=pass', out)
                else:
                    self.assertIn('EPS:custom:apn=private,ip-type=ipv4v6,allowed-auth=chap', out)
        self.assertIn('mmcli --modem="${device}" --timeout 120 --simple-connect="${connectargs}"', text)

    def test_migration_only_changes_original_default_once(self):
        script = (HERE / 'network-startup-defaults').read_text()
        marker = self.root / 'migrated'
        script = script.replace('/etc/e5/t551m-network-startup-v1', str(marker))
        harness = '''
cat() { echo t551m; }
python3() { :; }
uci() {
    case "$*" in
        '-q get network.wan.proto') echo modemmanager;;
        '-q get network.wan.init_epsbearer') echo "$MODE";;
        *) echo "$*";;
    esac
}
'''
        for mode in ('custom', '', 'default'):
            marker.unlink(missing_ok=True)
            env = dict(os.environ, MODE=mode)
            out = subprocess.run(['busybox', 'sh', '-c', harness + script], env=env,
                                 capture_output=True, text=True, check=True).stdout
            self.assertEqual('set network.wan.init_epsbearer=keep' in out, mode == 'default')
            again = subprocess.run(['busybox', 'sh', '-c', harness + script], env=env,
                                   capture_output=True, text=True, check=True).stdout
            self.assertEqual(again, '')

    def test_default_dial_does_not_overwrite_pre_dial_mode(self):
        text = (self.root / 'lib/netifd/proto/modemmanager.sh').read_text()
        start = text.index('\tif [ -z "${allowedmode}" ]')
        end = text.index('\tif [ -z "${plmn}" ]', start)
        harness = '''
modemmanager_set_allowed_mode() { echo SET:$3; }
modemmanager_set_preferred_mode() { echo PREFERRED:$3:$4; }
run_modes() {
''' + text[start:end] + '\n: # real setup continues to PLMN and simple-connect\n}\nrun_modes\n'
        for mode in ('', '4g', 'any'):
            out = subprocess.run(['busybox', 'sh', '-c', harness],
                                 env=dict(os.environ, allowedmode=mode), capture_output=True, text=True, check=True).stdout
            if not mode:
                self.assertNotIn('SET:', out)
                self.assertIn('Keeping current network mode', out)
            else:
                self.assertEqual(out.strip(), 'SET:'+mode)

    def test_pdp_retry_shared_across_profiles(self):
        path = self.root / 'lib/netifd/proto/modemmanager.sh'
        for profile in ('e5', 't551m', 'm8'):
            with self.subTest(profile=profile):
                path.write_text(PROTO)
                patch_root(self.root, profile)
                self.assertEqual(path.read_text().count('# unisoc-pdp-connect-retry:'), 1)
                self.assertIn('modemmanager_connect_retry "$device" "$connectargs" || {', path.read_text())
                before = path.read_bytes()
                patch_root(self.root, profile)
                self.assertEqual(path.read_bytes(), before)
                self.test_pdp_retry_is_bounded_and_specific()

    def test_pdp_retry_is_bounded_and_specific(self):
        text = (self.root / 'lib/netifd/proto/modemmanager.sh').read_text()
        helper = text[text.index('# unisoc-pdp-connect-retry:'):text.index('proto_modemmanager_setup() {')]
        harness = '''
mmcli() {
    n=$(cat "$COUNT" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$COUNT"
    if [ "$n" -gt "$FAILURES" ]; then echo connected; return 0; fi
    echo "$ERROR" >&2; return 1
}
sleep() { echo "WAIT:$1"; }
''' + helper + '\nmodemmanager_connect_retry "$DEVICE" "apn=test,ip-type=ipv4v6"\n'
        error = "error: couldn't connect the modem: 'GDBus.Error:Unknown: Couldn't activate PDP context 1: Unknown mobile equipment error: 28'"
        cases = [(0, error, 'unisoc-sipc', 1, [], 0),
                 (1, error, 'unisoc-sipc', 2, ['WAIT:5'], 0),
                 (2, error, 'unisoc-sipc', 3, ['WAIT:5', 'WAIT:10'], 0),
                 (9, error, 'unisoc-sipc', 3, ['WAIT:5', 'WAIT:10'], 1),
                 (9, 'SIM PIN required', 'unisoc-sipc', 1, [], 1),
                 (9, error.replace("28'", "29'"), 'unisoc-sipc', 1, [], 1),
                 (9, error, '/dev/ttyUSB0', 1, [], 1)]
        count = self.root / 'attempts'
        for failures, message, device, expected, waits, rc in cases:
            with self.subTest(failures=failures, message=message, device=device):
                count.unlink(missing_ok=True)
                env = dict(os.environ, COUNT=str(count), FAILURES=str(failures), ERROR=message, DEVICE=device)
                result = subprocess.run(['busybox', 'sh', '-c', harness], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, rc, result.stderr)
                self.assertEqual(int(count.read_text()), expected)
                self.assertEqual([line for line in result.stdout.splitlines() if line.startswith('WAIT:')], waits)

    def test_unknown_package_layout_refused(self):
        path = self.root / 'lib/netifd/proto/modemmanager.sh'
        path.write_text('# changed upstream interface\n')
        with self.assertRaisesRegex(ValueError, 'Unexpected ModemManager'):
            patch_root(self.root)

    def test_modem_auto_mode_profile_boundary_and_explicit_override(self):
        path = self.root / 'etc/config/unisoc_modem'
        path.parent.mkdir(parents=True, exist_ok=True)
        original = "config core 'main'\n\toption device '/dev/ttyUSB0'\n"
        path.write_text(original)
        patch_root(self.root, 'e5')
        self.assertEqual(path.read_text(), original)
        patch_root(self.root, 'm8')
        self.assertEqual(path.read_text(), original)
        patch_root(self.root, 't551m')
        configured = path.read_text()
        self.assertIn("option auto_work_mode '152'", configured)
        patch_root(self.root, 't551m')
        self.assertEqual(path.read_text(), configured)
        path.write_text(configured.replace("auto_work_mode '152'", "auto_work_mode '134'"))
        patch_root(self.root, 't551m')
        self.assertIn("option auto_work_mode '134'", path.read_text())


if __name__ == '__main__':
    unittest.main()
