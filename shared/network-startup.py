#!/usr/bin/env python3
"""Apply shared Unisoc networking fixes and explicit board defaults to a root.

The historical path is retained for installed update hooks. PDP retries apply
equally to E5, T551M and M8; only radio capability defaults are board-specific.
"""
import argparse
from pathlib import Path
import re


# Based on mu300-linux 47d9463d; fixed channel fallback, no bandwidth changes.
NO_DFS = '''// unisoc-driver-dfs-capabilities: fixed fallback preserving bandwidth.
function device_no_dfs(config) {
	if (config.band != '5g' || phy_features.radar_detect)
		return;
	let radar = {};
	for (let f in phy_capabilities.freqs) {
		let chan = (f.freq - 5000) / 5;
		if (f.radar)
			radar[chan] = true;
	}
	if (!config.channel) {
		config.acs_exclude_dfs = true;
		return;
	}
	if (!radar[config.channel])
		return;
	// Fixed fallback requested by the user: only replace the primary channel.
	// hostapd/nl80211 retain their normal width and regulatory validation.
	log(`driver lacks radar detection: using channel 36 instead of DFS channel ${config.channel}, preserving ${config.htmode}`);
	config.channel = 36;
}

'''


def patch_wifi_capabilities(root):
    path = root / 'usr/share/ucode/wifi/hostapd.uc'
    if not path.exists():
        return
    text = path.read_text()
    marker = '// unisoc-driver-dfs-capabilities:'
    if marker in text:
        # Upgrade roots carrying the earlier auto-shrink policy too. Keep
        # imports/capability collection/call site intact and replace only our
        # marked policy section, which immediately precedes generate().
        start = text.index(marker)
        end = text.index('function generate(config) {', start)
        if text[start:end] != NO_DFS:
            path.write_text(text[:start] + NO_DFS + text[end:])
        return
    replacements = [
        ('wiphy_info, wiphy_band\n', 'wiphy_info, wiphy_band, log\n'),
        # New wifi-scripts moved cipher flags to ap.uc. Collect DFS capability
        # beside the shared phy/band lookup, which exists in both generators.
        ("\tlet band = wiphy_band(phy, config.band);",
         "\tlet band = wiphy_band(phy, config.band);\n"
         "\tphy_features.radar_detect = length(filter(phy.interface_combinations ?? [], (c) => c.radar_detect_widths)) > 0;\n"
         "\tphy_capabilities.freqs = band?.freqs ?? [];"),
        ('function generate(config) {', NO_DFS + 'function generate(config) {'),
        ('\tset_device_defaults(config);\n', '\tset_device_defaults(config);\n\tdevice_no_dfs(config);\n'),
    ]
    for old, new in replacements:
        if text.count(old) != 1:
            raise ValueError('Unexpected wifi-scripts hostapd.uc: ' + old[:70])
        text = text.replace(old, new)
    path.write_text(text)


CONNECT_RETRY = '''# unisoc-pdp-connect-retry: bounded recovery without disabling the radio.
modemmanager_connect_retry() {
	local device="$1" connectargs="$2" attempt=1 result rc delay
	while :; do
		result=$(mmcli --modem="${device}" --timeout 120 --simple-connect="${connectargs}" 2>&1)
		rc=$?
		printf '%s\\n' "$result"
		[ "$rc" -eq 0 ] && return 0
		# Observed after Android -> OpenWrt: context activation error 28 can
		# clear on a later attempt. Do not retry SIM/auth/other errors here.
		[ "$device" = unisoc-sipc ] && [ "$attempt" -lt 3 ] || return "$rc"
		case "$result" in
			*"Couldn't activate PDP context "*"Unknown mobile equipment error: 28'"*) ;;
			*) return "$rc" ;;
		esac
		delay=$((attempt * 5))
		echo "PDP activation error 28; retrying in ${delay}s (${attempt}/2)"
		sleep "$delay"
		attempt=$((attempt + 1))
	done
}

'''


