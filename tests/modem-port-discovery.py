#!/usr/bin/env python3
"""Real shell metadata notification in an isolated fake sysfs; never sends AT."""
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest

TOP = Path(__file__).resolve().parents[3]


class Discovery(unittest.TestCase):
    def fixture(self, card='0', external=False, net=True):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        parameter = root/'sys/module/sipc_wwan/parameters/card'
        parameter.parent.mkdir(parents=True); parameter.write_text(card)
        physical = root/('sys/devices/pci/usb/wwan/wwan0/wwan0at0' if external else
                         'sys/devices/platform/sipc-virt/core/channel/wwan/wwan0/wwan0at0')
        physical.mkdir(parents=True)
        port = root/'sys/class/wwan/wwan0at0'; port.parent.mkdir(parents=True); port.symlink_to(physical)
        if net:
            for name in ('sipa_eth0','sipa_eth8'):
                (root/'sys/class/net'/name).mkdir(parents=True)
        cache = root/'events.cache'; cache.write_text('add,sipa_eth8,net,old\nadd,sipa_eth0,net,old\n')
        common = root/'common'
        common.write_text(f'''MODEMMANAGER_RUNDIR={root}/run
MODEMMANAGER_EVENTS_CACHE={cache}
mm_report_event() {{ printf '%s %s %s\\n' "$1" "$2" "$3" >> {root}/events; }}
''')
        script = root/'report'
        source = (TOP/'device/openwrt_device_common/overlay/usr/libexec/e5-mm-report-ports').read_text()
        script.write_text(source.replace('/sys/', str(root)+'/sys/').replace(
            '/usr/share/ModemManager/modemmanager.common', str(common)))
        return root, script

    def run_script(self, root, script):
        subprocess.run(['sh', str(script)], check=True)
        events = root/'events'
        return events.read_text().splitlines() if events.exists() else []

    def test_selected_card_and_order(self):
        for card, own, other in [('0','sipa_eth0','sipa_eth8'), ('1','sipa_eth8','sipa_eth0')]:
            root, script = self.fixture(card)
            self.assertEqual(self.run_script(root, script), [
                'add wwan0at0 wwan', f'remove {other} net', f'add {own} net'])

    def test_external_wwan_is_not_claimed(self):
        root, script = self.fixture(external=True)
        self.assertEqual(self.run_script(root, script), [])

    def test_invalid_slot_is_not_guessed(self):
        root, script = self.fixture('2')
        self.assertEqual(self.run_script(root, script), [])

    def test_missing_at_port_does_not_publish_net(self):
        root, script = self.fixture(); (root/'sys/class/wwan/wwan0at0').unlink()
        self.assertEqual(self.run_script(root, script), [])

    def test_missing_net_port_keeps_at_notification(self):
        root, script = self.fixture(net=False)
        self.assertEqual(self.run_script(root, script), ['add wwan0at0 wwan'])

    def test_shared_patch_is_idempotent_and_preserves_replay_sequence(self):
        patch = runpy.run_path(str(TOP/'device/openwrt_device_common/shared/network-startup.py'))['patch_root']
        for profile in ('e5','t551m','m8'):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); wrapper = root/'usr/sbin/ModemManager-wrapper'
                wrapper.parent.mkdir(parents=True)
                wrapper.write_text('''#!/bin/sh
main() {
	/usr/sbin/ModemManager "$@" 1>/dev/null 2>/dev/null &
	mm_report_events_from_cache
	wait "$CHILD"
}
''')
                loader = root/'etc/init.d/e5-sipc-wwan'; loader.parent.mkdir(parents=True)
                loader.write_text((TOP/'device/openwrt_device_common/overlay/etc/init.d/e5-sipc-wwan').read_text())
                patch(root, profile); first = wrapper.read_bytes(), loader.read_bytes()
                patch(root, profile); self.assertEqual(first, (wrapper.read_bytes(), loader.read_bytes()))
                text = wrapper.read_text()
                self.assertLess(text.index('boot-replay early'), text.index('/usr/sbin/ModemManager "$@"'))
                self.assertGreater(text.index('# unisoc-port-discovery:'), text.index('/usr/sbin/ModemManager "$@"'))
                self.assertIn('tx_gap_ms=100', loader.read_text())

    def test_installed_update_hook_uses_overlay_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installed = root/'opt/e5/t551m-network-startup'
            installed.parent.mkdir(parents=True)
            installed.write_bytes((TOP/'device/openwrt_device_common/shared/network-startup.py').read_bytes())
            patch = runpy.run_path(str(installed))['patch_root']
            with self.assertRaisesRegex(ValueError, 'Missing shared'):
                patch(root)
            helper = root/'usr/libexec/e5-mm-report-ports'
            helper.write_bytes((TOP/'device/openwrt_device_common/overlay/usr/libexec/e5-mm-report-ports').read_bytes())
            before = helper.read_bytes()
            patch(root)
            self.assertEqual(before, helper.read_bytes())
            self.assertTrue(helper.stat().st_mode & 0o111)


if __name__ == '__main__': unittest.main()