APN_SELECTION = '''\t# unisoc-apn-selection: shared by E5, T551M and M8 on every dial.
\tif [ "$device" = unisoc-sipc ] && [ -x /usr/libexec/unisoc-modem/apn ]; then
\t\tlocal apn_selection apn_family apn_auth apn_source
\t\tapn_selection=$(/usr/libexec/unisoc-modem/apn resolve "$device")
\t\tif [ "$(printf '%s' "$apn_selection" | jsonfilter -e '@.ok')" != 1 ]; then
\t\t\tproto_notify_error "$interface" APN_PROFILE_INVALID
\t\t\treturn 1
\t\tfi
\t\tapn_source=$(printf '%s' "$apn_selection" | jsonfilter -e '@.source')
\t\tif [ "$interface" = "$(printf '%s' "$apn_selection" | jsonfilter -e '@.iface')" ] && [ "$apn_source" != network ]; then
\t\t\tapn=$(printf '%s' "$apn_selection" | jsonfilter -e '@.settings.apn')
\t\t\tusername=$(printf '%s' "$apn_selection" | jsonfilter -e '@.settings.username')
\t\t\tpassword=$(printf '%s' "$apn_selection" | jsonfilter -e '@.settings.password')
\t\t\tapn_family=$(printf '%s' "$apn_selection" | jsonfilter -e '@.settings.pdptype')
\t\t\tcase "$apn_family" in IP) iptype=ipv4 ;; IPV4V6) iptype=ipv4v6 ;; *) return 1 ;; esac
\t\t\tapn_auth=$(printf '%s' "$apn_selection" | jsonfilter -e '@.settings.auth')
\t\t\tcase "$apn_auth" in papchap) allowedauth='pap chap' ;; none|pap|chap) allowedauth=$apn_auth ;; *) return 1 ;; esac
\t\t\tinit_epsbearer=keep
\t\tfi
\t\tunset apn_selection
\tfi
'''


def patch_root(root, profile='t551m'):
    discovery = root / 'usr/libexec/e5-mm-report-ports'
    discovery.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / 'overlay/usr/libexec/e5-mm-report-ports'
    if source.is_file():
        discovery.write_bytes(source.read_bytes().replace(b'\r\n', b'\n'))
    elif not discovery.is_file():
        # On-device update hooks live in /opt/e5, outside the source tree.
        # The helper is installed by the common overlay before these run.
        raise ValueError('Missing shared SIPC port discovery helper')
    discovery.chmod(0o755)
    patch_wifi_capabilities(root)
    config = root / 'etc/config/unisoc_modem'
    defaults = root / 'etc/e5/radio-defaults.json'
    if config.exists() and defaults.is_file():
        import json
        policy = json.loads(defaults.read_text())
        text = config.read_text()
        additions = ''
        for key in ('auto_work_mode', 'lte_supported_bands'):
            value = policy.get(key)
            if value is not None and 'option ' + key + ' ' not in text:
                if not re.fullmatch(r'[0-9,]+', str(value)):
                    raise ValueError('Invalid radio default: ' + key)
                additions += "\toption " + key + " '" + str(value) + "'\n"
        if additions:
            anchor = "config core 'main'\n"
            if text.count(anchor) != 1:
                raise ValueError('Unexpected modem dashboard configuration')
            config.write_text(text.replace(anchor, anchor + additions))

    wrapper = root / 'usr/sbin/ModemManager-wrapper'
    if wrapper.exists():
        text = wrapper.read_text()
        if '# unisoc-pre-dial-replay' not in text:
            anchor = '\t/usr/sbin/ModemManager "$@" 1>/dev/null 2>/dev/null &'
            if text.count(anchor) != 1:
                raise ValueError('Unexpected ModemManager startup wrapper')
            text = text.replace(anchor, '''\t# unisoc-pre-dial-replay: finish saved AT settings before MM can enable/dial.
\tif [ -x /usr/libexec/unisoc-modem/boot-replay ]; then
\t\t/usr/libexec/unisoc-modem/boot-replay early ||
\t\t\tlogger -t unisoc-modem "pre-dial replay failed; continuing without a late radio cycle"
\tfi

''' + anchor)
        if '# unisoc-port-discovery:' not in text:
            anchor = '\tmm_report_events_from_cache\n'
            if text.count(anchor) != 1:
                raise ValueError('Unexpected ModemManager event replay location')
            text = text.replace(anchor, anchor + '''\t# unisoc-port-discovery: recover a port created before MM started.
\t[ ! -x /usr/libexec/e5-mm-report-ports ] || /usr/libexec/e5-mm-report-ports
''')
        wrapper.write_text(text)

    loader = root / 'etc/init.d/e5-sipc-wwan'
    if loader.exists():
        text = loader.read_text()
        if 'tx_gap_ms=100' not in text:
            old = 'insmod $M/sipc_wwan.ko card=$C'
            if text.count(old) != 1:
                raise ValueError('Unexpected SIPC loader')
            text = text.replace(old, old + ' tx_gap_ms=100')
            text = text.replace('insmod $M/sipc_wwan.ko;', 'insmod $M/sipc_wwan.ko tx_gap_ms=100;')
        if '# unisoc-port-discovery:' not in text:
            anchor = '\t\techo "sipc_wwan loaded, SIM card $C"'
            if text.count(anchor) != 1:
                raise ValueError('Unexpected SIPC port discovery location')
            text = text.replace(anchor, '''\t\t# unisoc-port-discovery: bypass the global hotplug queue after probe closes.
\t\t[ ! -x /usr/libexec/e5-mm-report-ports ] || /usr/libexec/e5-mm-report-ports
''' + anchor)
        loader.write_text(text)

    proto = root / 'lib/netifd/proto/modemmanager.sh'
    if proto.exists():
        text = proto.read_text()
        if '# unisoc-apn-selection:' not in text:
            anchor = '\t# set initial eps bearer settings\n'
            if text.count(anchor) != 1:
                raise ValueError('Unexpected ModemManager APN hook location')
            text = text.replace(anchor, APN_SELECTION + anchor)
            proto.write_text(text)
        if 'preserve AT-selected network mode' not in text:
            old = '\tif [ -z "${allowedmode}" ]; then\n\t\tmodemmanager_set_allowed_mode "$device" "$interface" "any"'
            if text.count(old) != 1:
                raise ValueError('Unexpected ModemManager default mode configuration')
            text = text.replace(old, '''\tif [ -z "${allowedmode}" ]; then
\t\t# preserve AT-selected network mode; Unisoc's MM mode setter is unsupported.
\t\t# Boot replay already applied the user's preference before radio activation.
\t\techo "Keeping current network mode (no WAN override configured)"''')
            proto.write_text(text)
        if 'T551M: preserve initial EPS' not in text:
            old = '\t\tcase "$init_epsbearer" in\n'
            if text.count(old) != 1:
                raise ValueError('Unexpected ModemManager initial EPS configuration')
            text = text.replace(old, old + '''\t\t\t"keep")
\t\t\t\t# T551M: preserve initial EPS; simple-connect still sets the APN.
\t\t\t\t# Rewriting it after registration forces CFUN=4/1 and a second search.
\t\t\t\techo "Keeping initial EPS bearer; applying connection APN at dial"
\t\t\t\t;;
''')
            proto.write_text(text)

        if '# unisoc-pdp-connect-retry:' not in text:
            anchor = 'proto_modemmanager_setup() {'
            call = '\tmmcli --modem="${device}" --timeout 120 --simple-connect="${connectargs}" || {'
            if text.count(anchor) != 1 or text.count(call) != 1:
                raise ValueError('Unexpected ModemManager connect configuration')
            text = text.replace(anchor, CONNECT_RETRY + anchor)
            text = text.replace(call, '\tmodemmanager_connect_retry "$device" "$connectargs" || {')
            proto.write_text(text)

    luci = root / 'www/luci-static/resources/protocol/modemmanager.js'
    if luci.exists():
        text = luci.read_text()
        if "o.value('keep'" not in text:
            # Works with the upstream readable source and APK's minified source.
            pattern = r"(o\.value\(\s*'custom'\s*,\s*'custom'\s*\);)"
            text, count = re.subn(pattern,
                r"\1o.value('keep', _('Keep existing initial EPS bearer'));", text)
            if count != 1:
                raise ValueError('Unexpected ModemManager LuCI options')
            luci.write_text(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--profile', default='t551m')
    args = parser.parse_args()
    patch_root(args.root, args.profile)
