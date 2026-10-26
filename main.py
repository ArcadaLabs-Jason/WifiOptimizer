"""WiFi Optimizer backend for Decky Loader.

Runs as root inside the plugin_loader process. All public async methods on
the Plugin class are callable from the React frontend via Decky's IPC. State
is persisted to settings.json under DECKY_PLUGIN_SETTINGS_DIR and shared with
the NetworkManager dispatcher script at defaults/dispatcher.sh.tmpl, which
reapplies volatile optimizations (power save, PCIe ASPM, buffer tuning, CAKE
QoS) on every WiFi reconnect independently of Decky.
"""

import os
import re
import copy
import base64
import hashlib
import pwd
import json
import tempfile
import time
import asyncio
import subprocess
import threading

try:
    import decky
except ImportError:
    # Local fallback when decky isn't importable (e.g., running outside
    # plugin_loader for static analysis or ad-hoc testing). All runtime
    # paths on a Deck have the real module.
    class decky:  # type: ignore
        DECKY_PLUGIN_SETTINGS_DIR = "/tmp/wifi-optimizer"
        DECKY_PLUGIN_DIR = "/tmp/wifi-optimizer"
        DECKY_PLUGIN_VERSION = "0.0.0"
        class logger:
            @staticmethod
            def info(msg): print(f"[INFO] {msg}")
            @staticmethod
            def error(msg): print(f"[ERROR] {msg}")


# People are asked to share this log in public threads. An access point's
# address can be looked up in the public WiFi databases, so every address
# the log would carry keeps only its last two octets: enough to tell access
# points apart when reading it, not enough to place anyone.
_LOG_MAC_RE = re.compile(
    r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){4}([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})(?![0-9A-Fa-f])"
)
# IP addresses go whole: GitHub names the network's public address in a rate
# limit refusal, and a private one says nothing a report needs. Neither edge
# may run on into more digits, but a full stop after the address is still the
# end of it ("for 10.0.0.1."), and five numbers in a row are not an address.
_IPV4_RE = re.compile(
    r"(?<!\d)(?<!\d\.)(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?!\d|\.\d)"
)
# Either the full eight groups or a form shortened with "::". Requiring one
# of the two keeps times and channel lists, which also have colons, out of it.
_IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:"
    r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
    r"|(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?::"
    r"(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?"
    r")(?![0-9A-Fa-f:])"
)
# A home directory names the desktop account, which on Bazzite and CachyOS is
# the person's own login. Paths reach the log in error messages.
_HOME_RE = re.compile(r"(?:/var)?/home/[^/\s'\"),:;]+")


def _mask_home(text: str) -> str:
    """text with the desktop user's home directory written as ~."""
    home = (getattr(decky, "DECKY_USER_HOME", "") or "").rstrip("/")
    if len(home) > 1 and home.startswith("/") and not _HOME_RE.fullmatch(home):
        text = text.replace(home, "~")
    return _HOME_RE.sub("~", text)


# NetworkManager, nmcli, iwd and wpa_supplicant name a network in quotes
# (nmcli: "Error: Failed to modify connection 'Smith's WiFi'"). Anything
# quoted goes, since a profile name is often a person's own wording. A name
# can hold a quote of its own, so a quoted value ends only at a quote
# followed by a space, a bracket or punctuation, or the end of the line, and
# starts only at a quote that does not follow a letter: a name cut at its
# apostrophe left the rest of it behind.
_QUOTED_RE = re.compile(
    r"(?<!\w)'.*?'(?=[\s),:;.\]]|$)|(?<!\w)\".*?\"(?=[\s),:;.\]]|$)"
)
# An opening quote still standing after that has lost its end (a line cut
# short): everything after it goes.
_OPEN_QUOTE_RE = re.compile(r"(?<!\w)['\"].*$")
# NetworkManager's own fixed words are quoted too, and they are what says
# why a connection failed: (reason 'no-secrets', sys-iface-state: 'managed').
_NM_WORD_KEY_RE = re.compile(r"(?:\breason |\bsys-iface-state: |\bmanaged-type: )$")
_NM_WORD_RE = re.compile(r"[a-z0-9-]+")


def _mask_quoted(line: str, keep_quotes: bool = False) -> str:
    """line with every quoted value replaced by <name>, except
    NetworkManager's fixed words after reason, sys-iface-state and
    managed-type. keep_quotes: '<name>' rather than <name>."""
    kept = []

    def quoted(found):
        value = found.group(0)[1:-1]
        if _NM_WORD_KEY_RE.search(line[:found.start()]) and _NM_WORD_RE.fullmatch(value):
            # Held aside so the pass below does not read its opening quote
            # as one that lost its end.
            kept.append(found.group(0))
            return f"\x00{len(kept) - 1}\x00"
        if keep_quotes:
            kept.append(found.group(0)[0] + "<name>" + found.group(0)[-1])
            return f"\x00{len(kept) - 1}\x00"
        return "<name>"

    text = _QUOTED_RE.sub(quoted, line)
    text = _OPEN_QUOTE_RE.sub("<name>", text)
    return re.sub(r"\x00(\d+)\x00", lambda found: kept[int(found.group(1))], text)


# systemd names a USB adapter or a dock by its hardware address when nothing
# else names it (wlx00c0ca123456, enx00e04c680001, wwx...): an address with
# no separators, which the patterns above do not see.
_IFNAME_MAC_RE = re.compile(r"\b(wl|en|ww)x([0-9A-Fa-f]{12})\b")
# NetworkManager names every device in its log, a VPN or virtual one by the
# name its owner gave it (a WireGuard device takes its config file's name).
# The kinds that bear on WiFi keep theirs, as the diagnostics' device list
# keeps them; the rest become <device>.
_NM_DEVICE_NAME_RE = re.compile(r"((?:\bdevice|\bmanager:) \()([^()\s]+)(\))")
_KEPT_DEVICE_RE = re.compile(r"(?:wl|en|eth|p2p-dev-)\S*|lo")


def _mask_device_names(text: str) -> str:
    """text with NetworkManager's device (NAME) and manager: (NAME) masked
    for every device that is not WiFi, Ethernet or loopback."""
    return _NM_DEVICE_NAME_RE.sub(
        lambda f: f.group(0) if _KEPT_DEVICE_RE.fullmatch(f.group(2))
        else f"{f.group(1)}<device>{f.group(3)}", text)


def _tool_text(text) -> str:
    """Output of nmcli, iw, wpa_supplicant, systemctl, steamos-manager or any
    other tool, as it may go into a log line: every quoted value masked,
    since a tool quotes a network's name. Only there: the plugin's own words
    quote values of its own (a region, a band, a version, a link's name),
    which a report needs as they are."""
    return "\n".join(_mask_quoted(line, keep_quotes=True) for line in str(text).split("\n"))


def _mask_addresses(text: str) -> str:
    """What every plugin log line goes through: addresses, the device names
    that carry one or a person's own wording, and the home directory masked.
    Quoted network names are masked where a tool's output enters a line
    (_tool_text), not here."""
    text = _mask_device_names(text)
    # The log keeps the last two octets of an address, here as everywhere.
    text = _IFNAME_MAC_RE.sub(lambda m: m.group(1) + "x" + m.group(2)[-4:], text)
    text = _LOG_MAC_RE.sub(lambda m: "xx:xx:xx:xx:" + m.group(1), text)
    text = _IPV4_RE.sub("<ip>", text)
    text = _IPV6_RE.sub(lambda m: m.group(0) if m.group(0) == "::" else "<ip>", text)
    return _mask_home(text)


# Any object with a filter method works as a logging filter, so the logging
# module itself is not imported (Decky's bundle carries only what it uses).
class _MaskAddresses:
    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        masked = _mask_addresses(message)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


if hasattr(decky.logger, "addFilter"):
    decky.logger.addFilter(_MaskAddresses())

DISPATCHER_PATH = "/etc/NetworkManager/dispatcher.d/99-wifi-optimizer"
SYSFS_NET = "/sys/class/net"
# Every wireless radio (wiphy), whether or not it has an interface.
SYSFS_IEEE80211 = "/sys/class/ieee80211"
NM_CONF_PATH = "/etc/NetworkManager/conf.d/99-wifi-optimizer.conf"
MODPROBE_CONF_PATH = "/etc/modprobe.d/99-wifi-optimizer.conf"
# Where modprobe looks for options, in its order of precedence.
MODPROBE_DIRS = ("/etc/modprobe.d", "/run/modprobe.d", "/usr/local/lib/modprobe.d",
                 "/usr/lib/modprobe.d", "/lib/modprobe.d")
PROC_CMDLINE = "/proc/cmdline"
# Driver parameters written at runtime only while the card's own radio is
# associated AND power save on it was just turned off by iw (contract C10).
# rtw88 checks disable_aspm each time it moves the PCIe link in or out of a
# power state: on entering and leaving IPS (radio idle, not associated) and
# on entering and leaving LPS, its power save while associated. Set while the
# link is in one, the way back out is skipped and ASPM stays on while the
# radio works: the state behind "failed to read ASPM" and an RTL8822CE that
# only a reboot revives. The dispatcher has the same two conditions.
ASSOCIATED_ONLY_PARAMS = ("/sys/module/rtw88_pci/parameters/disable_aspm",)
# SteamOS after 3.9 moves to Valve's "holo" packaging, which ships the same
# helper under a new name and nothing at the old path. Resolved once at load;
# an OS update reboots, so the plugin always loads against the current image.
_STEAMOS_BACKEND_HELPER = "/usr/bin/steamos-polkit-helpers/steamos-wifi-set-backend-privileged"
_HOLO_BACKEND_HELPER = "/usr/bin/holo-polkit-helpers/holo-wifi-set-backend-privileged"
BACKEND_HELPER = (
    _HOLO_BACKEND_HELPER
    if not os.path.isfile(_STEAMOS_BACKEND_HELPER) and os.path.isfile(_HOLO_BACKEND_HELPER)
    else _STEAMOS_BACKEND_HELPER
)
STEAMOSCTL = "/usr/bin/steamosctl"
WIFI_BACKEND_CONF = "/etc/NetworkManager/conf.d/99-valve-wifi-backend.conf"
NM_DEFAULT_CONF = "/usr/lib/NetworkManager/conf.d/10-steamos-defaults.conf"
HOLO_NM_DEFAULT_CONF = "/usr/lib/NetworkManager/conf.d/10-holo-defaults.conf"
GENERIC_BACKEND_CONF = "/etc/NetworkManager/conf.d/99-wifi-optimizer-backend.conf"
BAZZITE_IWD_CONF = "/etc/NetworkManager/conf.d/iwd.conf"

# chip_label names the family a driver serves, not one chip, and
# supports_6ghz is only the fallback for when the card cannot be asked
# (get_device_info asks `iw phy` first). A driver that also runs cards with
# no 6 GHz radio says False there: offering a band the card lacks pins the
# profile to a network it cannot join, while hiding it costs nothing until
# the card answers.
DRIVER_PROFILES = {
    "rtw88": {
        "chip_label": "WiFi 5 (RTL8822CE)",
        "supports_6ghz": False,
        "sysfs_power_fixes": [
            "/sys/module/rtw88_core/parameters/disable_lps_deep",
            "/sys/module/rtw88_pci/parameters/disable_aspm",
        ],
        "modprobe_options": [
            "options rtw88_core disable_lps_deep=Y",
            "options rtw88_pci disable_aspm=Y",
        ],
    },
    "ath11k_pci": {
        # Also the QCA6390, which has no 6 GHz.
        "chip_label": "WiFi 6E (QCA206X)",
        "supports_6ghz": False,
        "sysfs_power_fixes": [],
        "modprobe_options": [],
    },
    "rtw89": {
        # RTL8851BE, RTL8852AE/BE/CE and the WiFi 7 RTL8922AE; only some
        # of them have 6 GHz.
        "chip_label": "Realtek RTL885x/RTL8922",
        "supports_6ghz": False,
        "sysfs_power_fixes": [
            "/sys/module/rtw89_pci/parameters/disable_aspm_l1",
            "/sys/module/rtw89_pci/parameters/disable_aspm_l1ss",
        ],
        "modprobe_options": [
            "options rtw89_pci disable_aspm_l1=Y",
            "options rtw89_pci disable_aspm_l1ss=Y",
        ],
    },
    "ath12k": {
        "chip_label": "WiFi 7 (WCN7850)",
        "supports_6ghz": True,
        "sysfs_power_fixes": [],
        "modprobe_options": [],
    },
    "mt7921e": {
        # The 6E MT7922 (ROG Ally, Legion Go), and also the MT7921 and
        # MT7902, which have no 6 GHz.
        "chip_label": "MediaTek MT792x",
        "supports_6ghz": False,
        "sysfs_power_fixes": [
            "/sys/module/mt7921e/parameters/disable_aspm",
        ],
        "modprobe_options": [
            "options mt7921e disable_aspm=Y",
        ],
    },
    "iwlwifi": {
        # Every Intel card since 2013, the AX200 and AX201 without 6 GHz.
        "chip_label": "Intel WiFi",
        "supports_6ghz": False,
        "sysfs_power_fixes": [],
        # iwlmvm and iwlmld are separate modules from iwlwifi, so the modules
        # it may configure are listed rather than inferred from the driver
        # name. iwlmld runs the newer cards (BE200 and later) from kernel
        # 6.15, with its own power_scheme; iwlmvm's never reaches them.
        "modules": ["iwlwifi", "iwlmvm", "iwlmld"],
        "modprobe_options": [
            "options iwlwifi power_save=0 uapsd_disable=3",
            "options iwlmvm power_scheme=1",
            "options iwlmld power_scheme=1",
        ],
    },
}

DMI_DEVICES = {
    "Jupiter": {"family": "deck_lcd", "label": "Steam Deck LCD"},
    "Galileo": {"family": "deck_oled", "label": "Steam Deck OLED"},
    "83E1": {"family": "legion_go", "label": "Legion Go"},
    "83L3": {"family": "legion_go_s", "label": "Legion Go S"},
    "83N6": {"family": "legion_go_s", "label": "Legion Go S"},
    "83Q2": {"family": "legion_go_s", "label": "Legion Go S"},
    "83Q3": {"family": "legion_go_s", "label": "Legion Go S"},
    "83N0": {"family": "legion_go_2", "label": "Legion Go 2"},
    "83N1": {"family": "legion_go_2", "label": "Legion Go 2"},
}

DMI_SUBSTRING_DEVICES = [
    ("ROG Xbox Ally X RC73X", {"family": "rog_xbox_ally_x", "label": "ROG Xbox Ally X"}),
    ("ROG Xbox Ally RC73Y", {"family": "rog_xbox_ally", "label": "ROG Xbox Ally"}),
    ("ROG Ally X RC72LA", {"family": "rog_ally_x", "label": "ROG Ally X"}),
    ("ROG Ally RC71L", {"family": "rog_ally", "label": "ROG Ally"}),
    ("Claw", {"family": "msi_claw", "label": "MSI Claw"}),
    ("AYANEO", {"family": "ayaneo", "label": "AYANEO Device"}),
    ("GPD", {"family": "gpd", "label": "GPD Device"}),
    ("G1618", {"family": "gpd_win", "label": "GPD WIN Device"}),
]

# AYANEO and GPD put their brand in the DMI vendor, and on many models only
# the model in the product name ("AIR Plus"), so the vendor is matched as
# well, after the product name has had its say.
DMI_VENDOR_DEVICES = [
    ("AYANEO", {"family": "ayaneo", "label": "AYANEO Device"}),
    ("AYADEVICE", {"family": "ayaneo", "label": "AYANEO Device"}),
    ("GPD", {"family": "gpd", "label": "GPD Device"}),
]
DMI_DIR = "/sys/devices/virtual/dmi/id"

try:
    SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
    LEGACY_ENFORCED_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "last_enforced")
except Exception:
    SETTINGS_FILE = "/tmp/wifi-optimizer/settings.json"
    LEGACY_ENFORCED_FILE = "/tmp/wifi-optimizer/last_enforced"

# When the dispatcher last reapplied the settings. In /run, which only root
# can write, rather than beside the settings: that directory belongs to the
# desktop user, and anything root writes there can be raced with a symlink.
# /run is cleared at boot, which is fine for a "last applied" time.
ENFORCED_FILE = "/run/wifi-optimizer/last_enforced"

# A version string from the network ends up in a filename, a URL and a root
# shell script, so it is validated the moment it is parsed rather than at each
# use. Digits and dots, with an optional alphanumeric prerelease suffix.
# ASCII digits only: \d would also accept digits from other scripts.
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,3}(-[A-Za-z0-9.]+)?\Z")

# How far the update script has got, as "<phase> <version>". In /run for the
# same reason as ENFORCED_FILE: the panel acts on what it says, so only root
# may write it.
UPDATE_STATE_FILE = "/run/wifi-optimizer/update-state"
UPDATE_PHASES = ("downloading", "installing", "swapped", "loaded",
                 "restarting", "done", "failed")
# Phases a script that is still running reports. Older than the script's own
# time limit, they are left over from one that was killed.
UPDATE_ACTIVE_PHASES = ("downloading", "installing", "swapped", "loaded", "restarting")
UPDATE_STATE_MAX_AGE = 900
# Held by the update script for as long as it runs, so only one runs.
UPDATE_LOCK_FILE = "/run/wifi-optimizer-update.lock"

# Streaming auto mode: power save, buffer tuning and CAKE are applied only
# while one of these is running, matched against each process's command line.
# (id, label, patterns). Lowercase; a pattern is a plain substring.
STREAMING_APPS = (
    ("moonlight", "Moonlight", ("moonlight",)),
    ("geforce_now", "GeForce NOW", ("com.nvidia.geforcenow", "play.geforcenow.com")),
    ("chiaki", "Chiaki (PS Remote Play)", ("chiaki",)),
    ("steam_remote_play", "Steam Remote Play", ("/streaming_client",)),
    ("steam_link", "Steam Link", ("steamlink",)),
    ("greenlight", "Greenlight (Xbox)", ("greenlight",)),
    ("parsec", "Parsec", ("parsecd",)),
    ("xbox_cloud", "Xbox Cloud Gaming", ("xbox.com/play",)),
)
STREAMING_APP_IDS = tuple(app_id for app_id, _label, _patterns in STREAMING_APPS)
# Patterns the user adds. Short ones would match half the system ("steam"
# matches Steam itself, holding the fixes on for good), so they are refused.
STREAM_PATTERN_MIN = 4
STREAM_PATTERN_MAX = 64
STREAM_PATTERNS_MAX = 8
PROC_DIR = "/proc"
# Present while the fixes should be in place: a stream is running or the
# user is holding them on. The dispatcher checks it too. In /run, which only
# root writes and a reboot clears.
STREAM_FLAG_FILE = "/run/wifi-optimizer/streaming"
# The user's "keep fixes on", for a stream detection missed. Until reboot.
STREAM_HOLD_FILE = "/run/wifi-optimizer/streaming-hold"
STREAM_POLL_SECONDS = 5
# Passes without a stream before the fixes come off. A stream app that
# restarts, or a pass that lands mid-exec, should not flap them.
STREAM_MISSES_TO_CLOSE = 2
# The process search reads every process's command line, and a read of one
# stuck in the kernel never returns. One search runs at a time; a caller that
# finds one under way waits this long for it, then goes on with the last
# answer, so a stuck read holds one thread rather than one per caller.
PROC_SCAN_WAIT_SECONDS = 2.0
# Held by the dispatcher from before it reads the settings until it exits,
# and by the plugin while it saves power save off and takes the fixes off, so
# a dispatcher run already under way cannot put them back afterwards. Each
# side waits this long for the other, then logs it and carries on.
APPLY_LOCK_FILE = "/run/wifi-optimizer/apply.lock"
APPLY_LOCK_WAIT_SECONDS = 10.0
# How old the last search's answer may be for the status poll to go by it.
# The poll never waits on a search; it starts one and reads the answer at a
# later poll.
PROC_SCAN_FRESH_SECONDS = 15.0
# Per thread: set by _stream_running_now around its _find_stream call.
_PROC_SCAN_LOCAL = threading.local()
# The update script's own command line (bash, then the script in the
# directory apply_update made), so the search never takes it, or anything it
# started, for a stream: a name of the user's can match its paths or address.
_UPDATE_SCRIPT_ARG_RE = re.compile(r"\A(?:.*/)?wifi-optimizer-update-[^/]+/update\.sh\Z")
# Present while a backend switch on a system without SteamOS's own tools is
# under way: "<previous> <target> <boot id>". Left by a switch the plugin did
# not live to finish, so the next load can finish or undo it. Kept across a
# reboot (in /run, a reboot or power loss part way left a boot with the
# backend named and not enabled, or both enabled, and nothing to settle it),
# in the plugin's root-only directory, which uninstall and the dispatcher's
# self-removal already take away.
BACKEND_SWITCH_MARKER = "/var/lib/wifi-optimizer/backend-switch"
# This boot's id, which tells a marker from an earlier boot.
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
# What the PCIe power files held before we first changed them this boot.
PCIE_BASELINE_FILE = "/run/wifi-optimizer/pcie-baseline"
# Present once this boot's "driver parameters at load:" line is in the
# journal. Whichever of the dispatcher and the plugin is about to write a
# driver parameter first creates it and logs the line (see _log_params_at_load).
AT_LOAD_MARKER = "/run/wifi-optimizer/at-load-logged"
# The tag the dispatcher logs under, and the one the diagnostics read back.
JOURNAL_TAG = "wifi-optimizer"
# Every path the baseline may name is under here; the file is root's, but a
# value read back is still checked before root writes it anywhere.
SYSFS_ROOT = "/sys/"

# Starts the update script in a unit of its own, outside plugin_loader's. A
# Decky that stops its whole control group on restart (Decky PR #955 drops
# KillMode=process) would otherwise kill the script part way through.
SYSTEMD_RUN = "/usr/bin/systemd-run"

# Interface names are used to build sysfs paths. Kernel names cannot contain a
# separator, but the value is checked rather than assumed.
IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}\Z")

# NetworkManager connection uuids, as stored in our own settings.
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}\Z")

# Access point addresses are read back out of the settings file, which the
# desktop user owns, before being compared against scan results.
BSSID_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z")

# An ISO 3166-1 alpha-2 country, or 00 for the world-roaming domain. This one
# is strict for a specific reason: the value reaches `iw reg set` as root. A
# regulatory domain is also a legal constraint on transmit power, so a
# malformed one is not merely a bad setting.
REGDOMAIN_RE = re.compile(r"^(?:[A-Z]{2}|00)\Z")

# NetworkManager's 802-11-wireless.band values the band preference may write,
# and how the panel names them. "6GHz" exists from NetworkManager 1.58.
BAND_NAMES = {"a": "5 GHz", "bg": "2.4 GHz", "6GHz": "6 GHz"}

# The NetworkManager that knows band "6GHz", and the first that lets a
# WPA2-style (wpa-psk) profile join a WPA3-only access point, which every
# 6 GHz one is. 1.58.0 only allows that when the user connects by hand, so
# such a profile would not reconnect on its own after sleep or a dropout.
NM_6GHZ_BAND = (1, 58, 0)
NM_6GHZ_WPA_PSK = (1, 58, 1)

# DNS servers are free text from the panel. They are passed to nmcli as a
# single argument, so there is no shell involved, but the value is stored and
# replayed later and should be addresses and nothing else.
DNS_SERVER_RE = re.compile(r"^[0-9A-Fa-f:.]{2,45}\Z")
# Custom servers go into the profile's ipv4.dns, which NetworkManager only
# accepts IPv4 addresses for: anything else got as far as nmcli and came
# back as a failure that read like a broken network.
DNS_IPV4_RE = re.compile(r"^(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                         r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\Z")


DNS_PROVIDERS = {
    "cloudflare": "1.1.1.1 1.0.0.1",
    "google": "8.8.8.8 8.8.4.4",
    "quad9": "9.9.9.9 149.112.112.112",
}

# Tuned values for game streaming: larger socket buffers absorb bursty UDP
# traffic, higher netdev backlog/budget lets the kernel process more packets
# per NAPI cycle, and disabling tcp_slow_start_after_idle keeps TCP congestion
# window from resetting after idle pauses (matters for control-plane TCP).
# Values match commonly cited streaming presets rather than being
# exhaustively tuned.
SYSCTL_PARAMS = {
    "net.core.rmem_max": "16777216",
    "net.core.wmem_max": "16777216",
    "net.core.rmem_default": "1048576",
    "net.core.wmem_default": "1048576",
    "net.core.netdev_max_backlog": "5000",
    "net.core.netdev_budget": "600",
    "net.core.netdev_budget_usecs": "8000",
    "net.ipv4.tcp_slow_start_after_idle": "0",
}

# The one value the status poll checks to decide whether buffer tuning is
# still in place. Read from what we apply rather than repeated, so changing
# the tuning above cannot leave the drift check comparing to the old number.
TUNED_RMEM_MAX = SYSCTL_PARAMS["net.core.rmem_max"]

# Kernel defaults, the fallback when buffer tuning is turned off and no value
# from before our first change was recorded. Linux 6.18 raised rmem_max and
# wmem_max to 4 MiB (SteamOS 3.8.28 ships 6.18), so those two are adjusted by
# _stock_sysctl_defaults rather than read from here directly. Restoring the
# older 212992 there cut the stock buffers to a twentieth.
SYSCTL_DEFAULTS = {
    "net.core.rmem_max": "212992",
    "net.core.wmem_max": "212992",
    "net.core.rmem_default": "212992",
    "net.core.wmem_default": "212992",
    "net.core.netdev_max_backlog": "1000",
    "net.core.netdev_budget": "300",
    "net.core.netdev_budget_usecs": "2000",
    "net.ipv4.tcp_slow_start_after_idle": "1",
}

DEFAULT_SETTINGS = {
    "model": "unknown",
    "driver": "unknown",
    "device_family": "unknown",
    "device_label": "Unknown Device",
    "chip_label": "unknown",
    "supports_6ghz": False,
    "power_save_disabled": True,
    "auto_fix_on_wake": True,
    "bssid_lock_enabled": False,
    "bssid_lock_value": "",
    "bssid_lock_connection_uuid": "",
    # Every profile we have written a BSSID to. NetworkManager keeps more than
    # one profile per SSID and the lock follows whichever is in use, so a
    # single slot loses the earlier ones and leaves them pinned to an access
    # point with nothing able to clear it.
    "bssid_lock_uuids": [],
    # Which kinds of pin this plugin has ever written on this install. A band
    # or BSSID on a profile is only cleared as a leftover of ours when we have
    # written that kind before: people also set them by hand (nmcli, the KDE
    # network settings), and clearing those silently undid their choice.
    "pins_owned": [],
    # Bumped by one-time migrations in _main.
    "settings_version": 0,
    # What Valve's NetworkManager file said for wifi.powersave before this
    # plugin took power save over: "0"-"3", "" for nothing, "?" not recorded.
    "valve_powersave_before": "?",
    # Same reasoning for the band: a profile left demanding a band its
    # network does not offer will not associate, and only the one profile
    # that happened to be active was ever cleared.
    "band_preference_uuids": [],
    # The network the band preference was set on. Without it the preference
    # is enforced on every network you join, including ones that have no
    # 5 GHz at all, and the list above then grows once per network visited.
    "band_preference_ssid": "",
    "band_preference": "a",
    "band_preference_enabled": False,
    "dns_provider": "cloudflare",
    "dns_servers": "1.1.1.1 1.0.0.1",
    "dns_enabled": False,
    # Every profile we have written DNS servers to. Turning DNS off used to
    # reach only the profile in use, so the network it was set on kept
    # public DNS (and ignored the router's) with the panel showing it off.
    "dns_uuids": [],
    # Profiles Allow WPA2 here changed from WPA3 only (sae) to wpa-psk.
    # Reset and uninstall put sae back on each of them; nothing else in the
    # panel undoes it.
    "wpa2_uuids": [],
    # Profiles Reset or uninstall could not put back to WPA3 only. Retried a
    # profile at a time by the status poll until each one takes.
    "wpa2_restore_uuids": [],
    "ipv6_disabled": False,
    "ipv6_uuids": [],
    # Access points that accepted a scan but refused an association, and when.
    "ap_move_failures": {},
    "buffer_tuning_enabled": False,
    # What the buffer sysctls were before our first change, and the kernel
    # they were read on, so turning tuning off puts back what the system had.
    # A distro can ship its own values (CachyOS raises the backlog) and the
    # kernel's own defaults move between versions.
    "sysctl_baseline": {},
    "cake_enabled": False,
    "last_connection_uuid": "",
    "priority_set": False,
    # The wireless region, when the user has asked us to manage it. Empty
    # means we are not managing it and whatever the system has is left alone.
    "regdomain": "",
    # What the region was before our first change, so turning the setting off
    # restores it rather than guessing at a default.
    "regdomain_previous": "",
    "distro_id": "unknown",
    "distro_name": "Unknown",
    "update_channel": "stable",
    # A toast when an update is available, from a check that runs whether or
    # not the panel is open, and the version it was last shown for, so each
    # release is announced once.
    "update_notify": True,
    "update_notified_version": "",
    # Streaming auto mode: off means the fixes apply all the time, as always.
    "streaming_mode_enabled": False,
    # App id -> watched. An app missing from this is watched.
    "streaming_apps": {},
    # Extra process patterns, separated by spaces or commas.
    "streaming_custom_patterns": "",
    # The settings.json.corrupt the user has been told about, as its inode,
    # modification time in nanoseconds and size. Kept on disk rather than in
    # memory because Decky can reload the plugin between the file being set
    # aside and the panel showing it, and a flag in memory went with the old
    # process, so the notice never appeared. A later corruption replaces the
    # file, which no longer matches, so the notice comes back for it.
    "corrupt_acknowledged": [],
    "last_applied": 0,
}


def _child_env() -> dict:
    """Environment for every command we run.

    Decky is a PyInstaller build, and it points LD_LIBRARY_PATH at the
    libraries it bundles. A child that inherits that loads Decky's copies of
    libssl, readline, zstd and the rest instead of the system's, and fails
    with symbol-lookup errors that have nothing to do with what it was asked
    to do. Which commands that hits depends on what a given Decky release
    happens to bundle (3.3.0 added readline, tinfo and zstd), so it cannot be
    handled per command. PyInstaller keeps any original value in
    LD_LIBRARY_PATH_ORIG; restore that if present, otherwise drop it.
    """
    env = dict(os.environ)
    original = env.pop("LD_LIBRARY_PATH_ORIG", None)
    if original:
        env["LD_LIBRARY_PATH"] = original
    else:
        env.pop("LD_LIBRARY_PATH", None)
    return env


_SH_UNSAFE = re.compile(r"[^\w@%+=:,./-]", re.ASCII)


def _sh_quote(value: str) -> str:
    """Quote a string for the shell, the same way shlex.quote does.

    Not shlex itself: plugins can only import what Decky's own build
    bundles, and Decky 3.3.0 stopped bundling shlex.
    """
    if not value:
        return "''"
    if _SH_UNSAFE.search(value) is None:
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _display_ssid(ssid: str) -> str:
    """A network name as nmcli's terse output gives it, unescaped for the
    panel: it writes a colon in the name as \\: and a backslash as \\\\."""
    out, escaped = [], False
    for ch in ssid or "":
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    return "".join(out)


def _open_dir_no_follow(path: str, create: bool = False) -> int:
    """A descriptor for the directory at path, opened one component at a time.

    The settings and log directories belong to the desktop user, who can swap
    any of them for a link to somewhere else while root works in it. Opening
    each component with O_NOFOLLOW, and then working relative to what was
    opened, means a swap either fails the open or leaves the work in the
    directory that was opened. A link is followed only while every directory
    walked so far is root's and no one else can write it, so nobody but root
    could have made the link or moved anything above it. Bazzite's /home, a
    link to /var/home, is one. Past the first directory that is not, no link
    is followed at all.

    create makes missing components (mode 0755), as os.makedirs did. The
    caller closes the descriptor.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parts = [p for p in os.path.abspath(path).split("/") if p]
    links = 0
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    trusted = _root_only(os.fstat(fd))
    try:
        while parts:
            name = parts.pop(0)
            try:
                nxt = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(name, flags, dir_fd=fd)
            except OSError:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if ((st.st_mode & 0o170000) != 0o120000 or st.st_uid != 0
                        or not trusted or links >= 8):
                    _log_refused_link(path, name, st)
                    raise
                links += 1
                target = os.readlink(name, dir_fd=fd)
                if target.startswith("/"):
                    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
                    os.close(fd)
                    fd = root
                parts = [p for p in target.split("/") if p] + parts
                continue
            os.close(fd)
            fd = nxt
            trusted = trusted and _root_only(os.fstat(fd))
        return fd
    except BaseException:
        os.close(fd)
        raise


def _root_only(st: os.stat_result) -> bool:
    """Owned by root and writable by nobody else."""
    return st.st_uid == 0 and not st.st_mode & 0o022


# The paths _log_refused_link has already reported.
_REFUSED_LINKS: set = set()


def _log_refused_link(path: str, name: str, st: os.stat_result):
    """Say once, per path, that a link was not followed.

    A user who moved their homebrew folder behind a link of their own would
    otherwise see settings fall back to defaults with no reason given.
    """
    if (st.st_mode & 0o170000) != 0o120000:
        return
    if path in _REFUSED_LINKS:
        return
    _REFUSED_LINKS.add(path)
    try:
        decky.logger.error(
            f"Not following the link {name!r} on the way to {path}: the plugin "
            "only follows links that nobody but root could have made, so it "
            "will not read or write there"
        )
    except Exception:
        pass


def _remove_no_follow(path: str):
    """os.remove for a file in a directory the desktop user owns.

    Raises as os.remove does, FileNotFoundError included.
    """
    dfd = _open_dir_no_follow(os.path.dirname(path))
    try:
        os.unlink(os.path.basename(path), dir_fd=dfd)
    finally:
        os.close(dfd)


def _write_no_follow(path: str, data: str) -> bool:
    """Write a file in a directory someone else may control, without links.

    The settings directory belongs to the desktop user, so anything we write
    there can be replaced with a link to a file we should not be touching.
    Plain open(path, "w") follows that link and truncates the target as root.
    O_NOFOLLOW refuses instead, and O_EXCL means we only ever create. The
    directory itself is opened with _open_dir_no_follow and the file written
    relative to it, so swapping the directory for a link does not redirect
    the write either.
    """
    try:
        dfd = _open_dir_no_follow(os.path.dirname(path))
    except OSError:
        return False
    name = os.path.basename(path)
    try:
        try:
            os.unlink(name, dir_fd=dfd)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        fd = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
            dir_fd=dfd,
        )
        with os.fdopen(fd, "w") as f:
            f.write(data)
        return True
    except Exception:
        return False
    finally:
        os.close(dfd)


def _replace_no_follow(path: str, data: str) -> bool:
    """Write a file under a name of its own beside path, then rename it over
    path, so the file is never missing while it is rewritten.

    For the files the dispatcher reads by whether they exist: it records the
    system's PCIe values once a boot only while last_enforced is absent, and
    leaves the fixes off in auto mode while the streaming flag is absent.
    _write_no_follow removes the old file before creating the new one, and a
    dispatcher run in that gap took our values for the system's, or a stream
    for none. Same rules as _write_no_follow: no link followed, the directory
    opened once and worked in relative to that, and the temporary name is
    unique, so two writers never share it.
    """
    try:
        dfd = _open_dir_no_follow(os.path.dirname(path))
    except OSError:
        return False
    name = os.path.basename(path)
    tmp = f".{name}.{os.urandom(6).hex()}.plugin"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o644, dir_fd=dfd)
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        return True
    except Exception:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        return False
    finally:
        os.close(dfd)


def _with_pin(settings: dict, prop: str) -> list:
    """settings["pins_owned"] with prop added."""
    owned = list(settings.get("pins_owned", []))
    if prop not in owned:
        owned.append(prop)
    return owned


def _owned_kinds_left(owned: list, left: dict) -> list:
    """The kinds of pin in owned that a profile still carries after a
    clear-up, by the lists of profiles left. One taken off everywhere is no
    longer ours: kept, a band or BSSID the user later set by hand was
    cleared as a leftover of ours instead of reported as theirs."""
    lists = {"802-11-wireless.bssid": "bssid_lock_uuids",
             "802-11-wireless.band": "band_preference_uuids"}
    return [prop for prop in owned if left.get(lists.get(prop, ""))]


def _is_regular(mode: int) -> bool:
    """stat.S_ISREG without importing stat, which is kept off the list of
    modules this file relies on Decky's build to bundle."""
    return (mode & 0o170000) == 0o100000


def _read_small_int(path: str) -> int:
    """An integer from a small regular file, or 0. Not following a link and
    not blocking on a FIFO, and never reading more than a few bytes."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return 0
    try:
        if not _is_regular(os.fstat(fd).st_mode):
            return 0
        return int(os.read(fd, 32).decode("ascii", "replace").strip() or 0)
    except (OSError, ValueError):
        return 0
    finally:
        os.close(fd)


def _read_update_state() -> tuple[str, str, float]:
    """(phase, version, age in seconds) from the update script, or ("", "", 0).

    Read the way _read_small_int reads, and only a phase and version that
    look like ones the script writes are believed.
    """
    try:
        fd = os.open(UPDATE_STATE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "", "", 0
    try:
        st = os.fstat(fd)
        if not _is_regular(st.st_mode):
            return "", "", 0
        parts = os.read(fd, 128).decode("ascii", "replace").split()
    except OSError:
        return "", "", 0
    finally:
        os.close(fd)
    if len(parts) != 2 or parts[0] not in UPDATE_PHASES or not VERSION_RE.match(parts[1]):
        return "", "", 0
    return parts[0], parts[1], max(0.0, time.time() - st.st_mtime)


def _write_update_state(phase: str, version: str) -> bool:
    """Replace the update state in one step, so a reader never sees half."""
    tmp = UPDATE_STATE_FILE + ".plugin"
    if not _write_no_follow(tmp, f"{phase} {version}\n"):
        return False
    try:
        os.replace(tmp, UPDATE_STATE_FILE)
        return True
    except OSError:
        return False


# Command lines that are always running on a Deck. A pattern found inside one
# of them would hold the streaming fixes on all the time.
_ALWAYS_RUNNING = (
    "/home/deck/.local/share/steam/ubuntu12_32/steam", "steamwebhelper",
    "gamescope", "/home/deck/homebrew/services/pluginloader",
    "/usr/bin/networkmanager", "/usr/lib/systemd/systemd", "pipewire",
    "/usr/bin/python3",
)


def _parse_stream_patterns(text: str) -> tuple[list[str], list[str]]:
    """(kept, refused) from the user's process pattern field."""
    kept, refused = _stream_pattern_verdicts(text)
    return kept, [token for token, _why in refused]


def _stream_pattern_verdicts(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(kept, [(token, why)]) from the user's process pattern field, why
    being "ascii", "short", "long", "always" or "limit", so the panel can
    say which rule each refused one broke rather than list every rule at once.

    Only plain ASCII names: Python lowers any letter, but the update script's
    pgrep -i folds ASCII only, so the two searches disagreed on the rest."""
    kept, refused = [], []
    for token in re.split(r"[\s,]+", (text or "").lower()):
        if not token or token in kept:
            continue
        if not token.isascii():
            refused.append((token, "ascii"))
        elif len(token) < STREAM_PATTERN_MIN:
            refused.append((token, "short"))
        elif len(token) > STREAM_PATTERN_MAX:
            refused.append((token, "long"))
        elif any(token in line for line in _ALWAYS_RUNNING):
            refused.append((token, "always"))
        elif len(kept) >= STREAM_PATTERNS_MAX:
            refused.append((token, "limit"))
        else:
            kept.append(token)
    return kept, refused


_PCIE_VALUES = {"control": ("on", "auto")}


def _baseline_values(path: str) -> tuple:
    """The values a file named in the baseline can hold. Driver parameters
    under /sys/module are kernel booleans, which read back as Y or N."""
    if path.startswith("/sys/module/"):
        return ("Y", "N")
    return _PCIE_VALUES.get(os.path.basename(path), ("0", "1"))


def _read_pcie_baseline() -> dict:
    """What the PCIe power files and the driver's power parameters held
    before we changed them this boot."""
    try:
        fd = os.open(PCIE_BASELINE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return {}
    try:
        if not _is_regular(os.fstat(fd).st_mode):
            return {}
        data = json.loads(os.read(fd, 8192) or b"{}")
    except (OSError, ValueError):
        return {}
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and k.startswith(SYSFS_ROOT)
        and v in _baseline_values(k)
    }


def _save_pcie_baseline(baseline: dict, read_here=()):
    """Write the baseline beside its name and rename it over, so the file
    is never missing. The dispatcher records the system's values once a boot,
    when it finds no file: removing it before writing a new one gave a
    dispatcher run in between the chance to record our own values as the
    system's.

    read_here names the keys the caller read from the files itself, before
    any write of ours (the file had none of them then). Those win over what
    the file holds now: a dispatcher's first run that recorded them after
    the caller read them may have read the caller's own writes. Every other
    key the file holds is kept, since only a dispatcher run that found no
    file can have put it there, and that run read before it wrote. Callers
    save before they write, which leaves that window as small as it can be.
    """
    try:
        os.makedirs(os.path.dirname(PCIE_BASELINE_FILE), mode=0o755, exist_ok=True)
    except OSError:
        pass
    merged = {**baseline, **_read_pcie_baseline()}
    merged.update({key: baseline[key] for key in read_here if key in baseline})
    tmp = PCIE_BASELINE_FILE + ".plugin"
    if not _write_no_follow(tmp, json.dumps(merged)):
        return
    try:
        os.replace(tmp, PCIE_BASELINE_FILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _boot_time() -> int | None:
    """When this boot started, in seconds since the epoch."""
    try:
        with open("/proc/stat", "r") as f:
            return next(int(line.split()[1]) for line in f if line.startswith("btime "))
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def _kernel_bool(value: str | None) -> str | None:
    """A boolean module parameter as the kernel reads it (kstrtobool), as the
    Y or N it reads back as. None for anything it would refuse."""
    if not value:
        return None
    first, second = value[0].lower(), value[1:2].lower()
    if first in "yt1" or (first == "o" and second == "n"):
        return "Y"
    if first in "nf0" or (first == "o" and second == "f"):
        return "N"
    return None


def _param_name(path: str) -> tuple | None:
    """(module, parameter) for /sys/module/<module>/parameters/<parameter>."""
    parts = path.split("/")
    if len(parts) != 6 or parts[1:3] != ["sys", "module"] or parts[4] != "parameters":
        return None
    return parts[3], parts[5]


def _options_set(text: str, module: str, param: str) -> str | None:
    """The last value modprobe config text gives module.param, or None.
    A bare boolean parameter with no value means on."""
    found = None
    for line in text.splitlines():
        words = line.split("#", 1)[0].split()
        if len(words) < 3 or words[0] != "options":
            continue
        if words[1].replace("-", "_") != module:
            continue
        for word in words[2:]:
            key, eq, value = word.partition("=")
            if key.replace("-", "_") == param:
                found = value if eq else "Y"
    return found


def _param_from_config(path: str) -> str | None:
    """What the system's own configuration gives a driver parameter when the
    module loads, ignoring our file: modprobe.d (the first directory holding
    a given file name wins, files read in name order, the last setting wins)
    and then the kernel command line. None when nothing sets it.

    A guide that has people add disable_aspm=Y to a file of their own is the
    reason this exists: turning our fix off must not write N over that.
    """
    names = _param_name(path)
    if not names:
        return None
    module, param = names
    files = {}
    for directory in MODPROBE_DIRS:
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for name in entries:
            if name.endswith(".conf") and name not in files:
                files[name] = os.path.join(directory, name)
    found = None
    for name in sorted(files):
        if files[name] == MODPROBE_CONF_PATH:
            continue
        try:
            with open(files[name], "r") as f:
                value = _options_set(f.read(65536), module, param)
        except (OSError, UnicodeDecodeError):
            continue
        if value is not None:
            found = value
    try:
        with open(PROC_CMDLINE, "r") as f:
            for word in f.read(8192).split():
                key, eq, value = word.partition("=")
                if key.replace("-", "_") == f"{module}.{param}":
                    found = value if eq else "Y"
    except OSError:
        pass
    return _kernel_bool(found)


def _ours_at_boot() -> str:
    """Our modprobe file's text if it was in place before this boot, so its
    options were what the driver loaded with; "" otherwise."""
    boot = _boot_time()
    try:
        if boot is None or os.stat(MODPROBE_CONF_PATH).st_mtime >= boot:
            return ""
        with open(MODPROBE_CONF_PATH, "r") as f:
            return f.read(65536)
    except (OSError, UnicodeDecodeError):
        return ""


def _write_system_conf(path: str, content: str) -> bool:
    """Write a root-owned config file whole, or not at all. False if unchanged.

    These are rewritten on every reapply (each game launch). Written in place,
    an interruption could leave half a NetworkManager keyfile, which can stop
    NetworkManager starting. So a temp file beside it is renamed over it, and
    a file that already says the same is left untouched.
    """
    try:
        with open(path, "r") as f:
            if f.read(65536) == content:
                return False
    except (OSError, ValueError):
        # Missing, or not text (UnicodeDecodeError is a ValueError): a file
        # that cannot be read is replaced like one that differs.
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".new")
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return True


def _first_existing(paths: list[str]) -> str | None:
    """First path in the list that exists, or None."""
    for path in paths:
        if os.path.isfile(path):
            return path
    return None


# Where a system tool lives is distro layout: SteamOS and Arch keep iw, ip,
# tc and sysctl in /usr/bin, Fedora-based images (Bazzite) in /usr/sbin.
# Commands are written with the /usr/bin path and looked up here once.
TOOL_DIRS = ("/usr/bin", "/usr/sbin", "/bin", "/sbin")
_TOOL_PATHS: dict = {}


def _tool(path: str) -> str:
    """path if it exists, else the same tool in another system directory,
    else path unchanged (so the error names what was asked for)."""
    if path in _TOOL_PATHS:
        return _TOOL_PATHS[path]
    found = path
    if not os.path.isfile(path) and os.path.dirname(path) in TOOL_DIRS:
        name = os.path.basename(path)
        found = _first_existing([os.path.join(d, name) for d in TOOL_DIRS]) or path
    _TOOL_PATHS[path] = found
    return found


def _kernel_release() -> str:
    try:
        return os.uname().release
    except Exception:
        return ""


def _stock_sysctl_defaults(release: str) -> dict:
    """The kernel's own buffer defaults for this kernel version."""
    values = dict(SYSCTL_DEFAULTS)
    found = re.match(r"(\d+)\.(\d+)", release or "")
    if found and (int(found.group(1)), int(found.group(2))) >= (6, 18):
        values["net.core.rmem_max"] = "4194304"
        values["net.core.wmem_max"] = "4194304"
    return values


def _clean_sysctl_baseline(value) -> dict:
    """Keep only a well-formed baseline. These values reach `sysctl -w` as
    root and come from a file the desktop user can edit, so anything that is
    not a known key with a plain number is dropped."""
    if not isinstance(value, dict):
        return {}
    kernel = value.get("kernel", "")
    values = value.get("values", {})
    if not isinstance(kernel, str) or len(kernel) > 128 or not isinstance(values, dict):
        return {}
    kept = {
        k: v for k, v in values.items()
        if k in SYSCTL_PARAMS and isinstance(v, str)
        and re.fullmatch(r"[0-9]{1,12}", v, re.ASCII)
    }
    return {"kernel": kernel, "values": kept} if kernel and kept else {}


# NetworkManager's wifi.powersave values: default, ignore, disable, enable.
_VALVE_POWERSAVE_VALUES = ("0", "1", "2", "3")


def _set_aside_corrupt_settings(read_stat: os.stat_result, reason: Exception) -> bool:
    """Move a settings file that does not parse to settings.json.corrupt.

    The loader falls back to defaults and _main saves them straight away,
    which used to overwrite the only copy of the tracked profile lists and
    the recorded baselines. Moved rather than copied, so the next load finds
    no file and takes the ordinary missing-file path instead of setting it
    aside again, and a previous .corrupt file is replaced: one is enough to
    recover from, and they cannot pile up.

    Returns True when the file is no longer at the name (moved, or replaced
    by a save meanwhile), False when it is still there: the caller must then
    keep any save from writing over it.

    A set-aside that failed is not tried again, or logged again, while the
    file and whatever sits at the .corrupt name are both unchanged: every
    settings read (several per status poll) came here, and the same error
    line on each one crowded out the log a report reads.
    """
    tried = (read_stat.st_dev, read_stat.st_ino, read_stat.st_size,
             read_stat.st_mtime_ns, _aside_obstacle())
    if _SETTINGS_STATE.get("aside_failed") == tried:
        return False
    try:
        dfd = _open_dir_no_follow(os.path.dirname(SETTINGS_FILE))
        name = os.path.basename(SETTINGS_FILE)
        try:
            # Only the file that was read. A save that landed since then put
            # a good file at this name, and moving that aside would lose it.
            now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (read_stat.st_dev, read_stat.st_ino):
                return True
            os.replace(name, name + ".corrupt", src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
        decky.logger.error(
            f"Settings file could not be read ({type(reason).__name__}); "
            "kept it as settings.json.corrupt and started from defaults"
        )
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        _SETTINGS_STATE["aside_failed"] = tried
        decky.logger.error(f"Settings file could not be read or set aside: {e}")
        return False


def _aside_obstacle() -> tuple | None:
    """What sits at settings.json.corrupt (device, inode, type, mtime), not
    following a link, or None. Part of a failed set-aside's identity: once
    that changes (removed by the user, say), it is worth trying again."""
    try:
        st = os.lstat(SETTINGS_FILE + ".corrupt")
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mode, st.st_mtime_ns)


def _set_aside_unread_settings() -> bool:
    """Reset's way past a settings file that could not be read at all (an
    I/O error): renamed to settings.json.corrupt without another read, as a
    file that does not parse is. Only the file the failed read left there
    (_SETTINGS_STATE["unread"]). True once it is no longer at the name."""
    unread = _SETTINGS_STATE.get("unread")
    try:
        dfd = _open_dir_no_follow(os.path.dirname(SETTINGS_FILE))
        name = os.path.basename(SETTINGS_FILE)
        try:
            now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if not unread or (now.st_dev, now.st_ino, now.st_mtime_ns) != tuple(unread):
                return _settings_file_identity() != unread
            os.replace(name, name + ".corrupt", src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
    except FileNotFoundError:
        pass
    except OSError as e:
        decky.logger.error(f"Reset could not set the unreadable settings file aside: {e}")
        return False
    decky.logger.error("Settings file could not be read; Reset kept it as "
                       "settings.json.corrupt and started from defaults")
    _SETTINGS_STATE["unread"] = None
    _SETTINGS_STATE["read_failed"] = False
    return True


def _corrupt_settings_identity() -> list | None:
    """Inode, modification time in nanoseconds and size of the settings file
    set aside as unreadable, or None when there is none.

    Looked at without following links and only counted when it is a regular
    file: the directory is the user's, so anything can sit at this name, and
    only a file this plugin moved there is worth telling them about. A stat
    and nothing more, as the status poll calls this every few seconds.
    """
    try:
        st = os.lstat(SETTINGS_FILE + ".corrupt")
    except OSError:
        return None
    if not _is_regular(st.st_mode):
        return None
    return [st.st_ino, st.st_mtime_ns, st.st_size]


def _load_settings() -> dict:
    try:
        # Bounded read. This file belongs to the desktop user and root parses
        # it on every status poll, so an oversized one is a way to make the
        # plugin allocate arbitrarily much, repeatedly. The dispatcher applies
        # the same cap.
        # Not blocking on a FIFO planted at this name, which would hang the
        # plugin on its first read; only ever a regular file; and not through
        # a link, at the name or anywhere in the user's directories above it.
        dfd = _open_dir_no_follow(os.path.dirname(SETTINGS_FILE))
        try:
            fd = os.open(os.path.basename(SETTINGS_FILE),
                         os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=dfd)
        finally:
            os.close(dfd)
        # Checked before os.fdopen, which raises on a directory without
        # closing the descriptor. Root reads this on every poll, so a
        # directory planted at the name leaked one each time until the
        # plugin ran out and every open failed.
        try:
            read_stat = os.fstat(fd)
            if not _is_regular(read_stat.st_mode):
                raise ValueError("settings file is not a regular file")
            f = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        with f:
            raw = f.read(65536)
            oversized = bool(f.read(1))
    except FileNotFoundError:
        return _default_settings()
    except Exception:
        # Not a regular file, or unreadable. Nothing was parsed, so the
        # defaults stand in, and whatever sits at the path is left where it
        # is: _save_settings will not write over the file that could not be
        # read, so a read error that passes does not cost the user their
        # settings.
        _note_unread_settings()
        return copy.deepcopy(DEFAULT_SETTINGS)
    if oversized:
        # A regular file past the cap is set aside like one that does not
        # parse. Left in place, the first save replaced it with the defaults
        # and the tracked profile lists went with it.
        return _defaults_for_unparsed(read_stat, ValueError("settings file too large"))
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("settings file does not hold a JSON object")
    except (ValueError, RecursionError) as e:
        return _defaults_for_unparsed(read_stat, e)
    try:
        settings = _clean_settings(data)
    except Exception:
        _note_unread_settings()
        return copy.deepcopy(DEFAULT_SETTINGS)
    _SETTINGS_STATE["read_failed"] = False
    _SETTINGS_STATE["unread"] = None
    return settings


def _defaults_for_unparsed(read_stat: os.stat_result, reason: Exception) -> dict:
    """The defaults standing in for a settings file that does not parse. Set
    aside first; when that fails (something at the .corrupt name, say), the
    file is still there and is treated as unread, so no save replaces it."""
    if _set_aside_corrupt_settings(read_stat, reason):
        return _default_settings()
    _note_unread_settings()
    return copy.deepcopy(DEFAULT_SETTINGS)


def _settings_file_identity() -> tuple | None:
    """Device, inode and modification time of the regular file at the
    settings name, not following a link, or None for anything else."""
    try:
        st = os.lstat(SETTINGS_FILE)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_mtime_ns) if _is_regular(st.st_mode) else None


def _note_unread_settings():
    """A read that failed: the defaults stand in, and the regular file that
    could not be read, if that is what sits there, is noted so no save
    replaces it (see _save_settings). A directory, FIFO or link at the name
    holds no settings to lose."""
    _SETTINGS_STATE["read_failed"] = True
    _SETTINGS_STATE["unread"] = _settings_file_identity()


def _unread_settings_in_place() -> bool:
    """Whether the file the last read could not read is still there."""
    unread = _SETTINGS_STATE.get("unread")
    return bool(unread) and _settings_file_identity() == unread


# What the last settings read and save came to, for the status the panel
# shows and for the streaming watch. read_failed: the file is there but the
# defaults had to stand in for it. save_failed: the last save did not land.
# unread: the regular file a failed read left in place (see _note_unread_settings).
# aside_failed: the identity of the last set-aside that failed (see
# _set_aside_corrupt_settings), so it is neither retried nor logged again.
_SETTINGS_STATE = {"read_failed": False, "save_failed": False, "unread": None,
                   "aside_failed": None}


def _default_settings() -> dict:
    """The defaults, for a settings file that is missing.

    After a file was set aside as unreadable, the pins this install wrote
    are still on the profiles, and the record that they were ours went with
    the file. A band or BSSID pin nobody owns is left alone as set by hand,
    so nothing would ever clear them. Both kinds are taken as ours instead,
    which is what an install that has a .corrupt file to show has done.
    """
    _SETTINGS_STATE["read_failed"] = False
    _SETTINGS_STATE["unread"] = None
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    if _corrupt_settings_identity() is not None:
        settings["pins_owned"] = ["802-11-wireless.bssid", "802-11-wireless.band"]
    return settings


def _clean_settings(data: dict) -> dict:
    """Parsed settings merged over the defaults, with every value checked."""
    # Merge with defaults (adds new keys), then strip stale keys. The
    # defaults are copied: their lists and dicts are shared otherwise,
    # and a caller appending to one would change DEFAULT_SETTINGS itself.
    merged = {**copy.deepcopy(DEFAULT_SETTINGS), **data}
    merged = {k: v for k, v in merged.items() if k in DEFAULT_SETTINGS}
    # This file lives in a directory the desktop user owns, so its types
    # are not guaranteed. A driver of the wrong type makes the profile
    # lookup raise on an unhashable key and silently kills the feature.
    for key, default in DEFAULT_SETTINGS.items():
        if not isinstance(merged.get(key), type(default)):
            merged[key] = copy.deepcopy(default)
    # An out-of-range band would be rejected by the setter, including on
    # the path that turns the preference OFF - leaving a profile pinned to
    # a band with no control able to clear it.
    if merged.get("band_preference") not in BAND_NAMES:
        merged["band_preference"] = "a"
    # Only the two channels exist. A hand-edited "Beta" was treated as
    # stable while the panel's dropdown had no option to show for it.
    if merged.get("update_channel") not in ("stable", "beta"):
        merged["update_channel"] = "stable"
    # Compared against GitHub's answer and shown in a toast.
    notified = merged.get("update_notified_version", "")
    if notified and (len(notified) > 64 or not VERSION_RE.match(notified)):
        merged["update_notified_version"] = ""
    # These lists are handed to nmcli as root. argv rather than a shell,
    # so junk is not dangerous, but a list of it turns a reset into a
    # long series of doomed calls on the event loop.
    for key in ("last_connection_uuid", "bssid_lock_connection_uuid"):
        value = merged.get(key, "")
        if value and not UUID_RE.match(value):
            merged[key] = ""
    # ipv6_uuids belongs here too: it is the third tracked list, it is
    # handed to nmcli on exactly the same path, and leaving it out meant
    # the one list whose entries were never checked was also the one the
    # cleanup refuses to act on without a record.
    for key in ("bssid_lock_uuids", "band_preference_uuids", "ipv6_uuids",
                "dns_uuids", "wpa2_uuids", "wpa2_restore_uuids"):
        merged[key] = [
            u for u in merged.get(key, [])
            if isinstance(u, str) and UUID_RE.match(u)
        ]
    # The region reaches `iw reg set`. Both the setter and the startup
    # reapply check it, but a value that cannot be valid has no business
    # surviving a load either.
    for key in ("regdomain", "regdomain_previous"):
        value = merged.get(key, "")
        if value and not REGDOMAIN_RE.match(value):
            merged[key] = ""
    merged["sysctl_baseline"] = _clean_sysctl_baseline(
        merged.get("sysctl_baseline")
    )
    # Older versions saved DNS off as provider "custom" with no servers,
    # and turning it on from there is refused with no field to fill in.
    if (not merged.get("dns_enabled") and merged.get("dns_provider") == "custom"
            and not str(merged.get("dns_servers", "")).strip()):
        merged["dns_provider"] = "cloudflare"
    merged["pins_owned"] = [
        p for p in merged.get("pins_owned", [])
        if p in ("802-11-wireless.band", "802-11-wireless.bssid")
    ]
    # Written back into a NetworkManager file as root, so nothing but the
    # values NetworkManager defines may come through.
    if merged.get("valve_powersave_before") not in _VALVE_POWERSAVE_VALUES + ("", "?"):
        merged["valve_powersave_before"] = "?"
    merged["streaming_apps"] = {
        k: v for k, v in merged.get("streaming_apps", {}).items()
        if k in STREAMING_APP_IDS and isinstance(v, bool)
    }
    merged["streaming_custom_patterns"] = merged.get("streaming_custom_patterns", "")[:512]
    # Sorted by time when recording a new failure: a non-number there made
    # that raise part way through moving the lock.
    merged["ap_move_failures"] = {
        k: v for k, v in merged.get("ap_move_failures", {}).items()
        if isinstance(k, str) and BSSID_RE.match(k)
        and isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    # Only ever compared, but a malformed value would never match and would
    # keep the notice up however often it was dismissed. bool is excluded by
    # name because Python counts it as an int.
    acknowledged = merged.get("corrupt_acknowledged", [])
    if not (len(acknowledged) == 3
            and all(type(n) is int and n >= 0 for n in acknowledged)):
        merged["corrupt_acknowledged"] = []
    return merged


def _diagnostics_path() -> str:
    """Where Save diagnostics writes, beside the settings. Worked out when
    called rather than fixed at import, as SETTINGS_FILE can be moved."""
    return os.path.join(os.path.dirname(SETTINGS_FILE), "diagnostics.json")


class SettingsSaveError(OSError):
    """A settings save that did not land. An OSError, as the save raised
    before, so every caller that handles one handles this."""


def _save_settings(data: dict):
    """Save the settings whole, or raise SettingsSaveError.

    The outcome is kept for the status: a settings directory that is full,
    read-only or not the user's any more otherwise fails every toggle with
    nothing on the panel saying why.

    Refused while the file at the name could not be read: what is being
    saved was built on the defaults that stood in for it.
    """
    if _unread_settings_in_place():
        _SETTINGS_STATE["save_failed"] = True
        raise SettingsSaveError("settings not saved: the file there could not be read")
    try:
        _write_settings(data)
    except Exception as e:
        _SETTINGS_STATE["save_failed"] = True
        raise SettingsSaveError(f"settings not saved: {e}") from e
    _SETTINGS_STATE["save_failed"] = False


def _write_settings(data: dict):
    # The directory is the user's: everything below is relative to it as
    # opened by _open_dir_no_follow, which also creates it if missing.
    dfd = _open_dir_no_follow(os.path.dirname(SETTINGS_FILE), create=True)
    # Write to a temp file then rename, so a crash cannot leave a half-written
    # settings file. The temp name is unique per call rather than fixed: with a
    # shared name, two writers race and the loser's os.replace finds its own
    # file already renamed away. Every caller is on the event loop today, but
    # that is not something a future change should have to know.
    tmp_name = f".settings-{os.urandom(6).hex()}.tmp"
    try:
        fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
                # Created 0600 and os.replace preserves it, which would
                # silently narrow this file from the 0644 it has always had
                # and make it unreadable to the user whose settings it holds.
                # Set on the open file, not by name, which could be swapped.
                os.fchmod(f.fileno(), 0o644)
            os.replace(tmp_name, os.path.basename(SETTINGS_FILE),
                       src_dir_fd=dfd, dst_dir_fd=dfd)
        except Exception:
            try:
                os.unlink(tmp_name, dir_fd=dfd)
            except FileNotFoundError:
                pass
            raise
    finally:
        os.close(dfd)


def _save_settings_with_timestamp(data: dict):
    """Save settings and update last_applied timestamp in one write."""
    data["last_applied"] = int(time.time())
    _save_settings(data)


# Release signatures. Coming from GitHub over TLS is not enough on its own:
# a compromised account or a tampered release asset could then push code that
# runs as root. So every release carries a SHA256SUMS file signed with a key
# pinned below, and the updater installs nothing that list does not vouch
# for. With no key pinned every check fails, and so does every update.
#
# The format is minisign's, so releases can be signed with the stock tool and
# checked here without anything Decky does not bundle. Ed25519 is written out
# below for the same reason: hashlib and base64 are the only modules it needs.
#
# Each entry is a minisign public key as the second line of a minisign.pub
# file, the base64 that starts "RW". A string is what a person compares
# against the published key, so it is pinned as one. The first key signs
# releases. The second is a rescue key, kept apart from the first and used
# only to sign a release that rotates the first one out if it is ever lost or
# leaked.
TRUSTED_RELEASE_KEYS = (
    # Release key, id FE0DCD913B7044FA.
    "RWT6RHA7kc0N/vvpQCVTRBrSXNZpv7hBFPZpSRX9m5v55Mrl15BB1heh",
    # Rescue key, id 0DB3B91752AFFB43.
    "RWRD+69SF7mzDbaCBVP3Pj5/5sOngkf/rVfOxCocbSTf+H8OP115i+8g",
)

# Bounds on what is read. A real signature file is about 300 bytes and a
# checksum list a few lines, so anything near these is not ours.
RELEASE_SIG_MAX = 4096
RELEASE_SUMS_MAX = 65536

# Fetching the two signature files. A slow first connection to GitHub's
# download host is common, so each try gets the same connect timeout as the
# payload download and one more try after a network error. The budget covers
# both files and every try, well inside the panel's own update timeout.
RELEASE_FETCH_BUDGET = 110
RELEASE_FETCH_TRY_MAX = 60
RELEASE_FETCH_RETRY_DELAY = 2
# curl: could not resolve, could not connect, timed out, TLS handshake
# failed, connection reset while receiving.
RELEASE_FETCH_NETWORK_RC = (6, 7, 28, 35, 56)

# Ed25519 per RFC 8032. Verification only: releases are signed elsewhere.
_ED_P = 2 ** 255 - 19
_ED_L = 2 ** 252 + 27742317777372353535851937790883648493
_ED_D = -121665 * pow(121666, _ED_P - 2, _ED_P) % _ED_P
_ED_SQRT_M1 = pow(2, (_ED_P - 1) // 4, _ED_P)


def _ed_add(p: tuple, q: tuple) -> tuple:
    """Add two points in extended coordinates (X, Y, Z, T).

    The formula from RFC 8032 section 5.1.4. It holds for any two points,
    a point and itself included, so it doubles as well.
    """
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _ED_P
    b = (y1 + x1) * (y2 + x2) % _ED_P
    c = 2 * t1 * t2 * _ED_D % _ED_P
    d = 2 * z1 * z2 % _ED_P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _ED_P, g * h % _ED_P, f * g % _ED_P, e * h % _ED_P)


def _ed_mul(s: int, p: tuple) -> tuple:
    """s times p. Both are public when verifying, so taking longer for some
    scalars than others gives nothing away."""
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _ed_add(q, p)
        p = _ed_add(p, p)
        s >>= 1
    return q


def _ed_encode(p: tuple) -> bytes:
    """A point as its 32-byte encoding: y, with the low bit of x on top."""
    x, y, z, _t = p
    zi = pow(z, _ED_P - 2, _ED_P)
    x, y = x * zi % _ED_P, y * zi % _ED_P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _ed_decode(s: bytes):
    """The point a 32-byte encoding names, or None.

    Strict, per RFC 8032 section 5.1.3: a y at or past p, a y with no x on
    the curve, and a sign bit set on x = 0 are all refused, so each point has
    exactly one encoding that is accepted.
    """
    if not isinstance(s, bytes) or len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    if y >= _ED_P:
        return None
    u = (y * y - 1) % _ED_P
    v = (_ED_D * y * y + 1) % _ED_P
    x2 = u * pow(v, _ED_P - 2, _ED_P) % _ED_P
    x = pow(x2, (_ED_P + 3) // 8, _ED_P)
    if (x * x - x2) % _ED_P != 0:
        x = x * _ED_SQRT_M1 % _ED_P
    if (x * x - x2) % _ED_P != 0:
        return None
    if x == 0 and sign:
        return None
    if x & 1 != sign:
        x = _ED_P - x
    return (x, y, 1, x * y % _ED_P)


# The base point: y = 4/5, with the even x.
_ED_B = _ed_decode((4 * pow(5, _ED_P - 2, _ED_P) % _ED_P).to_bytes(32, "little"))


def _ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """Whether signature is public_key's Ed25519 signature on message.

    The check is the cofactorless one, [S]B == R + [k]A, compared as
    encodings. An S at or past the group order is refused rather than
    reduced: otherwise one signature has many valid forms.
    """
    if not (isinstance(public_key, bytes) and isinstance(message, bytes)
            and isinstance(signature, bytes)):
        return False
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _ed_decode(public_key)
    r = _ed_decode(signature[:32])
    if a is None or r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _ED_L:
        return False
    k = int.from_bytes(
        hashlib.sha512(signature[:32] + public_key + message).digest(), "little"
    ) % _ED_L
    return _ed_encode(_ed_mul(s, _ED_B)) == _ed_encode(_ed_add(r, _ed_mul(k, a)))


def _b64_exact(text: str, length: int):
    """Decode base64 that must be in its one canonical form and decode to
    exactly length bytes, or None. Re-encoding catches padding bits and
    other spellings that a lenient decoder would let through."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != length or base64.b64encode(raw).decode("ascii") != text:
        return None
    return raw


def _parse_minisign_key(key):
    """(key id, public key) from a minisign public key, or None.

    Takes the whole minisign.pub file (an untrusted comment line, then the
    key) or just the key line. The key is "Ed", an 8-byte id and the 32-byte
    Ed25519 key.
    """
    if not isinstance(key, str):
        return None
    lines = key[:-1].split("\n") if key.endswith("\n") else key.split("\n")
    if len(lines) == 2 and lines[0].startswith("untrusted comment: "):
        lines = lines[1:]
    if len(lines) != 1:
        return None
    raw = _b64_exact(lines[0], 42)
    if raw is None or raw[:2] != b"Ed":
        return None
    return raw[2:10], raw[10:]


def _parse_minisig(text):
    """The parts of a minisign signature file, or None.

    Four lines: an untrusted comment, the signature, the trusted comment and
    the global signature over the signature and that comment. Anything else
    is refused, including a carriage return, since that would make the
    trusted comment a different string from the one that was signed.
    """
    if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > RELEASE_SIG_MAX:
        return None
    if "\r" in text:
        return None
    lines = text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    if len(lines) != 4:
        return None
    if not lines[0].startswith("untrusted comment: "):
        return None
    if not lines[2].startswith("trusted comment: "):
        return None
    sig = _b64_exact(lines[1], 74)
    global_sig = _b64_exact(lines[3], 64)
    if sig is None or global_sig is None or sig[:2] not in (b"Ed", b"ED"):
        return None
    try:
        comment = lines[2][len("trusted comment: "):].encode("utf-8")
    except UnicodeEncodeError:
        return None
    return {
        "algorithm": sig[:2],
        "key_id": sig[2:10],
        "signature": sig[10:],
        "trusted_comment": comment,
        "global_signature": global_sig,
    }


def _minisign_verify(data: bytes, minisig_text: str, keys,
                     algorithms=(b"Ed", b"ED")) -> dict:
    """Check a minisign signature on data against the pinned keys.

    {"ok": True, "trusted_comment": text} or {"ok": False, "error": reason}.
    Both signatures must hold: the one over the file, and the global one that
    binds the trusted comment to it. What the comment says is left to the
    caller, which knows what it expects. algorithms is which signature forms
    the caller accepts.
    """
    if not isinstance(data, bytes):
        return {"ok": False, "error": "nothing to verify"}
    parsed = _parse_minisig(minisig_text)
    if parsed is None:
        return {"ok": False, "error": "signature file malformed"}
    try:
        pinned = [_parse_minisign_key(k) for k in keys]
    except TypeError:
        return {"ok": False, "error": "no release keys pinned"}
    pinned = [k for k in pinned if k is not None]
    if not pinned:
        return {"ok": False, "error": "no release keys pinned"}
    public_key = next((pk for kid, pk in pinned if kid == parsed["key_id"]), None)
    if public_key is None:
        return {"ok": False, "error": "signature is not from a pinned key"}
    if parsed["algorithm"] not in algorithms:
        return {"ok": False, "error": "signature does not verify"}
    # "ED" signs a BLAKE2b-512 hash of the file, what minisign writes by
    # default now. "Ed" signs the file itself, as older minisign did.
    if parsed["algorithm"] == b"ED":
        signed = hashlib.blake2b(data, digest_size=64).digest()
    else:
        signed = data
    if not _ed25519_verify(public_key, signed, parsed["signature"]):
        return {"ok": False, "error": "signature does not verify"}
    if not _ed25519_verify(public_key, parsed["signature"] + parsed["trusted_comment"],
                           parsed["global_signature"]):
        return {"ok": False, "error": "trusted comment signature does not verify"}
    return {"ok": True, "trusted_comment": parsed["trusted_comment"].decode("utf-8")}


# One line of a SHA256SUMS file as sha256sum writes it in text mode. The name
# is a bare filename, never a path: it will be looked for in one directory.
_SHA256SUMS_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+-]{0,127})\Z")


def _parse_sha256sums(data: bytes):
    """{filename: sha256 hex} from a SHA256SUMS file, or None.

    Every line must parse, no name may appear twice, and no name may climb
    out of the directory it is meant for.
    """
    if not isinstance(data, bytes) or len(data) > RELEASE_SUMS_MAX:
        return None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    lines = text[:-1].split("\n") if text.endswith("\n") else text.split("\n")
    sums = {}
    for line in lines:
        match = _SHA256SUMS_LINE.match(line)
        if match is None or ".." in match.group(2) or match.group(2) in sums:
            return None
        sums[match.group(2)] = match.group(1)
    return sums or None


def verify_release_manifest(sums_bytes: bytes, minisig_text: str,
                            expected_version: str, keys=None) -> dict:
    """Check a release's signed SHA256SUMS before trusting anything in it.

    {"ok": True, "sha256": {filename: hex}} or {"ok": False, "error": reason}.
    The signature must come from a pinned key, and its trusted comment must
    name exactly this version. That comment is covered by the signature, so
    an old release signed by the same key cannot be passed off as a newer
    one. Fails closed: anything unexpected is a failure, never an exception.
    keys defaults to TRUSTED_RELEASE_KEYS, read at call time.
    """
    try:
        if keys is None:
            keys = TRUSTED_RELEASE_KEYS
        if not keys:
            return {"ok": False, "error": "no release keys pinned"}
        if not isinstance(expected_version, str) or not VERSION_RE.match(expected_version):
            return {"ok": False, "error": "version malformed"}
        if not isinstance(sums_bytes, bytes):
            return {"ok": False, "error": "checksum list malformed"}
        if len(sums_bytes) > RELEASE_SUMS_MAX:
            return {"ok": False, "error": "checksum list too large"}
        # Only the prehashed form, which is what releases are signed with.
        # The algorithm is not covered by either signature, so accepting the
        # legacy form too would let a relabelled signature be checked over
        # the hash of the file as if it were the file. That cannot parse as
        # a checksum list today, but it should not rest on the parser.
        result = _minisign_verify(sums_bytes, minisig_text, keys, algorithms=(b"ED",))
        if not result["ok"]:
            return result
        if result["trusted_comment"] != f"wifi-optimizer v{expected_version}":
            return {"ok": False, "error": "signature is for a different release"}
        sums = _parse_sha256sums(sums_bytes)
        if sums is None:
            return {"ok": False, "error": "checksum list malformed"}
        return {"ok": True, "sha256": sums}
    except Exception:
        return {"ok": False, "error": "verification failed"}


# Where releases are looked up and downloaded. Every release, beta or stable,
# carries the same three files: the source tarball, SHA256SUMS listing its
# checksum, and SHA256SUMS.minisig signing that list. From 0.24.3 there is
# also wifi-optimizer.zip, for installing from Decky; SHA256SUMS lists it too,
# and the updater only ever looks up the tarball's line.
RELEASES_API = "https://api.github.com/repos/ArcadaLabs-Jason/WifiOptimizer/releases"
RELEASE_DOWNLOAD = "https://github.com/ArcadaLabs-Jason/WifiOptimizer/releases/download"
# A page of releases, notes included, is tens of kilobytes. Anything past
# this is not GitHub answering the question we asked.
RELEASES_LIST_MAX = 4 * 1024 * 1024


# The update script, run as root with its values as arguments (see
# apply_update). Raw, so what is written here is exactly what bash reads.
UPDATE_SCRIPT = r"""#!/bin/bash
set -u
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

SELF_DIR="$1"
PLUGIN_DIR="$2"
DOWNLOAD_URL="$3"
SRC_NAME="$4"
LABEL="$5"
EXPECTED="$6"
# The tarball's sha256, from the release's signed SHA256SUMS. It is what
# makes the download trustworthy, so there is no way on without one.
EXPECTED_SHA="${7:-}"
# What counts as a stream, as one extended regex: the streaming apps the user
# has ticked and their own process names, the same ones the plugin watches.
# Empty when nothing is watched, and then only a game counts. Read from a file
# the plugin leaves in this script's own directory, never from an argument:
# anything on a command line is there for a process search to find, and this
# script searching for the patterns would find itself.
STREAM_PATTERNS=""
[ -f "$SELF_DIR/streams" ] && IFS= read -r STREAM_PATTERNS < "$SELF_DIR/streams"
# The plugin backend's PID, from a file beside it: what the backend started
# (curl, nmcli) is never a stream (see playing).
OWNER=""
[ -f "$SELF_DIR/owner" ] && IFS= read -r OWNER < "$SELF_DIR/owner"
case "$OWNER" in ''|*[!0-9]*) OWNER="" ;; esac

STATE=/run/wifi-optimizer/update-state
# This script's own time limit, in seconds since it started. Inside the
# RuntimeMaxSec its systemd unit is given (UPDATE_STATE_MAX_AGE in main.py,
# 900), with room to finish, so the unit is never killed part way. Applied
# just the same when it was started without systemd-run, which sets none.
BUDGET=840
TMP=""
OWN_LOCK=0
FINISHED=0
# The panel shows these as the update goes, and on "swapped" asks Decky to
# reload the plugin. Only the update holding the lock may write them.
set_state() {
    [ "$OWN_LOCK" = 1 ] || return 0
    mkdir -p -m 0755 /run/wifi-optimizer 2>/dev/null
    printf '%s %s\n' "$1" "$EXPECTED" > "$STATE.tmp" 2>/dev/null &&
        mv -f "$STATE.tmp" "$STATE" 2>/dev/null
}
cleanup() {
    [ -n "$TMP" ] && rm -rf "$TMP"
    # Only the update holding the lock may clear staged files; another one
    # exiting early must not delete what the running one is about to move.
    if [ "$OWN_LOCK" = 1 ]; then
        rm -f "$PLUGIN_DIR"/*.wifiopt-new "$PLUGIN_DIR"/*/*.wifiopt-new
        [ "$FINISHED" = 1 ] || set_state failed
    fi
    rm -rf "$SELF_DIR"
}
trap cleanup EXIT
trap 'exit 1' TERM INT HUP
# Made once the trap is set: a failure here (a full /tmp) used to exit
# before it, leaving this script's directory behind, and while that exists
# the plugin believes an update is still running.
TMP=$(mktemp -d) || exit 1

# One update at a time. A retry after the panel gave up waiting used to
# start a second copy alongside the first, and both restarted Decky.
# The lock file is private (0600), one an older version left 0644 included:
# any user who can open it can hold the lock, and while they did no update
# would install.
if command -v flock >/dev/null 2>&1; then
    (umask 077; : >> /run/wifi-optimizer-update.lock) || exit 1
    chmod 600 /run/wifi-optimizer-update.lock || exit 1
    exec 9>/run/wifi-optimizer-update.lock || exit 1
    flock -n 9 || { logger -t wifi-optimizer "Update already running"; exit 0; }
fi
OWN_LOCK=1
# Checked once the lock is held, so the panel is told this one failed.
case "$EXPECTED_SHA" in
    *[!0123456789abcdef]*) EXPECTED_SHA="" ;;
esac
if [ "${#EXPECTED_SHA}" != 64 ]; then
    logger -t wifi-optimizer "Update failed: no signed checksum to check the payload against"
    exit 1
fi
set_state downloading

sleep 2

# Bounded, so a stalled download ends instead of outliving the panel's wait,
# and in size, far above a real release (under 1 MB), so a huge one cannot
# fill /tmp, which is memory.
OPTS="--connect-timeout 15 --speed-limit 1024 --speed-time 30 --max-time 300 --max-filesize 52428800"
curl -sSL --fail --proto '=https' --tlsv1.2 $OPTS "$DOWNLOAD_URL" -o "$TMP/update.tar.gz" || {
    logger -t wifi-optimizer "Update failed: download error"
    exit 1
}

# Recorded so a bad or unexpected payload is traceable after the fact, and
# compared before anything is extracted: until it matches, the file is just
# whatever the network handed us.
SUM=$(sha256sum "$TMP/update.tar.gz" 2>/dev/null | cut -d' ' -f1)
logger -t wifi-optimizer "Update payload sha256=$SUM"
if [ "$SUM" != "$EXPECTED_SHA" ]; then
    logger -t wifi-optimizer "Update failed: payload does not match its signed checksum"
    exit 1
fi

tar xzf "$TMP/update.tar.gz" -C "$TMP" --no-same-owner --no-same-permissions || {
    logger -t wifi-optimizer "Update failed: extract error"
    exit 1
}
SRC="$TMP/$SRC_NAME"

# Uninstalled while this ran: Decky removed the plugin's directory, and
# carrying on would put the plugin back, or restart Decky for nothing.
uninstalled() {
    [ -f "$PLUGIN_DIR/plugin.json" ] && return 1
    logger -t wifi-optimizer "Update stopped: the plugin was uninstalled"
    FINISHED=1
    return 0
}

FILES="plugin.json main.py decky.pyi dist/index.js defaults/dispatcher.sh.tmpl package.json"
for f in $FILES; do
    if [ ! -f "$SRC/$f" ]; then
        logger -t wifi-optimizer "Update failed: payload incomplete ($f)"
        exit 1
    fi
done
GOT=$(grep -o '"version": *"[^"]*"' "$SRC/package.json" | head -n 1 | cut -d'"' -f4)
if [ "$GOT" != "$EXPECTED" ]; then
    logger -t wifi-optimizer "Update failed: payload is version $GOT, expected $EXPECTED"
    exit 1
fi

# Copy everything beside the live files first, and only then move each into
# place. A copy that fails part way (a full disk) leaves the installed
# version whole. package.json goes last because it carries the version: if a
# move fails, the files already moved stay new but the version reads old, so
# the same update is offered again and completes the set.
uninstalled && exit 0
set_state installing
mkdir -p "$PLUGIN_DIR/dist" "$PLUGIN_DIR/defaults" || {
    logger -t wifi-optimizer "Update failed: could not create directories"
    exit 1
}
for f in $FILES; do
    rm -f "$PLUGIN_DIR/$f.wifiopt-new"
    cp "$SRC/$f" "$PLUGIN_DIR/$f.wifiopt-new" || {
        logger -t wifi-optimizer "Update failed: could not copy $f"
        exit 1
    }
done
for f in $FILES; do
    mv -f "$PLUGIN_DIR/$f.wifiopt-new" "$PLUGIN_DIR/$f" || {
        logger -t wifi-optimizer "Update failed: could not replace $f"
        exit 1
    }
done
rm -f "$PLUGIN_DIR/dist/index.js.map"
cp "$SRC/dist/index.js.map" "$PLUGIN_DIR/dist/" 2>/dev/null || true

# The new files are in place. The panel now asks Decky to reload just this
# plugin, the way Decky's own store does, and the new backend answers
# "loaded" once it is running. Restarting plugin_loader instead restarts all
# of Steam, so that is only the fallback: for a Decky without the reload, or
# a panel that was not there to ask.
set_state swapped
for _ in $(seq 1 45); do
    sleep 1
    if [ "$(cat "$STATE" 2>/dev/null)" = "loaded $EXPECTED" ]; then
        FINISHED=1
        set_state done
        logger -t wifi-optimizer "Updated to $LABEL, reloaded the plugin"
        exit 0
    fi
done

set_state restarting
# Restarting plugin_loader restarts Steam, which ends a game. The panel only
# checked for one when the update started, and a download can take minutes,
# so look again and wait for it (or a stream) to end. A stream is one the
# plugin has flagged, or one of the apps the user has it watch running with
# auto mode off, when nothing flags it. pgrep's answer goes to a file rather
# than through $(...), whose subshell is a copy of this script under another
# PID that pgrep would list. Never a stream, as in the plugin's own search:
# this script and what it runs, the plugin's backend and what it runs (its
# PID is in "owner"; curl and nmcli carry addresses a name can match), any
# Decky plugin's backend (each is titled "<Name> (/.../homebrew/plugins/
# <dir>/main.py)", and a name like "moondeck" matches one that always runs)
# and Decky's PluginLoader. Read with builtins only, no fork.
# Fails closed: when the answer cannot be had or read back whole (a full
# /tmp), it counts as playing, since restarting Decky would end the game.
playing() {
    local rc pid last="" arg0 key val ppid depth
    pgrep -if "steamlaunch appid=${STREAM_PATTERNS:+|$STREAM_PATTERNS}" 2>/dev/null > "$TMP/pids"
    rc=$?
    [ "$rc" = 0 ] || [ "$rc" = 1 ] || return 0
    printf 'done\n' 2>/dev/null >> "$TMP/pids" || return 0
    while IFS= read -r pid; do
        last="$pid"
        case "$pid" in
            done) continue ;;
            "$$") continue ;;
            ''|*[!0-9]*) return 0 ;;
        esac
        [ -n "$OWNER" ] && [ "$pid" = "$OWNER" ] && continue
        arg0=""
        IFS= read -r -d '' arg0 2>/dev/null < "/proc/$pid/cmdline"
        case "$arg0" in
            *" (/"*"/homebrew/plugins/"*"/main.py)"*) continue ;;
        esac
        case "${arg0##*/}" in
            PluginLoader|plugin_loader) continue ;;
        esac
        depth=0
        while [ "$depth" -lt 8 ]; do
            ppid=""
            while IFS=$' \t' read -r key val; do
                if [ "$key" = "PPid:" ]; then ppid="$val"; break; fi
            done 2>/dev/null < "/proc/$pid/status"
            case "$ppid" in ''|*[!0-9]*|0|1) break ;; esac
            if [ "$ppid" = "$$" ] || { [ -n "$OWNER" ] && [ "$ppid" = "$OWNER" ]; }; then
                depth=ours
                break
            fi
            pid="$ppid"
            depth=$((depth + 1))
        done
        [ "$depth" = ours ] || return 0
    done 2>/dev/null < "$TMP/pids"
    [ "$last" = done ] || return 0
    [ -e /run/wifi-optimizer/streaming ]
}
if playing; then
    logger -t wifi-optimizer "Update installed; waiting for the game or stream to end before restarting Decky"
    while playing && [ "$SECONDS" -lt "$BUDGET" ]; do
        sleep 15
    done
    # Out of time. Restarting now would end the game, so the update stops
    # here with the new files in place, to load at the next restart.
    if playing; then
        logger -t wifi-optimizer "Update to $LABEL installed, but a game or stream was still running, so Decky was not restarted. Restart the device to finish."
        exit 1
    fi
fi
uninstalled && exit 0
if systemctl restart plugin_loader; then
    FINISHED=1
    set_state done
    logger -t wifi-optimizer "Updated to $LABEL, restarted plugin_loader"
else
    logger -t wifi-optimizer "Updated to $LABEL, but Decky did not restart. Restart the device to load it."
    exit 1
fi
"""


def _take_apply_lock(wait: float = APPLY_LOCK_WAIT_SECONDS) -> int | None:
    """Take APPLY_LOCK_FILE, waiting up to wait seconds. The descriptor that
    holds it, for _drop_apply_lock, or None when it could not be had (logged:
    the caller carries on without it, as before there was one).

    Polled rather than blocked on, so a lock nobody releases never keeps the
    thread that asked for it. fcntl is imported here, and its absence only
    means no lock: Decky's build carries it for subprocess, but a module
    missing from that build at the top of this file stops the whole backend.
    """
    try:
        import fcntl
    except ImportError:
        decky.logger.info("Apply lock unavailable (no fcntl), going on without it")
        return None
    try:
        os.makedirs(os.path.dirname(APPLY_LOCK_FILE), mode=0o755, exist_ok=True)
        # 0600: any user who can open the file can flock it, and one holding
        # it made every root path that takes it wait out its 10 s. A file an
        # older version left 0644 is tightened, by the descriptor, not the name.
        fd = os.open(APPLY_LOCK_FILE,
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as e:
        decky.logger.info(f"Apply lock unavailable, going on without it: {e}")
        return None
    try:
        if os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)
    except OSError as e:
        decky.logger.info(f"Could not make the apply lock private: {e}")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            pass
        except OSError as e:
            os.close(fd)
            decky.logger.info(f"Apply lock unavailable, going on without it: {e}")
            return None
        if time.monotonic() >= deadline:
            os.close(fd)
            decky.logger.info(
                f"The apply lock was still held after {wait:.0f}s; going on")
            return None
        time.sleep(0.05)


def _drop_apply_lock(fd: int | None):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


class _apply_lock_async:
    """APPLY_LOCK_FILE held for an `async with` block, taken in a worker
    thread. Never held over an await of anything that takes the stream or
    power save guard: those are always taken first."""

    async def __aenter__(self):
        self.fd = await asyncio.to_thread(_take_apply_lock)
        return self.fd

    async def __aexit__(self, *exc):
        _drop_apply_lock(self.fd)
        return False


def _stream_process_pattern(patterns: list[str]) -> str:
    """Process patterns as one extended regex for the update script's pgrep,
    each matched as plain text, as the plugin does."""
    return "|".join(
        re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", pattern)
        for pattern in dict.fromkeys(patterns)
    )


# ---- Send report to the developer ----

# Where Send report posts. Empty turns the feature off: the panel shows no
# button, nothing is read for the wake notice and nothing is ever sent.
REPORT_URL = ""
# https only, and nothing curl or a log line could read as more than a URL.
# The receiving end answers on /report only. Any other path is a URL set
# wrong, and is treated as none rather than sending to a 404 every time.
_REPORT_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/report\Z")

# The receiving end refuses a report over 96 KiB and a request over 128 KiB.
# A report is measured here as compact JSON with every non-ASCII character
# escaped, which is never smaller than the receiver's own measure of it for
# the values a report carries. (A float from 1e16 to 1e21 would be: Python
# writes 1e+17 where JavaScript writes 100000000000000000. A report has no
# such floats.)
REPORT_MAX_BYTES = 80 * 1024
# The log sections of the diagnostics. When a report is too large these go
# first, in this order, and the report says which went. The previous boot's
# kernel log goes last: it is the account of a wake that failed, which is
# what most reports are sent for, and also usually the biggest.
REPORT_LOG_KEYS = (
    "plugin_log_this_boot", "kernel_log_this_boot", "plugin_log_previous_boot",
    "network_log_previous_boot", "kernel_log_previous_boot",
)
# Never dropped to make room: without them a report says nothing, and the
# receiving end refuses one without success, plugin_version and device_info
# (or extras_error in place of plugin_version, when the extras failed).
# wake_failure is the wake notice's verdict on the previous boot.
REPORT_KEEP_KEYS = ("success", "plugin_version", "device_info", "left_out",
                    "extras_error", "wake_failure")
# What the receiving end answers with. Checked before it is shown or kept.
REPORT_REF_RE = re.compile(r"^WO-[0-9A-HJKMNP-TV-Z]{6}\Z")

# A report the user said to send that could not go yet. A report about WiFi
# that did not come back after sleep is most often sent after the reboot
# that fixed it, so it is kept on disk rather than in /run, in a directory
# only root can open. Never beside the settings: that directory belongs to
# the desktop user.
REPORT_DIR = "/var/lib/wifi-optimizer"
REPORT_PENDING_NAME = "pending-report.json"
# The reference of a waiting report that went while the panel was closed,
# kept until the panel has shown it.
REPORT_SENT_NAME = "sent-report-ref"
# The wake (its time) whose notice the user dismissed. Only Dismiss writes it.
REPORT_WAKE_SEEN_NAME = "wake-notice-seen"
# The wake (its time) a report was sent or queued about, with " sent" once a
# report about it has gone. Withdrawing a queued one removes it, so the
# notice is back; a report that went keeps it.
REPORT_WAKE_SENT_NAME = "wake-notice-sent"
# Present when a waiting report was dropped unsent (refused, too large, not
# one of ours, or too old), until the panel has said so.
REPORT_DROPPED_NAME = "report-dropped"
# A waiting report this old is dropped unsent: the problem it describes is
# long gone, and the person may well have forgotten agreeing to it.
REPORT_PENDING_MAX_AGE = 14 * 24 * 3600
# Seconds before a waiting report is tried again, by how the last try ended.
# Offline is checked against NetworkManager first, which costs one nmcli.
# Unreachable is a connection with no way through to the report service (no
# DNS, refused, timed out) while NetworkManager says connected.
REPORT_RETRY_AFTER = {
    "offline": 120, "unreachable": 900, "unavailable": 900, "unexpected": 3600,
    "rate_limited": 3600,
}
# curl's exit codes for a request that never got an answer through.
REPORT_NETWORK_RC = (6, 7, 28, 52, 55, 56)
# curl's exit codes for a TLS failure: a captive portal's certificate, or a
# clock far off. Not cured by waiting two minutes.
REPORT_TLS_RC = (35, 51, 58, 60)

# The wake-failure notice, read from the previous boot's kernel and
# NetworkManager log, bounded in lines and time.
WAKE_JOURNAL_MAX = "10000"
# How long the boot went on after the wake with no WiFi. Shorter, and it may
# simply have been switched off straight after waking.
WAKE_MIN_SECONDS = 30
# The driver's lines are read from just before the sleep to this long after
# the wake: a driver that fails at resume says so then, and a driver that
# keeps failing for hours would otherwise bury the wake in its own lines.
WAKE_DRIVER_WINDOW = 300
# With no "ACPI: PM: Waking up" line (s2idle), how long before "PM: suspend
# exit" the devices' resume is counted from: lines from going to sleep, at
# "PM: suspend entry", say nothing about the wake.
WAKE_EXIT_LEAD = 5
# "1712345678.123456 kernel: PM: suspend exit" with -o short-unix and
# --no-hostname. A host column, if a journalctl prints one anyway, is skipped.
_WAKE_LINE_RE = re.compile(
    r"^([0-9]{9,11}(?:\.[0-9]+)?) (?:\S+ )?(kernel|NetworkManager\[[0-9]+\]): (.*)$")
# mac80211 says this once the radio has joined an access point.
_WAKE_KERNEL_UP_RE = re.compile(r"^wl[\w.-]*: associated\b")
# NetworkManager saying a WiFi device came up, for drivers that do their own
# association and so print nothing above. WiFi interfaces are named wl*.
_WAKE_NM_UP_RE = re.compile(r"device \(wl[\w.-]*\): state change: \S+ -> activated\b")
# NetworkManager trying a saved network on a WiFi device. A failed wake no
# longer needs one (see _wake_failure_at); read as an attempt all the same,
# so a network named like a radio switch is not taken for one.
_WAKE_NM_ATTEMPT_RE = re.compile(
    r"device \(wl[\w.-]*\): (?:Activation: starting connection|"
    r"state change: \S+ -> (?:prepare|config|need-auth|failed)\b)")
# One of those attempts failing.
_WAKE_NM_FAILED_RE = re.compile(r"device \(wl[\w.-]*\): state change: \S+ -> failed\b")
# NetworkManager's own sleep and wake, and its overall state. One that was
# put to sleep and never woken (or never connected again) after the system
# woke is a failed wake that leaves no driver error at all.
_WAKE_NM_SLEEP_RE = re.compile(r"\bmanager: sleep: sleep requested\b")
_WAKE_NM_WAKE_RE = re.compile(r"\bmanager: sleep: wake requested\b")
_WAKE_NM_CONNECTED_RE = re.compile(r"\bNetworkManager state is now CONNECTED")
# WiFi switched off, by the user or a radio switch, and back on.
_WAKE_RADIO_OFF_RE = re.compile(
    r"(?i)rfkill.*\b(?:disabled|blocked)\b|wireless-enabled:off|\bradio\b.*\bdisabled\b")
_WAKE_RADIO_ON_RE = re.compile(
    r"(?i)rfkill.*\b(?:enabled|unblocked)\b|wireless-enabled:on|\bradio\b.*\benabled\b")
# Messages from the WiFi drivers this plugin knows, and mac80211's own. Not
# mmc, usb or anything else that also logs errors around a sleep. mhi is the
# bus the OLED's ath11k card talks over.
_WAKE_DRIVER_RE = re.compile(
    r"^(?:rtw\w*|ath1[0-2]k\w*|mt7\w*|iwlwifi|brcmfmac\w*|ieee80211 phy[0-9]+|mhi mhi[0-9]+)[ :]")
_WAKE_ERROR_RE = re.compile(
    r"(?i)\b(?:fail(?:ed|ure|s)?|error|timeout|timed out|crash(?:ed)?|unable|"
    r"not respond(?:ing)?|no response|halt(?:ed)?|restart)\b"
)
# Of a driver's lines with an error word, the ones that mean the card did
# not come back: powering on, the PCIe or MDIO registers, leaving its power
# save states, firmware that crashed, a resume that failed, and the OLED's
# MHI bus. rtw88 also logs "timed out to flush queue" or "failed to get tx
# report" on a card that works, and those count only when NetworkManager
# then tried a network and failed.
_WAKE_FATAL_RE = re.compile(
    r"(?i)\bpower (?:on|up)\b|failed to (?:read|write)(?: to)? (?:DBI|MDIO)|"
    r"hardware became unavailable|firmware crash|\bleav(?:e|ing) (?:lps|ips)\b|"
    r"failed to read ASPM|failed to poll|failed to resume|resume failed|^mhi mhi[0-9]+[ :]"
)
# A kernel WARN from a WiFi driver, mac80211 or cfg80211: the line that says
# where it fired. Its headline, the line before it, carries no driver name.
# Up to Linux 6.18 "WARNING: CPU: 1 PID: 239 at drivers/net/...", from 6.19
# "WARNING: drivers/net/...:105 at <function>, CPU#1: ..." or, with no file
# known, "WARNING: at <function> [module], CPU#1: ...".
_WAKE_WARN_RE = re.compile(
    r"^WARNING: (?:CPU: [0-9]+ PID: [0-9]+ at )?(?:drivers/net/wireless/|net/mac80211/|net/wireless/)"
    r"|^WARNING: at .*\[(?:rtw|ath1[0-2]k|mt7|iwl|mac80211|cfg80211)")
# Those headlines, for the failures that matter here. They are errors with no
# driver name and often no error word: mac80211's for a card that did not
# come back from resume, and rtw88's for a card lost in a power state or off
# the PCIe bus.
_WAKE_HEADLINES = ("Hardware became unavailable upon resume",
                   "failed to ack driver for leaving Deep Power mode",
                   "failed to read DBI register",
                   "failed to write to DBI register",
                   "failed to write to MDIO register",
                   "firmware crash, start reset and recover")
_WAKE_HEADLINE_RE = re.compile("|".join(re.escape(h) for h in _WAKE_HEADLINES))
# journalctl --grep patterns for the reads: every event but the driver's
# own lines, which come from the window around the wake instead.
_WAKE_EVENT_GREP = (r"PM: suspend (entry|exit)|ACPI: PM: Waking up|: associated|"
                    r"state change: |Activation: starting|rfkill|wireless-enabled|radio|"
                    r"sleep requested|wake requested|state is now")
# Everything _wake_events can count as a WiFi error, and nothing it cannot
# use, as one pattern journalctl --grep takes.
_WAKE_DRIVER_GREP = (
    r"^(rtw|ath1[0-2]k|mt7|iwlwifi|brcmfmac|ieee80211 phy[0-9]|mhi mhi[0-9])|"
    + _WAKE_WARN_RE.pattern + "|" + _WAKE_HEADLINE_RE.pattern)


def _wake_error_line(message: str) -> bool:
    """Whether a kernel line is a WiFi error the wake rule counts on its own:
    a WiFi driver's or mhi's line with an error word that is on the fatal
    list, a WARN from the WiFi stack, or one of the headlines above."""
    return bool((_WAKE_DRIVER_RE.match(message) and _WAKE_ERROR_RE.search(message)
                 and _WAKE_FATAL_RE.search(message))
                or _WAKE_WARN_RE.match(message) or _WAKE_HEADLINE_RE.search(message))


def _wake_soft_error_line(message: str) -> bool:
    """A WiFi driver's line with an error word that is not on the fatal list
    (see _WAKE_FATAL_RE): counted only beside a failed attempt to connect."""
    return bool(_WAKE_DRIVER_RE.match(message) and _WAKE_ERROR_RE.search(message))


def _report_url() -> str:
    """REPORT_URL when it is one the plugin will send to, otherwise ""."""
    url = REPORT_URL if isinstance(REPORT_URL, str) else ""
    return url if _REPORT_URL_RE.match(url) else ""


def _report_dir_fd(create: bool = False) -> int:
    """A descriptor for REPORT_DIR, refused unless it is the plugin's alone.

    Walked without links like the settings directory. Made 0700 when it is
    not, so nobody else can read a report or put anything in its place, and
    refused outright if someone else owns it. The caller closes it.
    """
    fd = _open_dir_no_follow(REPORT_DIR, create=create)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise PermissionError(f"{REPORT_DIR} belongs to someone else")
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


# Held by a write into REPORT_DIR and by its removal at uninstall, so a write
# that may not make the folder again (create False) cannot land between the
# removal of the files and the removal of the folder, leaving it behind.
_REPORT_FILES_LOCK = threading.Lock()


def _write_report_file(name: str, data: str, create: bool = True) -> bool:
    """Replace REPORT_DIR/name in one step with a file only root can read.

    A new file each time, opened without following links, then renamed over
    the old one, so a reader sees the old report or the new, never half. The
    temporary name is new each time too: two writers of the same file (the
    panel and the background watch dropping the same report) each rename
    their own whole file, and never write into each other's. create False:
    only into a REPORT_DIR that is there (see Plugin._write_report).
    """
    with _REPORT_FILES_LOCK:
        return _write_report_file_locked(name, data, create)


def _write_report_file_locked(name: str, data: str, create: bool) -> bool:
    try:
        dfd = _report_dir_fd(create=create)
    except FileNotFoundError:
        decky.logger.info(f"Report: {name} not written, {REPORT_DIR} is gone (uninstalling)")
        return False
    except OSError as e:
        decky.logger.error(f"Report: {REPORT_DIR} cannot be used: {e}")
        return False
    tmp = f"{name}.{os.urandom(6).hex()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                     dir_fd=dfd)
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        return True
    except OSError as e:
        decky.logger.error(f"Report: could not write {name}: {e}")
        try:
            os.unlink(tmp, dir_fd=dfd)
        except OSError:
            pass
        return False
    finally:
        os.close(dfd)


def _read_report_file(name: str, limit: int) -> tuple:
    """(text, modification time) of REPORT_DIR/name, or (None, 0).

    Only a regular file no larger than limit, read without following a link.
    """
    try:
        dfd = _report_dir_fd()
    except OSError:
        return None, 0
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        os.close(dfd)
        return None, 0
    try:
        st = os.fstat(fd)
        if not _is_regular(st.st_mode) or st.st_size > limit:
            return None, 0
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, limit + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            return None, 0
        return b"".join(chunks).decode("utf-8", "replace"), st.st_mtime
    except OSError:
        return None, 0
    finally:
        os.close(fd)
        os.close(dfd)


def _remove_report_file(name: str) -> bool:
    """Remove REPORT_DIR/name. True when it is gone, or was never there."""
    try:
        dfd = _report_dir_fd()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        os.unlink(name, dir_fd=dfd)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        decky.logger.error(f"Report: could not remove {name}: {e}")
        return False
    finally:
        os.close(dfd)


# A temporary file _write_report_file left behind (a write cut short), and
# the fixed name older versions used.
_REPORT_TMP_RE = re.compile(r"^[a-z-]+(?:\.json)?(?:\.[0-9a-f]{12})?\.tmp\Z")


def _forget_reports():
    """Remove REPORT_DIR and what is in it, for uninstalling."""
    with _REPORT_FILES_LOCK:
        _forget_reports_locked()


def _forget_reports_locked():
    names = [REPORT_PENDING_NAME, REPORT_SENT_NAME, REPORT_WAKE_SEEN_NAME,
             REPORT_WAKE_SENT_NAME, REPORT_DROPPED_NAME]
    try:
        dfd = _report_dir_fd()
    except OSError:
        dfd = None
    if dfd is not None:
        try:
            names += [name for name in os.listdir(dfd) if _REPORT_TMP_RE.match(name)]
        except OSError:
            pass
        finally:
            os.close(dfd)
    for name in dict.fromkeys(names):
        _remove_report_file(name)
    try:
        os.rmdir(REPORT_DIR)
    except OSError:
        pass


def _report_payload(info: dict, version: str, wake_failure: bool = False):
    """(payload, body) for diagnostics, or None if it cannot be made to fit.

    payload is {"plugin_version", "report"} as the receiving end takes it,
    and body is the exact text sent. The report is the diagnostics as they
    are plus wake_failure (whether the wake notice found a failed wake in
    the previous boot), unless they are too large: then the log sections go
    in REPORT_LOG_KEYS order, then whatever else is biggest, and "left_out"
    in the report names them.
    """
    report = dict(info)
    report["wake_failure"] = bool(wake_failure)
    dropped = []

    def size(value) -> int:
        return len(json.dumps(value, separators=(",", ":")))

    while size(report) > REPORT_MAX_BYTES:
        logs = [k for k in REPORT_LOG_KEYS if k in report]
        if logs:
            biggest = logs[0]
        else:
            candidates = [k for k in report if k not in REPORT_KEEP_KEYS]
            if not candidates:
                return None
            biggest = max(candidates, key=lambda k: size(report[k]))
        del report[biggest]
        dropped.append(biggest)
        report["left_out"] = ("Too large to send, so these were left out: "
                              + ", ".join(dropped))
    payload = {"plugin_version": version, "report": report}
    return payload, json.dumps(payload, separators=(",", ":"))


def _report_looks_real(report) -> bool:
    """Whether the receiving end will take report as a real one, by its own
    rule: success true or false, device_info an object, and plugin_version
    text, or in its place extras_error text. One it would refuse is never
    sent: a refusal drops the report for good."""
    if not isinstance(report, dict):
        return False
    if not isinstance(report.get("success"), bool) or not isinstance(report.get("device_info"), dict):
        return False
    if isinstance(report.get("plugin_version"), str):
        return True
    return "plugin_version" not in report and isinstance(report.get("extras_error"), str)


def _pending_body_valid(body: str) -> bool:
    """A waiting report read back from disk is one this plugin made."""
    if len(body) > REPORT_MAX_BYTES + 1024:
        return False
    try:
        data = json.loads(body)
    except ValueError:
        return False
    version = data.get("plugin_version") if isinstance(data, dict) else None
    return (isinstance(version, str) and len(version) <= 64
            and bool(VERSION_RE.match(version))
            and isinstance(data.get("report"), dict))


def _wake_events(text: str) -> list:
    """(time, kind) for the lines the wake rule reads, in time order.

    text is kernel and NetworkManager lines as journalctl -o short-unix
    prints them, from one read or several. Lines that do not parse are
    ignored; a line read twice counts once.
    """
    events = []
    for raw in dict.fromkeys(text.splitlines()):
        found = _WAKE_LINE_RE.match(raw)
        if not found:
            continue
        stamp, who, message = float(found.group(1)), found.group(2), found.group(3)
        kind = "line"
        if who == "kernel":
            if message.startswith("PM: suspend exit"):
                kind = "wake"
            elif message.startswith("PM: suspend entry"):
                kind = "sleep"
            elif message.startswith("ACPI: PM: Waking up from system sleep state"):
                kind = "resume"
            elif _WAKE_KERNEL_UP_RE.match(message):
                kind = "up"
            elif _wake_error_line(message):
                kind = "error"
            elif _wake_soft_error_line(message):
                kind = "soft_error"
        elif _WAKE_NM_UP_RE.search(message):
            kind = "up"
        elif _WAKE_NM_FAILED_RE.search(message):
            kind = "attempt_failed"
        elif _WAKE_NM_ATTEMPT_RE.search(message):
            kind = "attempt"
        elif _WAKE_NM_SLEEP_RE.search(message):
            kind = "nm_sleep"
        elif _WAKE_NM_WAKE_RE.search(message):
            kind = "nm_wake"
        elif _WAKE_NM_CONNECTED_RE.search(message):
            kind = "nm_connected"
        elif _WAKE_RADIO_OFF_RE.search(message):
            kind = "radio_off"
        elif _WAKE_RADIO_ON_RE.search(message):
            kind = "radio_on"
        events.append((stamp, kind))
    events.sort(key=lambda event: event[0])
    return events


def _wake_candidate(events: list) -> tuple | None:
    """(where the sleep began, where it woke) as indexes into events, for
    the first wake after WiFi was last up, or None.

    The first wake, not the last: someone whose WiFi did not come back often
    sleeps and wakes again to retry, and that second wake is not the one
    that failed. The sleep began at its "PM: suspend entry", or at "ACPI: PM:
    Waking up" when that line is missing, both after WiFi was last up.
    """
    ups = [i for i, (_t, kind) in enumerate(events) if kind == "up"]
    if not ups:
        return None
    wake = next((i for i in range(ups[-1] + 1, len(events)) if events[i][1] == "wake"), None)
    if wake is None:
        return None
    for kind in ("sleep", "resume"):
        found = [i for i in range(ups[-1] + 1, wake) if events[i][1] == kind]
        if found:
            return found[-1], wake
    return wake, wake


def _wake_failure_at(text: str, ended_at: float = 0) -> int:
    """When a wake left WiFi down for the rest of a boot, from its log.

    text is the kernel and NetworkManager lines of the boot as journalctl
    -o short-unix prints them; ended_at is when the boot ended, if known
    (otherwise its last line here). Returns the wake's time (Unix seconds),
    or 0. All of these must hold, so an ordinary boot never qualifies:
      - WiFi came up during the boot and a sleep followed; the wake judged
        is the first after WiFi was last up (see _wake_candidate), so WiFi
        never came back after it, whatever sleeps followed;
      - the WiFi stack logged an error from "ACPI: PM: Waking up" on (from
        WAKE_EXIT_LEAD before "PM: suspend exit" when that line is missing,
        as in s2idle), up to WAKE_DRIVER_WINDOW after the wake: a driver
        that fails to resume says so while devices resume, before "PM:
        suspend exit". That is a WiFi driver's or mhi's line on the fatal
        list, a WARN from the WiFi stack, or a headline such as "Hardware
        became unavailable upon resume" (see _wake_error_line); a driver's
        other error lines count only when NetworkManager then tried a
        network and failed. Lines from going to sleep do not count, nor do
        another driver's errors (mmc, usb) or a driver's ordinary lines
        (ath11k is probed afresh after a wake on a Steam Deck OLED).
        Or, with no such error: NetworkManager was put to sleep before the
        sleep and logged neither its wake nor a connected state after it
        (it missed the resume, and stays asleep);
      - the radio was on after the wake: still on when the boot ended, or on
        for WAKE_MIN_SECONDS or more before it was turned off. Turned off
        straight after waking and left off, nothing failed;
      - the boot went on for WAKE_MIN_SECONDS or more after the wake.
    NetworkManager trying a network is not needed: it tries only once a scan
    finds a saved network, and a radio that came back dead never scans. The
    driver's error is what tells a dead radio from being away from every
    saved network.
    """
    events = _wake_events(text)
    found = _wake_candidate(events)
    if not found:
        return 0
    start, wake = found
    woke_at = events[wake][0]
    resumed = [i for i in range(start, wake + 1) if events[i][1] == "resume"]
    if resumed:
        first = resumed[-1]
    else:
        first = next(i for i in range(start, wake + 1)
                     if events[i][0] >= woke_at - WAKE_EXIT_LEAD or i == wake)
    window = [kind for stamp, kind in events[first:] if stamp <= woke_at + WAKE_DRIVER_WINDOW]
    tried_and_failed = any(kind == "attempt_failed" for _s, kind in events[wake + 1:])
    driver_failed = "error" in window or ("soft_error" in window and tried_and_failed)
    ups = [i for i, (_t, kind) in enumerate(events) if kind == "up"]
    nm_slept = any(events[i][1] == "nm_sleep" for i in range(ups[-1] + 1, wake))
    nm_back = any(kind in ("nm_wake", "nm_connected") for _s, kind in events[wake + 1:])
    if not driver_failed and not (nm_slept and not nm_back):
        return 0
    radio_on, on_since, on_long = True, woke_at, False
    for stamp, kind in events[wake + 1:]:
        if kind == "radio_off" and radio_on:
            radio_on = False
            on_long = on_long or stamp - on_since >= WAKE_MIN_SECONDS
        elif kind == "radio_on" and not radio_on:
            radio_on, on_since = True, stamp
    if not (radio_on or on_long):
        return 0
    if max(events[-1][0], ended_at) - woke_at < WAKE_MIN_SECONDS:
        return 0
    return int(woke_at)


class Plugin:
    """Root plugin instance. Decky exposes every async method here as a
    callable from the frontend. Synchronous helpers prefixed with `_` are
    for internal use only."""

    # ---- Helpers ----

    # Tools that talk to the WiFi driver. One stuck in the kernel behind a
    # driver that wedged on a bad wake is left behind by _run_cmd, and every
    # status pass used to start another, each as stuck as the first.
    _STUCK_GUARDED = ("iw", "tc", "ip")

    def _run_cmd(self, cmd: list[str], timeout: float = 5) -> dict:
        """Run a subprocess and return a result dict.

        Every child gets _child_env(), so it loads the system's libraries
        rather than the ones bundled inside Decky. See _child_env for why
        this cannot be left to the commands that happen to need it.

        Never waits longer than the timeout and a second. subprocess.run
        kills a child that times out and then waits for it with no limit,
        and a child stuck in the kernel (iw or tc behind a WiFi driver that
        wedged on a bad wake) does not die from the kill. Called on the
        event loop, that froze every call the panel makes, for good. Such a
        child is left behind instead; Python reaps it if it ever exits.
        While one of iw, tc or ip is left behind that way, no other copy of
        the same tool is started (see _stuck_tools).

        Inside uninstall's time budget (_cmd_deadline, which _teardown sets)
        the timeout is cut to what is left of it, and a call with too little
        left is not started at all. Everything run then is uninstall's, or a
        poll of a plugin about to be gone.
        """
        name = os.path.basename(cmd[0])
        deadline = getattr(self, "_cmd_deadline", None)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < 0.5:
                return {"success": False, "stdout": "",
                        "stderr": "Skipped: out of time", "returncode": -1}
            timeout = min(timeout, remaining)
        if name in self._STUCK_GUARDED and self._stuck_for(name, cmd):
            return {"success": False, "stdout": "",
                    "stderr": f"previous {name} is still stuck", "returncode": -1}
        cmd = [_tool(cmd[0]), *cmd[1:]]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=_child_env(),
            )
        except FileNotFoundError:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Command not found: {cmd[0]}",
                "returncode": -1,
            }
        except Exception as e:
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return {
                "success": proc.returncode == 0,
                "stdout": stdout.strip(),
                "stderr": stderr.strip(),
                "returncode": proc.returncode,
            }
        except subprocess.TimeoutExpired:
            self._abandon(proc, cmd)
            return {
                "success": False,
                "stdout": "",
                "stderr": "Command timed out",
                "returncode": -1,
            }
        except Exception as e:
            self._abandon(proc, cmd)
            return {
                "success": False,
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
            }

    def _abandon(self, proc, cmd: list[str]):
        """Kill a child that overran, give it a second, then stop waiting."""
        try:
            proc.kill()
        except OSError:
            pass
        wait = 1.0
        deadline = getattr(self, "_cmd_deadline", None)
        if deadline is not None:
            wait = min(wait, max(0.1, deadline - time.monotonic()))
        try:
            proc.communicate(timeout=wait)
            return
        except Exception:
            pass
        # Our ends of its pipes are closed so they do not pile up, one per
        # stuck call.
        for pipe in (proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
        name = os.path.basename(cmd[0])
        if proc.poll() is None:
            # Still there after SIGKILL: stuck in the kernel. Kept, so no
            # other copy of the tool is started until this one exits.
            if name in self._STUCK_GUARDED:
                self._abandoned_record().append((name, proc))
            message = (f"{name} did not exit after being killed; left it behind, "
                       f"and no other {name} runs until it exits"
                       if name in self._STUCK_GUARDED else
                       f"{name} did not exit after being killed; left it behind")
        else:
            # It exited, but something it started (runuser's child, say)
            # still holds its output open.
            message = (f"{name} exited, but something it started still holds its "
                       "output; stopped waiting for it")
        try:
            self._log_throttled(f"abandon:{name}", message)
        except Exception:
            pass

    def _stuck_tools(self) -> set:
        """The tools (iw, tc, ip) with a child left behind that is still
        running. Ones that have since exited are dropped from the record.

        Worker threads and the event loop both use the record, so it is only
        ever appended to or removed from one entry at a time, never rebuilt:
        a rebuild could drop an entry another thread had just added.
        """
        record = self._abandoned_record()
        stuck = set()
        for entry in list(record):
            name, proc = entry
            if proc.poll() is None:
                stuck.add(name)
            else:
                try:
                    record.remove(entry)
                except ValueError:
                    pass
        return stuck

    @staticmethod
    def _iw_device(argv) -> str | None:
        """The interface an `iw dev <if> ...` call names, or None for any
        other call (iw reg, iw phy, ...)."""
        argv = list(argv or ())
        if len(argv) >= 3 and os.path.basename(str(argv[0])) == "iw" and argv[1] == "dev":
            return str(argv[2])
        return None

    def _stuck_for(self, name: str, cmd: list[str]) -> bool:
        """Whether a call of tool name with argv cmd must not start, since
        one left behind stuck would block it too. An `iw dev <if>` stuck in
        one card's driver does not stop iw for another card; any other
        stuck iw, or a new iw that names no interface, still does. tc and
        ip are by tool, as before."""
        if name not in self._stuck_tools():
            return False
        if name != "iw":
            return True
        wanted = self._iw_device(cmd)
        for entry in list(self._abandoned_record()):
            tool, proc = entry
            if tool != "iw" or proc.poll() is not None:
                continue
            held = self._iw_device(getattr(proc, "args", None))
            if wanted is None or held is None or held == wanted:
                return True
        return False

    def _abandoned_record(self) -> list:
        """(tool, child) for every iw, tc or ip left behind stuck. Made with
        setdefault, which two threads cannot both win."""
        return self.__dict__.setdefault("_abandoned_procs", [])

    def _get_wifi_interface(self) -> str | None:
        # Reading sysfs saves an nmcli call on most polls, but only when the
        # answer is unambiguous. With two wireless netdevs, listdir order would
        # decide which one we tune, and picking the wrong one is silent: every
        # sysfs path built from it resolves to nothing and the setters report
        # success while applying nothing. So one physical candidate or ask
        # NetworkManager, which knows which device it is managing.
        try:
            candidates = [
                iface for iface in sorted(os.listdir(SYSFS_NET))
                if IFACE_RE.match(iface)
                and os.path.exists(os.path.join(SYSFS_NET, iface, "phy80211"))
                and os.path.exists(os.path.join(SYSFS_NET, iface, "device"))
            ]
            if len(candidates) == 1:
                return candidates[0]
        except OSError:
            pass

        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "DEVICE,TYPE", "dev", "status"]
        )
        if not result["success"]:
            return None
        for line in result["stdout"].split("\n"):
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wifi":
                return parts[0]
        return None

    def _get_active_connection_uuid(self) -> str | None:
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "UUID,TYPE", "con", "show", "--active"]
        )
        if not result["success"]:
            return None
        for line in result["stdout"].split("\n"):
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "802-11-wireless":
                return parts[0]
        return None

    def _session_bus_cmd(self, args: list[str]) -> list[str] | None:
        """Wrap a command so it runs against the desktop user's session bus.

        steamosctl talks to steamos-manager on the session bus, but the plugin
        runs as root inside plugin_loader and root has no session of its own -
        invoking it directly fails with ENOENT, and connecting to the user's
        bus as root is refused during authentication. Dropping to the user with
        the bus address supplied explicitly is what actually works.
        """
        user = getattr(decky, "DECKY_USER", None) or "deck"
        try:
            uid = pwd.getpwnam(user).pw_uid
        except Exception:
            return None
        bus = f"/run/user/{uid}/bus"
        if not os.path.exists(bus):
            return None

        # Resolve both binaries rather than assuming /usr/bin. Fedora-based
        # systems put runuser in /usr/sbin, and PATH is not trustworthy here
        # because runuser rebuilds it from login.defs. Same approach the
        # modprobe lookup already uses further down.
        runuser = _first_existing(
            ["/usr/bin/runuser", "/usr/sbin/runuser", "/sbin/runuser"]
        )
        env_bin = _first_existing(["/usr/bin/env", "/bin/env"])
        if not runuser or not env_bin:
            return None

        return [
            runuser, "-u", user, "--",
            env_bin,
            f"XDG_RUNTIME_DIR=/run/user/{uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}",
        ] + args

    def _steamosctl_backend(self) -> str | None:
        """Read the backend through steamos-manager. None if unavailable."""
        if not os.path.isfile(STEAMOSCTL):
            return None
        cmd = self._session_bus_cmd([STEAMOSCTL, "get-wifi-backend"])
        if not cmd:
            return None
        result = self._run_cmd(cmd, timeout=5)
        if not result["success"]:
            return None
        # Currently "Wi-Fi backend: wpa_supplicant", but match on the value
        # rather than the wrapping. Requiring the colon would turn a harmless
        # change of wording into "steamos-manager is unavailable", silently
        # disabling this whole path on a machine that supports it.
        out = result.get("stdout", "")
        for candidate in ("wpa_supplicant", "iwd"):
            if candidate in out:
                return candidate
        return None

    # How many profiles each pin list will remember. The cap matters less for
    # memory than for teardown: clearing them is a synchronous nmcli call per
    # entry on the event loop, so an unbounded list is an unbounded stall.
    _MAX_TRACKED_LOCK_UUIDS = 16

    def _capped(self, known: list, prop: str) -> list:
        """known held to the cap above, dropping the oldest profiles first.

        Each one dropped has our change taken off it before it goes. Cut
        from the list still carrying it, it was a profile nothing would ever
        clear: Reset, uninstall and turning the feature off all work from
        this list. One that will not clear stays, past the cap if need be.
        Only reached when a list is already full, so it costs nothing on
        the usual path.
        """
        known = list(known)
        while len(known) > self._MAX_TRACKED_LOCK_UUIDS:
            oldest = known[0]
            if prop == "ipv4.dns":
                cleared = self._clear_dns(oldest)
            elif prop == "802-11-wireless-security.key-mgmt":
                cleared = self._restore_wpa3_only(oldest)
            else:
                done = self._nmcli_modify(
                    oldest, prop, "auto" if prop == "ipv6.method" else "", timeout=2
                )
                cleared = done["success"] or "unknown connection" in (
                    done.get("stderr") or "").lower()
            if not cleared:
                break
            known.pop(0)
        return known

    # How long the access point lock waits for an association before giving
    # up. Sized from a measured Deck OLED on wpa_supplicant, where a radio
    # cycle took 13-24 s to come back and the "NM says active, iw says
    # nothing" window ran about 20 s.
    _ASSOCIATION_WAIT_SECONDS = 30.0

    # How many times a reassertion may fail before it stops being attempted.
    # Reconciliation runs on the event loop, so an action that can never
    # succeed - a profile NetworkManager will not let us modify, a kernel with
    # no sch_cake - would otherwise block it for seconds at a time on every
    # poll, forever. Giving up leaves the drift flag standing, which is the
    # honest report anyway.
    _MAX_REASSERT_FAILURES = 3

    # How long to leave it alone after giving up, before allowing one more
    # attempt. Long enough that a permanently failing reassertion costs
    # almost nothing, short enough that a recovered one heals by itself.
    _REASSERT_COOLDOWN_SECONDS = 300

    _PROBE_RETRY_SECONDS = 30

    def _has_steamos_manager(self) -> bool:
        """Whether backend switching can go through steamos-manager.

        Only a POSITIVE result is cached. A negative one must not be, because
        the probe fails for reasons that are temporary rather than structural:
        the desktop session bus does not exist yet, or the call timed out. The
        first probe runs from _main at plugin start, which on a cold boot is
        exactly when the session bus is least likely to be up - caching that
        miss would route every backend switch for the rest of the session to
        the legacy helper, which is the failure this path exists to avoid.

        Retries are rate limited: this is no longer on the polling path, but
        start_backend_switch can reach it repeatedly.
        """
        if getattr(self, "_steamos_manager_available", False):
            return True
        if not os.path.isfile(STEAMOSCTL):
            return False

        now = time.monotonic()
        last = getattr(self, "_steamos_manager_probed_at", 0.0)
        if last and (now - last) < self._PROBE_RETRY_SECONDS:
            return False
        self._steamos_manager_probed_at = now

        available = self._steamosctl_backend() is not None
        if available:
            self._steamos_manager_available = True
            decky.logger.info("steamos-manager backend control available")
        return available

    def _get_backend_method(self) -> str:
        """Return 'steamos_manager', 'steamos', 'generic', or 'none'.

        Preference order matters. SteamOS 3.8 switches the backend through
        steamos-manager, and that is what the OS settings UI drives; the older
        polkit helper is still installed but no longer maintained, and its
        interface recovery hardcodes phy0, which does not exist on every
        device. Probe for the manager by capability rather than by OS version
        so this degrades cleanly on 3.7 and on non-SteamOS systems.
        """
        settings = _load_settings()
        distro = settings.get("distro_id", "unknown")
        if distro == "steamos" and self._has_steamos_manager():
            return "steamos_manager"
        if distro == "steamos" and os.path.isfile(BACKEND_HELPER) and os.access(BACKEND_HELPER, os.X_OK):
            return "steamos"
        if self._iwd_installed():
            return "generic"
        return "none"

    def _has_backend_tool(self) -> bool:
        return self._get_backend_method() != "none"

    def _get_backend_method_cached(self) -> bool:
        """Whether a backend switch looks possible, without probing.

        Called from the status collector, which runs in a worker thread and
        must not mutate, so this answers from files alone. It is deliberately
        more permissive than _get_backend_method - it does not check the
        helper is executable - so the panel can offer the control while the
        switch itself still declines. Erring the other way would hide a
        working control.
        """
        settings = _load_settings()
        if settings.get("distro_id") == "steamos" and os.path.isfile(BACKEND_HELPER):
            return True
        return self._iwd_installed()

    def _get_current_backend(self) -> str | None:
        """Return 'iwd', 'wpa_supplicant', or None if unknown.

        NetworkManager's own merged configuration first, so a file of the
        user's or the distro's that this plugin does not know about is read
        in NetworkManager's order; then the config files we know, in their
        priority order; then which systemd service is active.
        """
        found = self._merged_nm_backend_cached() or self._configured_backend_from_files()
        if found:
            return found
        # No config found, check which service is running
        result = self._run_cmd(["/usr/bin/systemctl", "is-active", "iwd"], timeout=3)
        if result.get("stdout", "").strip() == "active":
            return "iwd"
        result = self._run_cmd(
            ["/usr/bin/systemctl", "is-active", "wpa_supplicant"], timeout=3
        )
        if result.get("stdout", "").strip() == "active":
            return "wpa_supplicant"
        return None

    def _configured_backend_from_files(self, skip: str | None = None,
                                       only: str | None = None) -> str | None:
        """The backend the known conf files name, highest priority first.

        NetworkManager reads conf.d in name order and the later file wins, so
        Bazzite's iwd.conf outranks our 99-wifi-optimizer-backend.conf, which
        outranks Valve's 99-valve-wifi-backend.conf; /usr/lib defaults last.
        only: read just that one of them.
        """
        for path in (
            BAZZITE_IWD_CONF,
            GENERIC_BACKEND_CONF,
            WIFI_BACKEND_CONF,
            NM_DEFAULT_CONF,
            HOLO_NM_DEFAULT_CONF,
        ):
            if path == skip or (only is not None and path != only):
                continue
            try:
                with open(path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or line.startswith(";"):
                            continue
                        if line.startswith("wifi.backend"):
                            _, _, val = line.partition("=")
                            val = val.strip()
                            if val in ("iwd", "wpa_supplicant"):
                                return val
            except FileNotFoundError:
                continue
            except Exception:
                continue
        return None

    def _file_naming_backend(self, backend: str) -> str | None:
        """The highest-priority known conf file, other than ours, that names
        backend, or None."""
        for path in (BAZZITE_IWD_CONF, WIFI_BACKEND_CONF, NM_DEFAULT_CONF,
                     HOLO_NM_DEFAULT_CONF):
            if self._configured_backend_from_files(
                    skip=GENERIC_BACKEND_CONF, only=path) == backend:
                return path
        return None

    def _release_generic_backend_conf(self) -> bool:
        """Remove our backend conf file (Reset, uninstall) without stranding WiFi.

        A switch from here disabled the backend it switched away from, and
        that also removes the D-Bus alias NetworkManager starts wpa_supplicant
        through. Deleting only the file would send NetworkManager back to its
        default at the next start with that backend still disabled: no WiFi.
        So the backend it will fall back to is enabled and the one our file
        named is disabled. Nothing is stopped or restarted now, so the
        connection in use carries on and the change lands at the next boot.

        Enabled before the file goes, and the file kept if that fails. The
        other way round, an uninstall killed between the two, or an enable
        that did not take, left exactly the no-WiFi boot this exists to stop.

        Returns whether nothing of ours is left: False when the file was
        kept or could not be read or removed (Reset then says so).
        """
        try:
            with open(GENERIC_BACKEND_CONF) as f:
                text = f.read(4096)
        except FileNotFoundError:
            return True
        except OSError as e:
            decky.logger.error(f"Could not read our backend setting: {e}")
            return False
        found = re.search(r"^\s*wifi\.backend\s*=\s*(iwd|wpa_supplicant)\s*$", text, re.M)
        named = found.group(1) if found else None
        after = self._backend_without_ours() if named else None
        # --no-reload: the change only matters at the next boot, and the
        # daemon reload that enable and disable do by default can take
        # seconds that an uninstall does not have.
        if named and after != named:
            enabled = self._run_cmd(["/usr/bin/systemctl", "enable", "--no-reload", after],
                                    timeout=15)
            if not enabled["success"]:
                decky.logger.error(
                    f"Kept our backend setting ({named}): {after}, which NetworkManager "
                    f"would use without it, could not be enabled: "
                    f"{_tool_text(enabled.get('stderr', '')[:120])}"
                )
                return False
        try:
            os.remove(GENERIC_BACKEND_CONF)
        except FileNotFoundError:
            pass
        except OSError as e:
            decky.logger.error(f"Could not remove our backend setting: {e}")
            return False
        if not named or after == named:
            return True
        self._run_cmd(["/usr/bin/systemctl", "disable", "--no-reload", named], timeout=15)
        decky.logger.info(
            f"Removed our backend setting ({named}); NetworkManager uses {after} "
            f"from the next boot, so {after} is enabled and {named} disabled"
        )
        return True

    def _backend_without_ours(self) -> str:
        """The backend NetworkManager will use once our conf file is gone.

        Asked of NetworkManager itself, pointed at a scratch copy of its
        conf.d that leaves our file out. Our file is never moved: moving it
        aside for the moment the question took meant an uninstall killed in
        that moment left it renamed, which NetworkManager ignores, and the
        boot after that had no WiFi. Where NetworkManager cannot be asked,
        the known files are read in its order without ours.
        """
        conf_dir = os.path.dirname(GENERIC_BACKEND_CONF)
        ours = os.path.basename(GENERIC_BACKEND_CONF)
        found = None
        scratch = None
        try:
            # Root's own, made fresh, so nobody else can add to it.
            scratch = tempfile.mkdtemp(prefix="wifi-optimizer-nmconf-")
            for name in sorted(os.listdir(conf_dir)):
                if name != ours and name.endswith(".conf"):
                    os.symlink(os.path.join(conf_dir, name), os.path.join(scratch, name))
            found = self._merged_nm_backend(config_dir=scratch)
        except OSError as e:
            decky.logger.info(f"Could not ask NetworkManager which backend it falls back to: {e}")
        finally:
            if scratch:
                try:
                    for name in os.listdir(scratch):
                        os.unlink(os.path.join(scratch, name))
                    os.rmdir(scratch)
                except OSError:
                    pass
        return (found
                or self._configured_backend_from_files(skip=GENERIC_BACKEND_CONF)
                or "wpa_supplicant")

    def _release_power_save(self, settings: dict | None = None, reload_conf: bool = False) -> bool:
        """Undo Prevent lag spikes now, for Reset and uninstall.

        Removing our file alone left power save off until the next reboot:
        NetworkManager keeps the config it read at startup, and nothing put
        the radio's own power save back. It goes back on unless Steam's own
        WiFi power management setting has it off.

        Every step is tried whatever the one before it did. One that raised
        (a read-only file, a driver that refused) used to end the whole
        Reset there, with the rest left as it was. settings are the ones
        from before a Reset replaced them. reload_conf: the files were
        already released, without the reload, earlier in an uninstall.

        Returns whether everything came off: False when a file stayed, a
        step raised or the radio's power save could not be turned back on.
        """
        settings = settings if settings is not None else _load_settings()
        failed = []
        changed = self._release_power_save_files(settings, failed=failed)
        if reload_conf and not changed:
            self._reload_nm_conf()
        for what, undo in (("driver fixes", lambda: self._apply_driver_fixes(False)),
                           ("PCIe power states", lambda: self._apply_pcie_aspm_fix(False))):
            try:
                undo()
            except Exception as e:
                decky.logger.error(f"Undoing power save, {what}: {e}")
                failed.append(what)
        # Only if it was this plugin that had it off. Otherwise it is off for
        # a reason of the user's, and turning it on is not undoing anything.
        if not (settings.get("power_save_disabled") and self._volatile_gate_open(settings)):
            return not failed
        # Every radio set_power_save turned it off on (contract C2), judged
        # on the one the status reads; another failing is logged.
        status_if, managed_if = self._power_save_radios()
        judged = status_if or managed_if
        if judged and self._valve_powersave() != "2":
            for iface in dict.fromkeys((status_if, managed_if)):
                if not iface:
                    continue
                result = self._run_cmd(["/usr/bin/iw", "dev", iface, "set", "power_save", "on"])
                # No iw at all is not something another Reset would fix.
                if not result["success"] and not result["stderr"].startswith("Command not found"):
                    decky.logger.error(
                        f"Undoing power save, the radio's power save: {_tool_text(result['stderr'][:120])}")
                    if iface == judged:
                        failed.append("radio power save")
        return not failed

    def _release_power_save_files(self, settings: dict, reload: bool = True,
                                  failed: list | None = None) -> bool:
        """The files that hold power save off past a reboot: ours for
        NetworkManager, our value in Valve's, and the modprobe options.
        Returns whether NetworkManager has anything new to read.

        Valve's line goes before NetworkManager is told to read its config
        again. Reloaded first, it read our 2 still in Valve's file and kept
        power save off until the next reload.

        Valve's line is only looked at while our file is there: only then
        can a 2 in it be ours. Without one, a 2 is the user's own Steam
        setting (WiFi power management off), and Reset followed by an
        uninstall used to turn it back on.

        failed, when given, collects what could not be undone.
        """
        failed = failed if failed is not None else []
        changed = False
        ours = os.path.lexists(NM_CONF_PATH)
        try:
            os.remove(NM_CONF_PATH)
            changed = True
        except FileNotFoundError:
            pass
        except OSError as e:
            decky.logger.error(f"Undoing power save, our NetworkManager file: {e}")
            failed.append("our NetworkManager file")
        if ours:
            try:
                valve = self._valve_powersave()
                self._release_valve_powersave(settings)
                changed = changed or self._valve_powersave() != valve
            except Exception as e:
                decky.logger.error(f"Undoing power save, Valve's setting: {e}")
                failed.append("Valve's setting")
        try:
            os.remove(MODPROBE_CONF_PATH)
        except FileNotFoundError:
            pass
        except OSError as e:
            decky.logger.error(f"Undoing power save, the modprobe options: {e}")
            failed.append("the modprobe options")
        if changed and reload:
            self._reload_nm_conf()
        return changed

    def _valve_powersave_record(self) -> str:
        """Valve's wifi.powersave as valve_powersave_before records it: the
        value, "" for none, "?" for anything NetworkManager does not define.
        Recorded once nothing of ours is left in that file, so what it says
        then is the user's own."""
        found = self._valve_powersave()
        return "" if found is None else found if found in _VALVE_POWERSAVE_VALUES else "?"

    def _release_regdomain(self, settings: dict | None = None) -> bool:
        """Put back the wireless region from before the user chose one here.

        `iw reg set` lasts until reboot, so without this Reset and uninstall
        left the chosen region in force for the rest of the boot. Returns
        False when the region could not be put back.
        """
        settings = settings if settings is not None else _load_settings()
        if not settings.get("regdomain"):
            return True
        target = settings.get("regdomain_previous", "") or "00"
        if not REGDOMAIN_RE.match(target):
            return True
        result = self._run_cmd(["/usr/bin/iw", "reg", "set", target], timeout=5)
        if result["success"] or result["stderr"].startswith("Command not found"):
            # With no iw, no region was ever set to put back.
            return True
        decky.logger.error(f"Could not put the region back: {_tool_text(result['stderr'][:120])}")
        return False

    def _ensure_backend_switch_state(self):
        if not hasattr(self, "_backend_switch"):
            self._backend_switch = {
                "in_progress": False,
                "phase": "idle",
                "target": None,
                "started_at": 0,
                "result": None,
            }

    def _friendly_backend_error(self, detail: str) -> str:
        """Rewrite raw stderr into user-friendly guidance for common failures.
        Returns a one-line explanation; callers pass the raw detail separately
        so the technical text is still available to the UI/logs."""
        d = (detail or "").lower()
        if "symbol lookup error" in d or "undefined symbol" in d:
            return ("The WiFi backend switch ran into a mismatch between system files. "
                    "Restart the device, then try again.")
        if "permission denied" in d:
            return ("The system refused the WiFi backend switch. Restart the device, "
                    "then try again.")
        if "command not found" in d or "no such file" in d:
            return ("A system tool the WiFi backend switch needs is missing, so this "
                    "system may not support it.")
        if "timed out" in d or "timeout" in d:
            return ("The system didn't answer in time during the WiFi backend switch. "
                    "Try again in a moment.")
        if "network is unreachable" in d or "connection refused" in d:
            return ("The connection dropped during the WiFi backend switch. Check WiFi, "
                    "then try again.")
        return ("The WiFi backend switch didn't take effect. Restart the device, "
                "then try again.")

    def _require_wifi(self) -> tuple:
        iface = self._get_wifi_interface()
        if not iface:
            return None, None, {
                "success": False,
                "error": "no_wifi",
                "message": "Not connected to WiFi. Connect first, then try again.",
            }
        uuid = self._get_active_connection_uuid()
        if not uuid:
            return iface, None, {
                "success": False,
                "error": "no_wifi",
                "message": "Not connected to WiFi. Connect first, then try again.",
            }
        return iface, uuid, None

    def _get_saved_connection_uuid(self) -> str | None:
        """Get connection UUID from settings (for modifying saved profiles when disconnected)."""
        settings = _load_settings()
        return settings.get("last_connection_uuid") or settings.get("bssid_lock_connection_uuid") or None

    def _read_profile_field(self, uuid: str, field: str, timeout: int = 5) -> str:
        """What nmcli reports for one field of a saved profile, "" if nothing.

        Terse output is `field:value` and a value may contain colons of its
        own - a BSSID is nothing but colons - so the split is on the FIRST one
        only. Escaping is left to the caller: only some fields carry it, and
        unescaping one that does not would be guessing at the value.

        _get_profile_ssid and _profile_key_mgmt read the same shape but carry
        their own sentinels for an unreadable profile, so they stay separate.
        """
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", field, "con", "show", "uuid", uuid],
            timeout=timeout,
        )
        _, sep, value = result.get("stdout", "").partition(":")
        return value.strip() if sep else ""

    def _get_profile_ssid(self, uuid: str, timeout: int = 5) -> str | None:
        """SSID of a saved connection profile, or None if it cannot be read."""
        if not uuid:
            return None
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "802-11-wireless.ssid",
             "con", "show", "uuid", uuid],
            timeout=timeout,
        )
        if not result["success"]:
            return None
        _, sep, value = result.get("stdout", "").partition(":")
        return value.strip() if sep else None

    def _clear_profile_pins(self, deadline: float | None = None,
                            settings: dict | None = None) -> dict:
        """Remove the pins this plugin added from every profile it touched.

        The lock follows whichever profile NetworkManager uses, so discarding
        the settings without clearing them would leave profiles pinned to an
        access point with nothing left that knows about them - and a pinned
        profile fails to associate once that access point is out of range.

        The band preference is cleared alongside it, for the same reason: a
        profile left demanding a band its network does not offer will not
        connect, and after teardown there is nothing left to undo it.

        Returns the tracked lists of what is still pinned: a profile that
        refused the write, or that deadline (time.monotonic()) left no time
        for. Reset keeps those, so the leftover-pin cleanup finishes the job,
        rather than dropping the only record of them. Each call is an nmcli
        round trip, and Decky kills an uninstall that runs past five seconds.
        """
        settings = settings if settings is not None else _load_settings()
        left: dict = {"bssid_lock_uuids": [], "band_preference_uuids": [],
                      "ipv6_uuids": [], "dns_uuids": [], "wpa2_uuids": [],
                      "wpa2_restore_uuids": []}

        def budget(default: float, calls: int = 1) -> float | None:
            """The timeout for each of the next calls, or None when there is
            not time for all of them."""
            if deadline is None:
                return default
            remaining = deadline - time.monotonic()
            if remaining < 0.5 * calls:
                return None
            return min(default, remaining / calls)

        def cleared(result: dict) -> bool:
            # A profile that no longer exists has nothing left to undo.
            return result["success"] or "unknown connection" in (
                result.get("stderr") or "").lower()

        uuids = list(settings.get("bssid_lock_uuids", []))
        current = settings.get("bssid_lock_connection_uuid", "")
        if current and current not in uuids:
            uuids.append(current)
        last = settings.get("last_connection_uuid", "")
        # The profile last in use too, once this install has written a BSSID
        # pin: the lock follows whichever copy of a network is in use, and
        # the band sweep below and the iwd unpin already cover it.
        if (last and last not in uuids
                and "802-11-wireless.bssid" in settings.get("pins_owned", [])):
            uuids.append(last)
        # Unconditional. Gating on the toggle skipped exactly the profiles
        # left behind by turning it OFF, which is when they are stranded.
        for uuid in uuids:
            timeout = budget(5)
            if timeout is None:
                left["bssid_lock_uuids"].append(uuid)
                continue
            result = self._nmcli_modify(uuid, "802-11-wireless.bssid", "", timeout=timeout)
            decky.logger.info(
                f"Clearing BSSID lock from {uuid}: "
                f"{'ok' if result['success'] else 'not found'}"
            )
            if not cleared(result):
                left["bssid_lock_uuids"].append(uuid)
        band_uuids = list(settings.get("band_preference_uuids", []))
        # The profiles beyond the recorded list only if a band pin was ever
        # ours: otherwise a band set by hand would be wiped by Reset.
        if "802-11-wireless.band" in settings.get("pins_owned", []):
            band_uuids += uuids + [last]
        for uuid in dict.fromkeys(u for u in band_uuids if u):
            timeout = budget(2)
            if timeout is None or not cleared(
                    self._nmcli_modify(uuid, "802-11-wireless.band", "", timeout=timeout)):
                left["band_preference_uuids"].append(uuid)
        # Same reasoning as the pins: after teardown nothing is left that
        # knows this plugin turned IPv6 off, so it has to be undone here or
        # not at all. Bounded to profiles we recorded writing to.
        for uuid in dict.fromkeys(u for u in settings.get("ipv6_uuids", []) if u):
            timeout = budget(2)
            if timeout is None or not cleared(
                    self._nmcli_modify(uuid, "ipv6.method", "auto", timeout=timeout)):
                left["ipv6_uuids"].append(uuid)
        # And DNS, which no reset or uninstall used to touch at all: the
        # profile kept public servers and ignored the router's afterwards.
        # Budgeted as the three calls it makes, not one: given one call's
        # slot, the later ones ran past the deadline.
        for uuid in dict.fromkeys(u for u in settings.get("dns_uuids", []) if u):
            timeout = budget(2, calls=3)
            if timeout is None or not self._clear_dns(uuid, timeout=timeout):
                left["dns_uuids"].append(uuid)
        # And WPA3 only, where Allow WPA2 here widened a profile: nothing
        # after teardown would know to put it back. One that cannot be put
        # back now is retried by the status poll.
        wpa2 = list(settings.get("wpa2_uuids", [])) + list(settings.get("wpa2_restore_uuids", []))
        for uuid in dict.fromkeys(u for u in wpa2 if u):
            timeout = budget(2, calls=2)
            if timeout is None or not self._restore_wpa3_only(uuid, timeout=timeout):
                left["wpa2_restore_uuids"].append(uuid)
        return left

    def _restore_wpa3_only(self, uuid: str, timeout: float = 2) -> bool:
        """Put a profile Allow WPA2 here widened back to WPA3 only (sae).

        Only while it still says wpa-psk, the value written there: a profile
        whose security has been changed some other way since is the user's
        to keep. True when done, when there is nothing to undo, or when the
        profile no longer exists.
        """
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt",
             "con", "show", "uuid", uuid],
            timeout=timeout,
        )
        if "unknown connection" in (result.get("stderr") or "").lower():
            return True
        if not result["success"]:
            return False
        _, sep, value = result.get("stdout", "").partition(":")
        if not sep or value.strip() != "wpa-psk":
            return True
        done = self._nmcli_modify(uuid, "802-11-wireless-security.key-mgmt", "sae",
                                  timeout=timeout)
        return done["success"] or "unknown connection" in (done.get("stderr") or "").lower()

    def _clear_dns(self, uuid: str, timeout: float = 2) -> bool:
        """Give a profile back its automatic DNS. True when done, or when the
        profile no longer exists and there is nothing left to undo.

        The router's DNS is let back in before our servers come out, and
        nothing more is tried once that fails: the servers left in place
        then still resolve. Cleared regardless, as before, a profile ended
        up ignoring the router's DNS with no servers of its own, which
        reconnects with no resolvers at all. IPv6's automatic DNS comes back
        too, as custom DNS turns it off alongside IPv4's.
        """
        def gone(result: dict) -> bool:
            return "unknown connection" in (result.get("stderr") or "").lower()

        first = self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "no", timeout=timeout)
        if gone(first):
            return True
        if not first["success"]:
            return False
        rest = [
            self._nmcli_modify(uuid, "ipv6.ignore-auto-dns", "no", timeout=timeout),
            self._nmcli_modify(uuid, "ipv4.dns", "", timeout=timeout),
        ]
        return any(gone(r) for r in rest) or all(r["success"] for r in rest)

    # What a profile setter answers while another is still mid-change. Two at
    # once interleave their radio cycles and pin writes on one profile.
    _BUSY_RESULT = {
        "success": False,
        "error": "busy",
        "message": "Another WiFi change is still in progress. Try again in a moment.",
    }

    def _busy_result(self) -> dict:
        """_BUSY_RESULT, saying what it is waiting for.

        _profile_change_refused also refuses while an update installs and
        while the WiFi backend switches, and the panel shows no WiFi change
        running in either case, so "another WiFi change" sent people
        looking for one that was not there.
        """
        result = dict(self._BUSY_RESULT)
        if self._update_running():
            result["message"] = "An update is being installed. Try again once it finishes."
        elif self._backend_switch_running():
            result["message"] = "Wait for the WiFi backend switch to finish, then try again."
        return result

    _RADIO_OFF_RESULT = {
        "success": False,
        "error": "iw_failed",
        "message": "The WiFi radio didn't come back on. Try toggling WiFi in Steam settings.",
    }

    async def _reconnect(self, uuid: str | None = None) -> bool:
        """_hard_reconnect from a coroutine, in a worker thread.

        `nmcli con up` waits for the activation, up to ten seconds, and the
        radio commands up to five each against a NetworkManager that is slow
        to answer. On the event loop that froze every other call for as long.
        The setters that reconnect hold the reconciliation guard across this,
        so no poll writes to the profile while it runs.
        """
        return await asyncio.to_thread(self._hard_reconnect, uuid)

    def _hard_reconnect(self, uuid: str | None = None) -> bool:
        """Reconnect by cycling WiFi radio to fully reset NM connection state.

        Returns whether the radio came back on. Callers previously reported
        success regardless, so a failed `radio on` left the user with the
        radio off and a setter claiming it had reconnected - and there is no
        drift indicator for radio state to reveal it.
        """
        self._run_cmd(["/usr/bin/nmcli", "radio", "wifi", "off"])
        back = self._run_cmd(["/usr/bin/nmcli", "radio", "wifi", "on"])
        if not back["success"]:
            decky.logger.error(
                f"Could not turn the WiFi radio back on: "
                f"{_tool_text(back.get('stderr', '')[:120])}"
            )
            retry = self._run_cmd(["/usr/bin/nmcli", "radio", "wifi", "on"])
            if not retry["success"]:
                return False
        if uuid:
            self._run_cmd(["/usr/bin/nmcli", "con", "up", "uuid", uuid], timeout=10)
        return True

    # A driver profile is privileged configuration, not data. Everything in it
    # is consumed as root: the sysfs paths get opened for write, and the
    # modprobe options are written into a file the kernel acts on at every
    # module load, including at boot before anyone logs in. Both are validated
    # here rather than trusted, because a new adapter profile is the most
    # ordinary-looking change anyone could send and reviewing it by eye is not
    # a control.
    _SYSFS_FIX_RE = re.compile(r"^/sys/module/[A-Za-z0-9_]+/parameters/[A-Za-z0-9_]+\Z")
    _MODPROBE_OPT_RE = re.compile(
        r"^options [a-z0-9_]+(?: [a-z0-9_]+=[A-Za-z0-9,._-]+)+\Z"
    )

    def _safe_sysfs_fixes(self, profile: dict) -> list[str]:
        out = []
        for path in profile.get("sysfs_power_fixes", []):
            if isinstance(path, str) and self._SYSFS_FIX_RE.match(path):
                out.append(path)
            else:
                decky.logger.error(f"Refusing sysfs path outside /sys/module: {path!r}")
        return out

    def _safe_modprobe_options(self, driver: str, profile: dict) -> list[str]:
        # The grammar check alone would let a profile set a parameter on any
        # module in the kernel, from a diff that reads like a WiFi adapter
        # entry. Restrict it to modules belonging to this driver: an explicit
        # list where the names differ, otherwise anything sharing its prefix.
        allowed = profile.get("modules")
        out = []
        for opt in profile.get("modprobe_options", []):
            # `install`, `alias`, `softdep` and `remove` all let modprobe run a
            # shell command as root. Only parameter assignments are accepted.
            if not isinstance(opt, str) or not self._MODPROBE_OPT_RE.match(opt):
                decky.logger.error(f"Refusing modprobe directive: {opt!r}")
                continue
            module = opt.split()[1]
            permitted = (
                module in allowed if allowed else module.startswith(driver)
            )
            if not permitted:
                decky.logger.error(
                    f"Refusing modprobe option for {module!r}, which does not "
                    f"belong to the {driver!r} profile"
                )
                continue
            out.append(opt)
        return out

    def _apply_driver_fixes(self, enable: bool, persist: bool = True,
                            record_all: bool = False, ps_off: bool = False):
        """Apply or revert driver-specific power save fixes from DRIVER_PROFILES.
        Silently no-ops for drivers with no sysfs paths or modprobe options.

        persist=False leaves out the modprobe options, which take effect at
        the next module load and so cannot follow a streaming session.

        Reverting puts back what each parameter held before our first change
        this boot, or failing that what the system's own configuration gives
        it, and N only when nothing does. Writing N regardless turned off a
        disable_aspm=Y the user had set up themselves, which on an RTL8822CE
        is the remedy for WiFi that does not come back after sleep.

        record_all: as for _apply_pcie_aspm_fix.

        ps_off: the caller's iw turned power save off on the managed card in
        this same call (contract C10). A parameter in ASSOCIATED_ONLY_PARAMS
        is written only then, and only while that card's own netdev is up;
        otherwise it waits for the dispatcher at the next connect, which
        checks the same two things. Without iw, or with iw failing, power
        save may still be on and the link in LPS.
        """
        settings = _load_settings()
        profile = DRIVER_PROFILES.get(settings.get("driver"), {})
        paths = self._safe_sysfs_fixes(profile)

        if paths:
            # Claimed before the first write either way: a revert writes too,
            # and a boot whose first write was ours must still say what the
            # driver loaded with. Inside uninstall (_cmd_deadline) the marker
            # is made without running logger, to keep its time budget.
            self._claim_at_load(paths, log=getattr(self, "_cmd_deadline", None) is None)
        baseline = _read_pcie_baseline() if paths else {}
        known = set(baseline)
        if enable and self._record_param_baseline(paths, baseline, record_all):
            _save_pcie_baseline(baseline, set(baseline) - known)
        for path in paths:
            if enable:
                val = "Y"
                if path in ASSOCIATED_ONLY_PARAMS and ps_off is not True:
                    decky.logger.info(
                        f"{path} left for the next connect: power save not confirmed off"
                    )
                    continue
                if path in ASSOCIATED_ONLY_PARAMS and not self._wifi_associated():
                    decky.logger.info(
                        f"{path} left for the next connect: the radio is not associated"
                    )
                    continue
            else:
                val = baseline.get(path) or _param_from_config(path) or "N"
            try:
                with open(path, "w") as f:
                    f.write(val)
            except FileNotFoundError:
                pass
            except PermissionError:
                decky.logger.info(f"sysfs path not writable: {path}")

        options = self._safe_modprobe_options(settings.get("driver", ""), profile)
        if enable and options and persist:
            try:
                _write_system_conf(
                    MODPROBE_CONF_PATH,
                    "# WiFi Optimizer - driver power save fixes\n"
                    + "".join(opt + "\n" for opt in options),
                )
            except Exception as e:
                decky.logger.error(f"Failed to write modprobe config: {e}")
        elif not enable:
            try:
                os.remove(MODPROBE_CONF_PATH)
            except FileNotFoundError:
                pass

    def _apply_pcie_aspm_fix(self, enable: bool, record_all: bool = False) -> list[str]:
        """Disable or restore PCIe ASPM for the WiFi device.
        Prevents throughput degradation during sustained streaming.
        Works on all PCIe-attached WiFi adapters.

        Returns the names of the files that did not end up disabled, so the
        caller can say so. Each file is written and read back on its own:
        catching only two error types used to let any other one abort the
        loop part way, and nothing checked whether a write took. A file the
        device does not have is skipped (the OLED has no l0s_aspm). Restoring
        is not verified, because firmware can keep a state off that we ask
        for, which is not a failure of ours.

        record_all: in streaming auto mode nothing of ours writes these files
        before the first stream of a boot, so a value already matching ours
        is the system's own and is recorded too.
        """
        not_applied = []
        try:
            # Discover WiFi PCI device path dynamically: the card the driver
            # fixes are for, not a second radio NetworkManager may list first
            # (a USB adapter, whose power/control is no PCIe link of ours).
            iface = self._managed_interface()
            if not iface or not IFACE_RE.match(iface):
                return not_applied
            # A USB adapter has a power/control of its own, but no PCIe link
            # of ours to write, whatever its driver.
            if self._device_bus(iface) not in ("pci", ""):
                decky.logger.info(f"PCIe power states left alone: {iface} is not a PCIe card")
                return not_applied
            device_link = os.path.realpath(os.path.join(SYSFS_NET, iface, "device"))
            if not os.path.isdir(device_link):
                return not_applied

            targets = []
            link_dir = os.path.join(device_link, "link")
            if os.path.isdir(link_dir):
                val = "0" if enable else "1"
                for aspm_file in ["l0s_aspm", "l1_aspm", "l1_1_aspm", "l1_2_aspm",
                                   "l1_1_pcipm", "l1_2_pcipm"]:
                    targets.append((os.path.join(link_dir, aspm_file), val))
            # PCI runtime power management
            targets.append((
                os.path.join(device_link, "power", "control"),
                "on" if enable else "auto",
            ))

            # Restoring writes back what the system had, where we saw it
            # before our first change this boot, and leaves a file alone
            # where we did not: then it already held our value before we
            # wrote it. Writing "the kernel's usual value" instead turned on
            # states the firmware or driver keeps off (mt7921e turns its own
            # ASPM off), and streaming auto mode restores after every stream.
            baseline = _read_pcie_baseline()
            known = set(baseline)
            # Saved before the first write: a dispatcher's first run of the
            # boot that comes in between records the files as it finds them,
            # and after our write that is our value, not the system's.
            if enable and self._record_pcie_baseline(targets, baseline, record_all):
                _save_pcie_baseline(baseline, set(baseline) - known)
            for path, val in targets:
                if not os.path.exists(path):
                    continue
                if not enable:
                    if path not in baseline:
                        continue
                    val = baseline[path]
                try:
                    with open(path, "w") as f:
                        f.write(val)
                except OSError as e:
                    decky.logger.info(f"PCIe power write {path}={val}: {e}")
                if not enable:
                    continue
                try:
                    with open(path, "r") as f:
                        current = f.read().strip()
                except OSError:
                    current = None
                if current != val:
                    not_applied.append(os.path.basename(path))

            if not_applied:
                decky.logger.error(
                    f"PCIe power states still enabled on {device_link}: "
                    f"{', '.join(not_applied)}"
                )
            elif enable:
                decky.logger.info(f"PCIe ASPM disabled for {device_link}")
            else:
                decky.logger.info(f"PCIe ASPM restored for {device_link}")
        except Exception as e:
            decky.logger.error(f"PCIe ASPM fix error: {e}")
        return not_applied

    @staticmethod
    def _record_pcie_baseline(targets: list, baseline: dict, record_all: bool) -> bool:
        """Read every file before any is written. Turning L1 off makes the
        kernel report its substates off too, so reading each one after the
        write before it recorded them as off and kept them off on restore.
        Measured on a Deck OLED."""
        recorded = False
        for path, val in targets:
            if path in baseline or not os.path.exists(path):
                continue
            try:
                with open(path, "r") as f:
                    before = f.read(16).strip()
            except OSError:
                continue
            allowed = _PCIE_VALUES.get(os.path.basename(path), ("0", "1"))
            if before in allowed and (before != val or record_all):
                baseline[path] = before
                recorded = True
        return recorded

    @staticmethod
    def _record_param_baseline(paths: list, baseline: dict, record_all: bool) -> bool:
        """Note what each driver parameter holds before our first write this
        boot. A Y already there is only recorded when nothing of ours can
        have written it (record_all), and never when our own modprobe file
        gave it at load: that is our value, not the system's, and restoring
        it would leave the fix on after it was turned off."""
        ours = _ours_at_boot()
        recorded = False
        for path in paths:
            names = _param_name(path)
            if path in baseline or not names:
                continue
            if ours and _options_set(ours, *names) is not None:
                continue
            try:
                with open(path, "r") as f:
                    before = _kernel_bool(f.read(16).strip())
            except OSError:
                continue
            if before and (before != "Y" or record_all):
                baseline[path] = before
                recorded = True
        return recorded

    def _wifi_associated(self) -> bool:
        """Whether the radio the driver fixes are for is up and associated.

        That is the netdev bound to the settings' driver (_driver_interface),
        never a stand-in for it. With a second radio (a USB adapter) the
        first one NetworkManager lists can be the other, and when the card's
        own netdev is gone (an iwd restart) _managed_interface falls back to
        it: its link said nothing about whether this driver's card idles.
        """
        iface = self._driver_interface()
        if not iface or not IFACE_RE.match(iface):
            return False
        try:
            with open(os.path.join(SYSFS_NET, iface, "operstate"), "r") as f:
                return f.read(16).strip() == "up"
        except OSError:
            return False

    @staticmethod
    def _module_profile(module: str) -> str:
        """The DRIVER_PROFILES key a kernel module belongs to, or the module
        itself (rtw88_8822ce is rtw88), as _detect_wifi_driver names it."""
        if module in DRIVER_PROFILES:
            return module
        return next((key for key in DRIVER_PROFILES if module.startswith(key)), module)

    @staticmethod
    def _device_bus(name: str) -> str:
        """The bus a netdev's device sits on: "pci" for a built-in card,
        "usb" for an adapter, "" when sysfs does not say."""
        path = os.path.join(SYSFS_NET, name, "device", "subsystem")
        # realpath of a missing path is the path itself, not an error.
        if not os.path.exists(path):
            return ""
        return os.path.basename(os.path.realpath(path))

    @staticmethod
    def _wireless_netdevs() -> list:
        """Every wireless netdev with a device behind it, sorted by name."""
        try:
            names = sorted(os.listdir(SYSFS_NET))
        except OSError:
            return []
        return [name for name in names
                if IFACE_RE.match(name)
                and os.path.exists(os.path.join(SYSFS_NET, name, "phy80211"))
                and os.path.exists(os.path.join(SYSFS_NET, name, "device"))]

    def _driver_interface(self) -> str | None:
        """The wireless netdev of the card the settings' driver profile is
        for, or None when no single one is (or the driver is not known).

        That card is the one whose module belongs to the profile AND that
        sits on PCI. rtw88 also has in-kernel USB drivers (rtw88_8821cu,
        rtw88_8822bu ...), and a dongle of the same family is not the card
        the PCIe fixes and the driver parameters are about."""
        driver = _load_settings().get("driver", "")
        if driver not in DRIVER_PROFILES:
            return None
        found = []
        for name in self._wireless_netdevs():
            module = os.path.join(SYSFS_NET, name, "device", "driver", "module")
            if not os.path.exists(module):
                continue
            if (self._module_profile(os.path.basename(os.path.realpath(module))) == driver
                    and self._device_bus(name) == "pci"):
                found.append(name)
        return found[0] if len(found) == 1 else None

    def _managed_interface(self) -> str | None:
        """The wireless netdev of the card the driver profile manages
        (contract C2): _driver_interface, or when the driver is not known or
        no such netdev exists, the one _get_wifi_interface picks."""
        return self._driver_interface() or self._get_wifi_interface()

    def _pci_wifi_interface(self) -> str | None:
        """The wireless netdev on PCI, for telling which card a device is
        built around. A USB adapter NetworkManager lists first is not it.
        With several on PCI, the one NetworkManager picks among them."""
        pci = [name for name in self._wireless_netdevs() if self._device_bus(name) == "pci"]
        if len(pci) <= 1:
            return pci[0] if pci else None
        picked = self._get_wifi_interface()
        return picked if picked in pci else pci[0]

    def _managed_phy(self) -> str | None:
        """The wiphy of the managed card, from /sys/class/ieee80211, which
        outlives its netdev (a backend restart destroys the netdev). None
        when the driver is not known or no such wiphy is there."""
        driver = _load_settings().get("driver", "")
        if driver not in DRIVER_PROFILES:
            return None
        try:
            phys = sorted(os.listdir(SYSFS_IEEE80211))
        except OSError:
            return None
        for phy in phys:
            device = os.path.join(SYSFS_IEEE80211, phy, "device")
            module = os.path.join(device, "driver", "module")
            subsystem = os.path.join(device, "subsystem")
            if not (os.path.exists(module) and os.path.exists(subsystem)):
                continue
            if (self._module_profile(os.path.basename(os.path.realpath(module))) == driver
                    and os.path.basename(os.path.realpath(subsystem)) == "pci"):
                return phy
        return None

    def _interface_back(self) -> str | None:
        """The WiFi interface a backend restart is waiting for: the managed
        card's netdev when that card is known, any wireless one otherwise.
        A USB adapter's netdev coming back is not the internal card back."""
        if self._managed_phy() is not None:
            return self._driver_interface()
        return self._get_wifi_interface()

    def _log_params_at_load(self, paths: list):
        """Log the driver parameters as the driver loaded them, once a boot
        (contract C12: a call to _claim_at_load)."""
        self._claim_at_load(paths)

    def _claim_at_load(self, paths: list | None = None, log: bool = True) -> bool:
        """Claim the boot's "driver parameters at load" line (contract C12).
        True when this call created the marker.

        The diagnostics read this line back after the reboot that follows a
        failed wake ("driver parameters at load:", journal tag
        wifi-optimizer), and it is the only record of whether the fixes were
        in force for that boot. Whoever is first to write a driver parameter
        or last_enforced in a boot (this plugin or the dispatcher) creates
        AT_LOAD_MARKER and logs it, so every first writer calls this before
        it writes. last_enforced with no marker was written by a version from
        before the marker, whose values are already ours, not the driver's at
        load: then nothing is logged.

        paths: the parameters to read, by default the settings' driver
        profile's. Read now, so the values are the ones before the caller's
        write. With no profile the line says the values are not known.
        log=False creates the marker without running logger (uninstall,
        which has a time budget to keep). Values only: nothing here names a
        network or a device.
        """
        try:
            os.makedirs(os.path.dirname(AT_LOAD_MARKER), mode=0o755, exist_ok=True)
            fd = os.open(AT_LOAD_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644)
            os.close(fd)
        except OSError:
            # Already claimed this boot, or /run cannot be written.
            return False
        if os.path.exists(ENFORCED_FILE) or not log:
            return True
        driver = None
        if paths is None:
            driver = _load_settings().get("driver", "")
            paths = self._safe_sysfs_fixes(DRIVER_PROFILES.get(driver, {}))
        if paths:
            values = []
            for path in paths:
                try:
                    with open(path, "r") as f:
                        value = f.read(16).strip()
                except OSError:
                    value = "missing"
                values.append(f"{os.path.basename(path)}={value}")
            line = "driver parameters at load: " + " ".join(values)
        elif driver in DRIVER_PROFILES:
            line = "driver parameters at load: none (the driver profile has no parameters)"
        else:
            line = "driver parameters at load: not known (no driver profile)"
        result = self._run_cmd(["/usr/bin/logger", "-t", JOURNAL_TAG, "--", line], timeout=3)
        if not result.get("success"):
            decky.logger.error(
                f"Could not log the driver parameters at load: {_tool_text((result.get('stderr') or '')[:120])}")
        return True

    @staticmethod
    def _dispatcher_temp() -> str:
        """Where _install_dispatcher writes the script before renaming it."""
        return os.path.join(
            os.path.dirname(DISPATCHER_PATH),
            "." + os.path.basename(DISPATCHER_PATH) + ".new",
        )

    def _install_dispatcher(self) -> bool:
        if getattr(self, "_tearing_down", False):
            decky.logger.info("Dispatcher not installed: the plugin is being uninstalled")
            return False
        tmp = self._dispatcher_temp()
        try:
            template_path = os.path.join(
                decky.DECKY_PLUGIN_DIR, "defaults", "dispatcher.sh.tmpl"
            )
            with open(template_path, "r") as f:
                script = f.read()
            # Quote both, rather than trusting that neither path will ever
            # contain a character the shell reads as syntax. They come from
            # Decky's environment, not from us.
            script = script.replace("__SETTINGS_PATH__", _sh_quote(SETTINGS_FILE))
            script = script.replace(
                "__PLUGIN_DIR__", _sh_quote(decky.DECKY_PLUGIN_DIR)
            )
            # Written beside it and renamed over it. NetworkManager runs
            # this on every connect, and writing in place let a connect that
            # landed mid-write run half a script. Hidden and not executable
            # until complete, so the dispatcher never picks up the temp file.
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(script)
            os.chmod(tmp, 0o755)
            os.replace(tmp, DISPATCHER_PATH)
            decky.logger.info("Dispatcher script installed")
            return True
        except Exception as e:
            decky.logger.error(f"Failed to install dispatcher: {e}")
            # Not left behind: once chmod ran it is a root script in
            # dispatcher.d that nothing else would remove.
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

    def _remove_dispatcher(self) -> bool:
        # A temp file an earlier install left behind goes too.
        try:
            os.remove(self._dispatcher_temp())
        except OSError:
            pass
        try:
            os.remove(DISPATCHER_PATH)
            decky.logger.info("Dispatcher script removed")
            return True
        except FileNotFoundError:
            return True
        except Exception as e:
            decky.logger.error(f"Failed to remove dispatcher: {e}")
            return False

    def _rotate_logs(self, keep: int = 10):
        """Prune old log files on plugin startup. Decky does not rotate plugin
        logs automatically; each plugin load creates a new timestamped file in
        DECKY_PLUGIN_LOG_DIR, so without pruning they accumulate forever.
        Keep the newest `keep` files. This runs at plugin start only, and says
        nothing about the growth of the log currently being written.
        """
        try:
            log_dir = getattr(decky, "DECKY_PLUGIN_LOG_DIR", None)
            if not log_dir:
                return
            # The log directory is the user's. Opened without following links
            # anywhere along it, and worked in relative to that, so a link
            # swapped in cannot point these deletions somewhere else.
            try:
                dfd = _open_dir_no_follow(log_dir)
            except OSError:
                return
            try:
                self._prune_logs(dfd, log_dir, keep)
            finally:
                os.close(dfd)
        except Exception as e:
            decky.logger.error(f"Log rotation error: {e}")

    def _prune_logs(self, dfd: int, log_dir: str, keep: int):
        """_rotate_logs, inside the log directory as opened."""
        files = []
        for name in os.listdir(dfd):
            if not name.endswith(".log"):
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                continue
            if _is_regular(st.st_mode):
                files.append((st.st_mtime, name))
        if len(files) <= keep:
            return
        files.sort(reverse=True)
        current_log = getattr(decky, "DECKY_PLUGIN_LOG", None)
        removed = 0
        for _mtime, name in files[keep:]:
            # Paranoia: never delete the file we're currently writing to.
            if current_log and os.path.realpath(os.path.join(log_dir, name)) \
                    == os.path.realpath(current_log):
                continue
            try:
                os.unlink(name, dir_fd=dfd)
                removed += 1
            except Exception:
                pass
        if removed:
            decky.logger.info(f"Rotated logs: removed {removed} old file(s), kept {keep} newest")

    # ---- Lifecycle ----

    async def _main(self):
        try:
            decky.logger.info("WiFi Optimizer starting")
            self._acknowledge_update()
            self._rotate_logs()
            self._ensure_backend_switch_state()
            await asyncio.to_thread(self._reconcile_backend_switch)
            info = await self.get_device_info()
            # Read here so the status collector can use it without forking.
            # Before the settings are loaded: anything saved during an await
            # between loading and saving them would be overwritten.
            await asyncio.to_thread(self._nm_version)
            # `iw reg set` does not survive a reboot, so a region the user
            # chose has to be put back here. This runs when the plugin loads,
            # which can be after the first association - so the very first
            # connection of a boot may still use the old domain. In a worker
            # thread: on a wedged driver it is the first iw of the load, and
            # it held the event loop for its whole timeout.
            chosen_region = _load_settings().get("regdomain", "")
            if chosen_region and REGDOMAIN_RE.match(chosen_region):
                await asyncio.to_thread(
                    self._run_cmd, ["/usr/bin/iw", "reg", "set", chosen_region], 5
                )
            # The migrations ask nmcli, so they run in a worker thread too,
            # on a copy; what they changed is laid over the settings loaded
            # below, so nothing saved meanwhile is overwritten.
            migrated = await asyncio.to_thread(self._migration_changes)
            settings = _load_settings()
            settings.update(migrated)
            settings["model"] = info.get("model", "unknown")
            settings["device_family"] = info.get("device_family", "unknown")
            settings["device_label"] = info.get("device_label", "Unknown Device")
            # The radio can be missing at load (a reload while WiFi is down
            # after a bad wake). Keep what was detected before rather than
            # recording "unknown", which turns off the driver's fixes.
            if info.get("driver", "unknown") != "unknown" or settings.get("driver") in (
                    None, "", "unknown"):
                settings["driver"] = info.get("driver", "unknown")
                settings["chip_label"] = info.get("chip_label", "unknown")
                settings["supports_6ghz"] = info.get("supports_6ghz", False)
            distro = self._detect_distro()
            settings["distro_id"] = distro["id"]
            settings["distro_name"] = distro["name"]
            if settings.get("auto_fix_on_wake", True):
                if not self._install_dispatcher():
                    # Don't leave the toggle reading on with nothing installed.
                    # There is no drift indicator for auto-fix, so a silent
                    # failure here is invisible until WiFi stops being fixed.
                    # Decided before the save below on purpose: correcting it
                    # afterwards is discarded, and the volatile-apply block
                    # then re-reads the stale value and writes it back.
                    settings["auto_fix_on_wake"] = False
                    # And not leave an older version's script behind, still
                    # run by NetworkManager on every connect under a toggle
                    # that now reads off.
                    self._remove_dispatcher()
                    decky.logger.error(
                        "Auto-fix disabled: the dispatcher could not be installed"
                    )
            else:
                # An older version may have left its script there with the
                # toggle off, and NetworkManager would go on running it.
                self._remove_dispatcher()

            # Best effort. A settings directory that cannot be written (full,
            # read-only, a link refused) used to end startup here, skipping
            # the streaming pickup, the reapply after a reboot and the rest.
            # These read the settings back from the file, so they go on with
            # what it last held; the panel is told the save failed.
            try:
                _save_settings(settings)
            except OSError as e:
                self._log_throttled("settings_save", f"Startup could not save settings: {e}")
            # After the save: both read the settings from disk, and before it
            # they saw the un-migrated copy, which knows nothing of the pins
            # an older version left (and the unpin then latches for the boot).
            await asyncio.to_thread(self._heal_orphaned_iwd_conf)
            await self._unpin_if_iwd()

            # Prevent lag spikes off, but the files that hold power save off
            # past a reboot still there: a Reset or a toggle-off cut short
            # by a reload, or the plugin stopped part way. Nothing else
            # looks at them with the setting off, so they would hold power
            # save off for good under a toggle that reads off. Auto mode is
            # left to _startup_streaming, which drops them its own way.
            current = _load_settings()
            if not current.get("power_save_disabled") and not current.get(
                    "streaming_mode_enabled"):
                try:
                    if await asyncio.to_thread(self._release_power_save_files, current):
                        decky.logger.info(
                            "Prevent lag spikes is off; removed the power save files "
                            "a change cut short had left"
                        )
                except Exception as e:
                    decky.logger.error(f"Startup power save file check failed: {e}")

            # On its own: a step in it that fails (a settings save, say) used
            # to end startup here, skipping the startup apply below, the
            # band scope adoption and the backend check, and leaving auto
            # mode on with no watch for the rest of the session.
            try:
                await self._startup_streaming(_load_settings())
            except Exception as e:
                decky.logger.error(f"Startup streaming pickup failed: {e}")
            if _load_settings().get("streaming_mode_enabled"):
                self._start_streaming_watch()

            # Apply volatile settings that may have been lost on reboot.
            # The dispatcher handles reconnects, but on a fresh boot WiFi
            # connects before the plugin starts, so we apply here too.
            # Order: buffer tuning first (sets txqueuelen), then CAKE
            # (overrides txqueuelen to 256), then power_save last (sticks
            # after any reconnects the dispatcher might trigger).
            # Decided from the settings as they are now, not as read at the
            # top: a Reset or a toggle turned off during the awaits since
            # then would otherwise be applied again and saved back on.
            # Each setter runs as a re-application (reapply=True), which reads
            # the settings again when its turn comes: a Reset holding the
            # guards when this got here wins over the read just below.
            iface = await asyncio.to_thread(self._get_wifi_interface)
            fresh = _load_settings()
            if iface and self._volatile_gate_open(fresh):
                try:
                    _applied, total, stamps, skipped = await self._reapply_volatile_steps(
                        fresh, power_save_first=False)
                    if total:
                        await asyncio.to_thread(self._claim_at_load)
                        self._record_reapplied(fresh.get("last_applied", 0), stamps, skipped)
                except Exception as e:
                    decky.logger.error(f"Startup apply failed: {e}")

            # Someone upgrading has a band preference with no record of which
            # network it belongs to. Treating that as unscoped would keep
            # enforcing it everywhere, which is what stranded profiles in the
            # first place, so adopt the current network. If that guess is
            # wrong the preference simply stops applying and one toggle fixes
            # it - far better than writing a band into networks that have none.
            # Fresh: the setters above saved their own changes since the read
            # at the top, and saving that older copy would undo them.
            settings = _load_settings()
            if settings.get("band_preference_enabled") and not settings.get(
                "band_preference_ssid"
            ):
                adopted = await asyncio.to_thread(self._current_network_name, settings)
                # Read again after that wait, so nothing saved during it is
                # undone, and only acted on if it still needs it.
                settings = _load_settings()
                if settings.get("band_preference_enabled") and not settings.get(
                        "band_preference_ssid"):
                    if adopted:
                        settings["band_preference_ssid"] = adopted
                        message = "Band preference scoped to the current network on upgrade"
                    else:
                        # Leaving it enabled with no network attached is the
                        # state that lets it be applied to the wrong one. Turn
                        # it off so the toggle tells the truth; one tap
                        # re-enables it against whatever network the user is
                        # actually on.
                        settings["band_preference_enabled"] = False
                        message = ("Band preference disabled: could not establish which "
                                   "network it belongs to")
                    # Best effort, like the save above: the rest of startup
                    # does not depend on it, and the next start tries again.
                    try:
                        _save_settings(settings)
                        decky.logger.info(message)
                    except OSError as e:
                        self._log_throttled("settings_save",
                                            f"Startup could not save settings: {e}")

            # Sanity check: does the conf-declared backend match what's actually
            # running? Divergence would indicate a previous switch got interrupted
            # (plugin_loader crash, external tool, etc.). Log only; user can
            # re-toggle to resolve.
            # Warm the steamos-manager probe here, off the polling path, so
            # the collector never has to fork or memoise from its thread.
            backend_method = await asyncio.to_thread(self._get_backend_method)
            if backend_method != "none":
                await asyncio.to_thread(self._log_backend_inconsistency)

            decky.logger.info(
                f"WiFi Optimizer ready: device={info.get('device_label')}, "
                f"family={info.get('device_family')}, driver={info.get('driver')}, "
                f"chip={info.get('chip_label')}, distro={distro['id']}"
            )
        except Exception as e:
            decky.logger.error(f"WiFi Optimizer _main error: {e}")
        self._stranded_task = asyncio.create_task(self._stranded_watch())

    def _current_network_name(self, settings: dict) -> str | None:
        """The network the last profile in use, or the one in use now,
        belongs to, for adopting a band preference saved without one."""
        return self._get_profile_ssid(
            settings.get("last_connection_uuid", "")
        ) or self._get_profile_ssid(
            self._get_active_connection_uuid() or ""
        )

    def _log_backend_inconsistency(self):
        """Log when the backend the config names is not the one running.
        Runs in a worker thread: both questions are subprocesses."""
        conf_backend = self._get_current_backend()
        if not conf_backend:
            return
        active = self._run_cmd(
            ["/usr/bin/systemctl", "is-active", conf_backend], timeout=3
        )
        state = (active.get("stdout") or "").strip()
        if state and state != "active":
            decky.logger.error(
                f"Backend inconsistency: conf says '{conf_backend}' "
                f"but systemd reports '{state}'. Likely an interrupted "
                f"backend switch - user can retry via the UI."
            )

    _SETTINGS_VERSION = 3

    def _migrate_settings(self, settings: dict):
        """Bring settings from an older version up to what this one tracks.

        Versions before the tracking lists recorded only the one profile in
        use, so an upgrade arrives with pins on profiles nothing knows about.
        Seeded here so the safety nets that walk those lists (the stranded
        pin release, turning a feature off everywhere) cover them too. And
        whether a kind of pin was ever ours is inferred from what the old
        settings show, because a pin with no such evidence is treated as set
        by hand and left alone.
        """
        version = settings.get("settings_version", 0)
        if not isinstance(version, int) or version >= self._SETTINGS_VERSION:
            return
        if version < 2:
            self._migrate_to_v2(settings)
        if version < 3:
            self._migrate_to_v3(settings)
        settings["settings_version"] = self._SETTINGS_VERSION
        decky.logger.info(f"Settings migrated to version {self._SETTINGS_VERSION}")

    def _migration_changes(self) -> dict:
        """_migrate_settings run on a copy of the saved settings: the keys it
        changed, with their new values. For _main, which runs it in a worker
        thread and lays the result over a fresh read."""
        before = _load_settings()
        after = copy.deepcopy(before)
        self._migrate_settings(after)
        return {k: v for k, v in after.items() if before.get(k, object()) != v}

    def _migrate_to_v3(self, settings: dict):
        """Custom DNS set by a version before it covered IPv6 left the
        router's IPv6 DNS let in (ipv6.ignore-auto-dns no), and the drift
        check then told every such user their DNS no longer matched. The
        profile is brought up to date here instead, without a reconnect:
        it takes effect at the next connect."""
        if not settings.get("dns_enabled"):
            return
        for uuid in settings.get("dns_uuids", []):
            if not isinstance(uuid, str) or not uuid:
                continue
            done = self._nmcli_modify(uuid, "ipv6.ignore-auto-dns", "yes", timeout=3)
            if not done["success"]:
                decky.logger.info(
                    f"Could not bring {uuid}'s IPv6 DNS up to date: "
                    f"{_tool_text((done.get('stderr') or '')[:120])}")

    def _migrate_to_v2(self, settings: dict):
        owned = list(settings.get("pins_owned", []))
        if any(settings.get(k) for k in ("bssid_lock_enabled", "bssid_lock_uuids",
                                         "bssid_lock_connection_uuid", "bssid_lock_value")):
            owned = _with_pin({"pins_owned": owned}, "802-11-wireless.bssid")
        if any(settings.get(k) for k in ("band_preference_enabled",
                                         "band_preference_uuids", "band_preference_ssid")):
            owned = _with_pin({"pins_owned": owned}, "802-11-wireless.band")
        settings["pins_owned"] = owned

        lock_uuid = settings.get("bssid_lock_connection_uuid", "")
        if settings.get("bssid_lock_enabled") and lock_uuid and not settings.get("bssid_lock_uuids"):
            settings["bssid_lock_uuids"] = [lock_uuid]

        last = settings.get("last_connection_uuid", "")
        if (settings.get("band_preference_enabled") and last
                and not settings.get("band_preference_uuids")
                and self._read_profile_field(last, "802-11-wireless.band", timeout=3)
                == settings.get("band_preference", "a")):
            settings["band_preference_uuids"] = [last]
        if settings.get("dns_enabled") and last and not settings.get("dns_uuids"):
            settings["dns_uuids"] = [last]

    async def _unload(self):
        try:
            decky.logger.info("WiFi Optimizer unloading")
            for name in ("_backend_switch_task", "_stranded_task", "_stream_task"):
                task = getattr(self, name, None)
                if task and not task.done():
                    task.cancel()
        except Exception as e:
            decky.logger.error(f"_unload error: {e}")

    # Decky kills a plugin about five seconds after asking it to stop, and
    # _unload and _uninstall both run inside that.
    _UNINSTALL_BUDGET = 4.0

    async def _uninstall(self):
        """Undo everything, in a worker thread and inside Decky's budget.

        On the event loop, a slow nmcli held up the rest of Decky's shutdown
        of the plugin as well. Whatever comes after the kill does not
        happen, so what lasts longest goes first: the backend setting (no
        WiFi at the next boot if it is left wrong), then the files that hold
        power save off past a reboot, then the pins, which nothing else
        would take off. The dispatcher goes after the files it would remove
        itself on the next connect, once it finds the plugin gone. What a
        reboot undoes anyway comes last.
        """
        try:
            decky.logger.info("WiFi Optimizer uninstalling")
            deadline = time.monotonic() + self._UNINSTALL_BUDGET
            # Work already under way (a Reset taking pins off, a
            # set_power_save waiting on NetworkManager, a stream pass) would
            # otherwise go on after the teardown and put back the
            # dispatcher, the settings file or the files that hold power
            # save off. From here they write none of those, and the teardown
            # waits a little for them to finish, then goes on regardless.
            self._tearing_down = True
            held = []
            try:
                until = time.monotonic() + self._TEARDOWN_GUARD_WAIT
                # The report guard last: a send under way finishes (or the
                # wait ends) before the teardown removes the report folder.
                for guard in (self._stream_guard(), self._power_save_guard(),
                              self._queue_tuning_guard(), self._report_guard()):
                    if not await self._acquire_until(guard, until):
                        decky.logger.info(
                            "_uninstall went on without waiting longer for a change under way")
                        break
                    held.append(guard)
                await asyncio.to_thread(self._teardown, deadline)
            finally:
                for guard in reversed(held):
                    guard.release()
        except Exception as e:
            decky.logger.error(f"_uninstall error: {e}")

    # How long uninstall waits for work already under way, out of its budget.
    _TEARDOWN_GUARD_WAIT = 1.0

    @staticmethod
    async def _acquire_until(lock: asyncio.Lock, until: float) -> bool:
        """Take lock, waiting no later than until (monotonic). Whether it was."""
        if not lock.locked():
            await lock.acquire()
            return True
        left = until - time.monotonic()
        if left <= 0:
            return False
        try:
            await asyncio.wait_for(lock.acquire(), left)
            return True
        except asyncio.TimeoutError:
            return False

    def _teardown(self, deadline: float):
        """_uninstall's work, each step skipped once deadline has passed.

        Checking the deadline between steps only stopped new ones: a step
        already running went on past Decky's kill with commands given ten
        or fifteen seconds each. So every command run here is held to what
        is left of the budget, and one with too little left is not started.
        """
        self._cmd_deadline = deadline
        try:
            settings = _load_settings()

            def step(what: str, undo, *args):
                if time.monotonic() >= deadline:
                    decky.logger.error(f"_uninstall ran out of time before {what}")
                    return None
                return file_step(what, undo, *args)

            def file_step(what: str, undo, *args):
                # Work on files only, run whatever the deadline: quick, and a
                # slow command before it used the budget up and left our
                # files (and a waiting report) behind.
                try:
                    return undo(*args)
                except Exception as e:
                    decky.logger.error(f"_uninstall could not undo {what}: {e}")
                    return None

            # A backend switch cut off by the uninstall leaves its marker, and a
            # reinstall in the same boot would then reconcile a switch against a
            # state this teardown has already changed, restarting NetworkManager
            # as it loads. Finishing the switch here could take half a minute.
            if os.path.lexists(BACKEND_SWITCH_MARKER):
                step("the backend switch marker", self._set_backend_marker, None)
                decky.logger.info("_uninstall cut a WiFi backend switch off part way; "
                                  "a reinstall leaves the backend as it finds it")
            step("the backend setting", self._release_generic_backend_conf)
            step("a running update", self._stop_update)
            reloaded = file_step("the power save files", self._release_power_save_files,
                                 settings, False)
            # A second left for the steps after it, which are quick.
            left = step("the profile pins", self._clear_profile_pins, deadline - 1.0, settings)
            if left is None:
                # Skipped or failed: nothing is known to have come off, so
                # every tracked profile stays on record (as Reset keeps them)
                # rather than the settings file, the only record of them,
                # going below.
                left = {key: list(settings.get(key, [])) for key in (
                    "bssid_lock_uuids", "band_preference_uuids", "ipv6_uuids", "dns_uuids")}
                current = settings.get("bssid_lock_connection_uuid", "")
                if current and current not in left["bssid_lock_uuids"]:
                    left["bssid_lock_uuids"].append(current)
                left["wpa2_uuids"] = []
                left["wpa2_restore_uuids"] = list(dict.fromkeys(
                    list(settings.get("wpa2_uuids", []))
                    + list(settings.get("wpa2_restore_uuids", []))))
            # Contract C8: the apply lock, held from before the dispatcher
            # goes until buffer tuning and CAKE are off. A dispatcher run
            # already past its own check that the plugin is there would
            # otherwise put the fixes back after they came off; holding the
            # lock, it finds its file gone once it gets the lock, and stops.
            apply_lock = _take_apply_lock(
                max(0.0, min(1.0, deadline - time.monotonic() - 0.5)))
            try:
                file_step("the dispatcher", self._remove_dispatcher)
                step("power save", self._release_power_save, settings, bool(reloaded))
                step("the region", self._release_regdomain, settings)
                step("buffer tuning and CAKE", self._revert_queue_tuning, settings)
            finally:
                _drop_apply_lock(apply_lock)
            file_step("a report waiting to be sent", _forget_reports)
            # The diagnostics file and a settings file set aside as unreadable
            # are ours too, and nothing else would ever remove them.
            users = [SETTINGS_FILE, SETTINGS_FILE + ".corrupt", _diagnostics_path(),
                     LEGACY_ENFORCED_FILE]
            if any(left.values()):
                # Pins that are still on profiles. A settings file naming only
                # them lets a reinstall take them off; without it they are set
                # by hand as far as anything can tell, for good.
                try:
                    _save_settings({**left, "pins_owned": _owned_kinds_left(
                        settings.get("pins_owned", []), left)})
                    users.remove(SETTINGS_FILE)
                    decky.logger.error(
                        "_uninstall could not take every pin off; kept the settings "
                        "file naming those profiles so a reinstall can"
                    )
                except OSError as e:
                    decky.logger.error(f"_uninstall could not keep the unreleased pins: {e}")
            for path in [*users, ENFORCED_FILE, STREAM_FLAG_FILE, STREAM_HOLD_FILE,
                         PCIE_BASELINE_FILE, UPDATE_STATE_FILE, UPDATE_LOCK_FILE]:
                # One that cannot go (the settings directory is the user's,
                # so anything can sit at these names) does not stop the rest.
                try:
                    (_remove_no_follow if path in users else os.remove)(path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    decky.logger.error(f"_uninstall could not remove {path}: {e}")
            # Decky removes only the plugin's own folder. The logs of earlier
            # loads go (the one being written stays until Decky is done with
            # it), and the settings folder once nothing in it is kept.
            self._rotate_logs(keep=0)
            if SETTINGS_FILE in users:
                self._remove_settings_dir()
        finally:
            self._cmd_deadline = None

    def _remove_settings_dir(self):
        """Remove the plugin's settings folder if it is empty. The folder is
        in the user's directory, so it is removed relative to its parent as
        opened without following links, and only while empty."""
        folder = os.path.dirname(SETTINGS_FILE)
        try:
            pfd = _open_dir_no_follow(os.path.dirname(folder))
        except OSError:
            return
        try:
            os.rmdir(os.path.basename(folder), dir_fd=pfd)
        except FileNotFoundError:
            pass
        except OSError as e:
            decky.logger.info(f"_uninstall left the settings folder: {e}")
        finally:
            os.close(pfd)

    def _stop_update(self):
        """Stop an update still running when the plugin is uninstalled.

        Left alone it would put the plugin's files back after Decky removed
        them, or restart Decky for a plugin that is gone. The script tidies
        up after itself on SIGTERM.
        """
        if not self._update_running():
            return
        proc = getattr(self, "_update_proc", None)
        if proc is not None and proc.poll() is None:
            try:
                # 15 is SIGTERM, named by number: the signal module is not
                # one Decky's build is relied on to carry.
                os.killpg(proc.pid, 15)
            except OSError:
                pass
        if os.path.exists(SYSTEMD_RUN):
            self._run_cmd(["/usr/bin/systemctl", "stop", "--no-block",
                           "wifi-optimizer-update-*.service"], timeout=2)

    async def _migration(self):
        pass

    # ---- Hardware detection ----

    def _detect_device_family(self) -> tuple[str, str, str]:
        """Read DMI product_name (and sys_vendor) and return
        (raw_product, family_id, display_label)."""
        try:
            with open(os.path.join(DMI_DIR, "product_name"), "r") as f:
                product = f.read().strip()
        except Exception:
            return ("unknown", "unknown", "Unknown Device")

        if product in DMI_DEVICES:
            info = DMI_DEVICES[product]
            return (product, info["family"], info["label"])

        for prefix, info in DMI_SUBSTRING_DEVICES:
            if product.startswith(prefix):
                return (product, info["family"], info["label"])

        try:
            with open(os.path.join(DMI_DIR, "sys_vendor"), "r") as f:
                vendor = f.read(256).strip()
        except Exception:
            vendor = ""
        for prefix, info in DMI_VENDOR_DEVICES:
            if vendor.upper().startswith(prefix):
                return (product, info["family"], info["label"])

        return (product, "unknown", "Unknown Device")

    def _detect_wifi_driver(self) -> str:
        """Detect the kernel driver of the active WiFi interface via sysfs.
        Normalizes sub-module names (e.g. rtw88_pci) to the canonical
        DRIVER_PROFILES key (rtw88).

        The card on PCI first: a USB adapter NetworkManager happens to list
        first (an mt7921u, an rtl8xxxu) would otherwise replace the driver
        saved at load and turn off the built-in card's fixes."""
        iface = self._pci_wifi_interface() or self._get_wifi_interface()
        if not iface or not IFACE_RE.match(iface):
            return "unknown"
        try:
            driver_path = os.path.realpath(os.path.join(SYSFS_NET, iface, "device", "driver", "module"))
            return self._module_profile(os.path.basename(driver_path))
        except Exception:
            return "unknown"

    # A 6 GHz channel: 5955 MHz (channel 1) up to 7115 MHz (channel 233).
    _SIX_GHZ_FREQ_RE = re.compile(r"^\s*\* (59[5-9]\d|6\d{3}|70\d\d|71[01]\d)(?:\.\d+)? MHz", re.M)

    def _radio_has_6ghz(self) -> bool | None:
        """Whether the WiFi card lists any 6 GHz channel (even one the region
        disables), from `iw phy`. None when it cannot be asked. The card on
        PCI first, as for _detect_wifi_driver."""
        iface = self._pci_wifi_interface() or self._get_wifi_interface()
        if not iface or not IFACE_RE.match(iface):
            return None
        try:
            with open(os.path.join(SYSFS_NET, iface, "phy80211", "name"), "r") as f:
                phy = f.read(32).strip()
        except OSError:
            return None
        if not IFACE_RE.match(phy):
            return None
        result = self._run_cmd(["/usr/bin/iw", "phy", phy, "info"], timeout=3)
        if not result.get("success") or "Frequencies" not in result.get("stdout", ""):
            return None
        return bool(self._SIX_GHZ_FREQ_RE.search(result["stdout"]))

    def _detect_distro(self) -> dict:
        """Detect OS from /etc/os-release. Returns {id, name}."""
        info = {"id": "unknown", "name": "Unknown"}
        try:
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if line.startswith("ID="):
                        info["id"] = line.split("=", 1)[1].strip().strip('"')
                    elif line.startswith("PRETTY_NAME="):
                        info["name"] = line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
        # Valve's newer images can identify as holo, the name of the layer
        # SteamOS is built from. Everything this plugin does differently on
        # SteamOS applies there unchanged.
        if info["id"] == "holo":
            info["id"] = "steamos"
        return info

    async def get_device_info(self) -> dict:
        try:
            # Off the event loop: the interface lookup can ask nmcli and the
            # 6 GHz question runs `iw phy`, which waits out its timeout behind
            # a wedged driver, and every call the panel makes would wait too.
            def detect():
                return (self._detect_device_family(), self._detect_wifi_driver(),
                        self._radio_has_6ghz())

            (product, device_family, device_label), driver, supports_6ghz = \
                await asyncio.to_thread(detect)
            profile = DRIVER_PROFILES.get(driver, {})

            chip_label = profile.get("chip_label", "unknown")
            # The card itself says, where it can be asked: one driver serves
            # chips with and without 6 GHz (mt7921e drives both the MT7921
            # and the 6E MT7922; iwlwifi every Intel card since 2013).
            # When it cannot (iw stuck or timing out, no iw at all), the
            # answer the card gave at an earlier load for the same driver
            # stands, and the profile's guess only when there is none: the
            # load saves this, and a guess would overwrite a measurement.
            if supports_6ghz is None:
                saved = _load_settings()
                if saved.get("driver") == driver and isinstance(saved.get("supports_6ghz"), bool):
                    supports_6ghz = saved["supports_6ghz"]
                else:
                    supports_6ghz = profile.get("supports_6ghz", False)

            model = "unknown"
            if device_family == "deck_lcd":
                model = "lcd"
            elif device_family == "deck_oled":
                model = "oled"

            return {
                "success": True,
                "model": model,
                "driver": driver,
                "device_family": device_family,
                "device_label": device_label,
                "chip_label": chip_label,
                "supports_6ghz": supports_6ghz,
            }
        except Exception as e:
            decky.logger.error(f"get_device_info error: {e}")
            return {
                "success": True,
                "model": "unknown",
                "driver": "unknown",
                "device_family": "unknown",
                "device_label": "Unknown Device",
                "chip_label": "unknown",
                "supports_6ghz": False,
            }

    def _get_support_tier(self) -> int:
        """Return 1 (full), 2 (partial), or 3 (generic) based on detection.
        Tier 1: recognized device + recognized driver.
        Tier 2: unknown device + recognized driver.
        Tier 3: unknown device + unknown driver."""
        settings = _load_settings()
        driver = settings.get("driver", "unknown")
        device_family = settings.get("device_family", "unknown")
        if driver in DRIVER_PROFILES and device_family != "unknown":
            return 1
        if driver in DRIVER_PROFILES:
            return 2
        return 3

    def _unexpected_response(self, e: Exception) -> dict:
        """Standard error dict for the catch-all exception handler in every
        setter. Callers log the error separately with the setter name.

        The exception text is NOT returned. The panel shows `message` as it
        is, so this is the one thing the user reads, and a traceback under
        the control they just touched helps nobody. Callers log the
        exception, so nothing is lost.

        A save that failed is said as such. It mostly comes after the change
        itself was made, so "something went wrong" left the user believing
        nothing had happened while the panel, reading the old settings, put
        the toggle back where it was.
        """
        if isinstance(e, SettingsSaveError):
            return {
                "success": False,
                "error": "settings_unwritable",
                "message": (
                    "Couldn't save the plugin's settings, so the panel may not "
                    "match what is applied. Try again. If it keeps happening, restart "
                    "the device."
                ),
            }
        return {
            "success": False,
            "error": "unexpected",
            "message": ("Something went wrong. Try again. If it keeps happening, use "
                        "Copy diagnostics at the bottom of this panel and report it."),
        }

    @staticmethod
    def _log_detail(where: str, detail) -> None:
        """Log the command output behind a failure the panel reports in
        plain words. The panel no longer shows it, so this is where a report
        finds it."""
        if detail:
            decky.logger.error(f"{where} failed: {_tool_text(str(detail)[:300])}")

    def _nmcli_modify(self, uuid: str, key: str, value: str, timeout: int = 5) -> dict:
        """Run `nmcli con mod uuid <uuid> <key> <value>`. Returns the
        _run_cmd dict so callers can handle success/failure themselves."""
        return self._run_cmd(
            ["/usr/bin/nmcli", "con", "mod", "uuid", uuid, key, value],
            timeout=timeout,
        )

    def _resolve_uuid(self, active_required_msg: str | None = None) -> tuple:
        """Resolve a WiFi connection UUID for a setter. Returns (uuid, None)
        on success or (None, error_dict) on failure.

        If active_required_msg is provided and there's no active WiFi
        connection, fails with that specific message (e.g., "Connect to WiFi
        first to disable IPv6"). Otherwise falls back to the most recently
        saved connection UUID so setters can still modify a saved profile
        while disconnected.
        """
        _iface, uuid, _err = self._require_wifi()
        if active_required_msg and not uuid:
            return None, {
                "success": False,
                "error": "no_wifi",
                "message": active_required_msg,
            }
        if not uuid:
            uuid = self._get_saved_connection_uuid()
        if not uuid:
            return None, {
                "success": False,
                "error": "nmcli_failed",
                "message": "Connect to WiFi first.",
            }
        return uuid, None

    # ---- Diagnostics ----

    async def get_regdomain(self) -> dict:
        """The wireless region, with enough context for the panel to be honest."""
        try:
            info = await asyncio.to_thread(self._regdomain_info)
            info["success"] = True
            info["setting"] = _load_settings().get("regdomain", "")
            return info
        except Exception as e:
            decky.logger.error(f"get_regdomain error: {e}")
            return {"success": False, "error": "unexpected", "readable": False,
                    "changeable": False, "self_managed": False,
                    "global": "", "governing": "", "phys": [], "setting": ""}

    async def set_regdomain(self, country: str) -> dict:
        """Set the wireless region, or pass "" to stop managing it.

        Some radios carry their own regulatory domain and ignore this
        entirely. Rather than appearing to succeed and changing nothing, the
        result says so: a silent no-op that imitates a working setting is the
        worst shape this could take.
        """
        try:
            country = (country or "").strip().upper()
            if country and not REGDOMAIN_RE.match(country):
                return {
                    "success": False, "error": "invalid_region",
                    "message": "A region is two letters, like US or DE.",
                }
            info = await asyncio.to_thread(self._regdomain_info)
            if not info.get("readable"):
                return {
                    "success": False, "error": "unreadable",
                    "message": ("Couldn't read the current wireless region. Try again "
                                "in a moment. If it keeps failing, restart the device."),
                }
            settings = _load_settings()
            previous = settings.get("regdomain_previous", "")
            if country:
                # Record what was there before OUR first change, so turning
                # this off restores it instead of guessing at a default.
                if not settings.get("regdomain"):
                    previous = info.get("global", "")
                target = country
            else:
                target = previous or "00"
            if not REGDOMAIN_RE.match(target):
                return {
                    "success": False, "error": "invalid_region",
                    "message": "A region is two letters, like US or DE.",
                }
            applied = await asyncio.to_thread(
                self._run_cmd, ["/usr/bin/iw", "reg", "set", target], 5
            )
            if not applied["success"]:
                self._log_detail("set_regdomain", (applied.get("stderr") or "")[:200])
                return {
                    "success": False, "error": "iw_failed",
                    "message": ("Couldn't set the wireless region. Try again in a "
                                "moment. If it keeps failing, restart the device."),
                    "detail": (applied.get("stderr") or "")[:200],
                }
            # Re-read: anything saved while iw ran would otherwise be undone
            # by writing back the copy taken before it.
            settings = _load_settings()
            settings["regdomain_previous"] = previous
            settings["regdomain"] = country
            _save_settings(settings)
            after = await asyncio.to_thread(self._regdomain_info)
            # The kernel applies a region request from a queue of its own, so
            # a read straight after can still show the old one. Give it a
            # moment before saying the card did not follow.
            for _ in range(2):
                if not country or after.get("self_managed") or after.get("governing") == country:
                    break
                await asyncio.sleep(0.5)
                after = await asyncio.to_thread(self._regdomain_info)
            result = {
                "success": True,
                "regdomain": country,
                "governing": after.get("governing", ""),
                "self_managed": after.get("self_managed", False),
                "changeable": after.get("changeable", False),
            }
            # The Steam Deck is named only on one: the panel shows this on
            # every device the plugin runs on.
            on_deck = settings.get("device_family") in ("deck_lcd", "deck_oled")
            normal = ("That is normal on many handhelds, the Steam Deck included, "
                      if on_deck else "That is normal on many handhelds, ")
            governing = after.get("governing", "")
            if after.get("self_managed"):
                result["message"] = (
                    f"This device's WiFi carries {self._region_words(governing, country)} "
                    "and ignores this "
                    "setting, so the channels available to you have not "
                    "changed. " + normal + "and does not mean a band is missing."
                )
            elif country and after.get("readable") and after.get("governing") != country:
                # Not self-managed, and still not on the chosen region: a card
                # that follows the region it was built for (some read one from
                # the chip itself) can turn the request down, and the
                # selector would otherwise show a region the radio is not on.
                where = (f"is on {self._region_words(governing, country)}, not {country} alone"
                         if governing == "98" else
                         f"stayed on {self._region_words(governing, country)} instead of {country}")
                result["message"] = (
                    f"The WiFi card {where}, so the channels available to you may not "
                    "have changed. " + normal + "and nothing else needs doing."
                )
            decky.logger.info(
                f"regdomain set to {target!r}: governing="
                f"{after.get('governing')!r} self_managed={after.get('self_managed')}"
            )
            return result
        except Exception as e:
            decky.logger.error(f"set_regdomain error: {e}")
            return self._unexpected_response(e)

    # What identifies a network or a device in the diagnostics. People paste
    # these into public issues, and an SSID next to an access point's address
    # is enough to place someone on a map through the public WiFi databases.
    _DIAG_SSID_LINE_RE = re.compile(r"^([ \t]*ssid ).*$", re.MULTILINE)
    # The edges only refuse a longer run of octets, so an address followed by
    # ":" and a word ("AA:..:FF:bluetooth" in nmcli's output) is still caught.
    _DIAG_MAC_RE = re.compile(
        r"(?<![0-9A-Fa-f-])(?<![0-9A-Fa-f]:)(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"
        r"(?![0-9A-Fa-f-])(?![:-][0-9A-Fa-f]{2}(?![0-9A-Za-z]))"
    )
    # The same patterns the plugin log is masked with (see _IPV4_RE).
    _DIAG_IPV4_RE = _IPV4_RE
    _DIAG_IPV6_RE = _IPV6_RE

    @classmethod
    def _redact_diagnostics(cls, value, ssids: tuple = ()):
        """The diagnostics with network names and addresses replaced.

        Placeholders rather than removal, so the shape of the output still
        says what was there: an interface with an SSID is associated, and
        one without is not. Applied to every string, because anything added
        to the diagnostics later should be covered without someone having to
        remember this. Keys get the address and home folder masking but not
        the network names: they are the plugin's own words, and a network
        called "info" turned device_info into device_<ssid>, which the
        receiving end then refused.
        """
        if isinstance(value, dict):
            return {(cls._redact_diagnostics(k) if isinstance(k, str) else k):
                    cls._redact_diagnostics(v, ssids)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._redact_diagnostics(v, ssids) for v in value]
        if not isinstance(value, str):
            return value
        text = cls._DIAG_SSID_LINE_RE.sub(r"\1<ssid>", value)
        # The name can also turn up away from the line that labels it. Very
        # short ones are left, since replacing every "ab" would wreck the rest.
        for ssid in ssids:
            if len(ssid) >= 4:
                text = text.replace(ssid, "<ssid>")
        text = cls._DIAG_MAC_RE.sub("<mac>", text)
        text = _IFNAME_MAC_RE.sub(r"\1x<mac>", text)
        text = _mask_device_names(text)
        # IPv4 first: an IPv4-mapped address (::ffff:192.168.1.5) would
        # otherwise lose only its first octet to the IPv6 pattern.
        text = cls._DIAG_IPV4_RE.sub("<ip>", text)
        text = cls._DIAG_IPV6_RE.sub(
            lambda m: m.group(0) if m.group(0) == "::" else "<ip>", text
        )
        # Error text carries paths, and a home directory names the account.
        return _mask_home(text)

    async def get_diagnostic_info(self) -> dict:
        """Collect system info for remote debugging. Sanitized: no passwords,
        and no network names or addresses (see _redact_diagnostics)."""
        try:
            info = await self.get_device_info()

            def basics():
                # One trip off the event loop for all four: iw waits up to
                # its timeout behind a wedged driver, which is exactly when
                # someone copies a report, and every other call the panel
                # makes would wait with it.
                return (
                    self._get_wifi_interface() or "none",
                    self._run_cmd(["/usr/bin/iw", "dev"], timeout=3),
                    self._run_cmd(["/usr/bin/iw", "reg", "get"], timeout=3),
                    self._run_cmd(["/usr/bin/uname", "-r"], timeout=3),
                )

            iface, iw_dev, iw_reg, uname = await asyncio.to_thread(basics)
            os_release = ""
            try:
                with open("/etc/os-release", "r") as f:
                    os_release = f.read()
            except Exception:
                pass
            distro = self._detect_distro()
            ssids = tuple(
                found.group(0).split("ssid ", 1)[1]
                for found in self._DIAG_SSID_LINE_RE.finditer(iw_dev.get("stdout", ""))
            )
            # The extras redact the service logs with these too, before their
            # own quote rules run (see _diag_service_lines).
            self._diag_ssids = ssids
            # The report predates these parts, so one that fails is noted
            # rather than costing the person the whole report.
            try:
                extras = await asyncio.to_thread(
                    self._diagnostic_extras, info.get("driver", ""), iface
                )
            except Exception as e:
                decky.logger.error(f"diagnostic extras error: {e}")
                extras = {"extras_error": str(e)}
            collected = self._redact_diagnostics({
                "success": True,
                **extras,
                "device_info": info,
                "wifi_interface": iface,
                "iw_dev": iw_dev.get("stdout", ""),
                "iw_reg": iw_reg.get("stdout", ""),
                # The raw block above is kept, but it leads with the GLOBAL
                # domain, which a self-managed radio ignores. Anyone reading
                # it sees `country 00: DFS-UNSET` and concludes their 6 GHz is
                # crippled. Ship the interpretation alongside it.
                "regdomain": self._parse_reg(iw_reg.get("stdout", "")),
                "kernel": uname.get("stdout", "").strip(),
                "os_release": os_release,
                "distro_id": distro["id"],
                "distro_name": distro["name"],
                "support_tier": self._get_support_tier(),
            }, ssids)
            # Kept for Save diagnostics, the fallback for a clipboard that
            # failed a moment ago: it writes this rather than collecting again.
            self._last_diagnostics = (time.monotonic(), copy.deepcopy(collected))
            return collected
        except Exception as e:
            decky.logger.error(f"get_diagnostic_info error: {e}")
            return {"success": False, "error": self._redact_diagnostics(str(e))}

    # The settings worth seeing in a report. Picked rather than dumped: the
    # rest is profile ids and the band preference's network name.
    _DIAG_SETTINGS = (
        "power_save_disabled", "auto_fix_on_wake", "bssid_lock_enabled",
        "band_preference_enabled", "band_preference", "dns_enabled",
        "dns_provider", "ipv6_disabled", "buffer_tuning_enabled",
        "cake_enabled", "streaming_mode_enabled", "update_channel",
        "settings_version", "regdomain",
    )
    # Kernel lines about the WiFi driver, its PCIe link and suspend/resume.
    # The interface and driver actually present are added at call time, so a
    # wlp1s0 or a driver missing from this list is covered too. A WARN from
    # the WiFi stack names no driver in its headline ("Hardware became
    # unavailable upon resume", rtw88's "failed to ack driver for leaving
    # Deep Power mode"), and the OLED's card talks over mhi.
    _DIAG_KERNEL_TERMS = (
        r"rtw88|rtw89|rtw_|ath1[012]k|mt7\d|iwlwifi|brcmfmac|wlan\d|"
        r"ieee80211 phy\d|cfg80211|PM: |ACPI: PM|pcieport|AER|ASPM|FADT|"
        r"mac80211|drivers/net/wireless/|Hardware became unavailable|Deep Power mode|"
        r"DBI register|mhi[0-9]*"
    )
    # Connection chatter: on a healthy boot it is most of what matches, and it
    # pushed the suspend lines out of the report on Jason's own Deck.
    _DIAG_CHATTER_RE = re.compile(
        r": (?:authenticate with|send auth to|authenticated|associate with|"
        r"RX AssocResp|associated|deauthenticating from|"
        r"Limiting TX power|aborting authentication|authentication with)"
    )
    _DIAG_KERNEL_LINES = 50
    # A kernel WARN prints a call trace and every loaded module on one line.
    # Its first frames say where it fired; the rest would crowd out the wake.
    _DIAG_TRACE_FRAMES = 12
    _DIAG_LINE_MAX = 300
    # How much of a boot's kernel log is read. Bounds memory and time when a
    # driver has been logging errors for days.
    _DIAG_JOURNAL_MAX = "5000"
    # With --no-hostname a kernel record reads "[  12.345678] kernel: msg".
    # Anything else (systemd's "Hostname set to ..." also lands in -k) is left out.
    _DIAG_KERNEL_LINE_RE = re.compile(r"^(\[\s*[\d.]+\]) kernel: (.*)$")

    # A call trace frame: " func+0x1c/0x40 [module]", " ? func+...", or a
    # context marker such as <TASK>, <IRQ> and their closing forms. The kernel
    # indents frames by a space; the symbol form is also accepted in case a
    # journal ever stores the line without it.
    _DIAG_FRAME_RE = re.compile(
        r"^(?:[ ?<]|[\w.$]+\+0x[0-9a-fA-F]+/0x[0-9a-fA-F]+)"
    )

    @classmethod
    def _diag_is_frame(cls, message: str) -> bool:
        return bool(message) and bool(cls._DIAG_FRAME_RE.match(message))

    @classmethod
    def _diag_kernel_lines(cls, text: str, terms: str, cap: int | None = None,
                           anchor: float | None = None) -> list:
        """The lines of one boot's kernel log worth reading for a WiFi problem.

        Repeats are folded into a count on the line they repeat, since a
        wedged driver logs the same error thousands of times, and connection
        chatter is dropped. A WARN's call trace keeps only its first frames,
        and the line before a kept WARNING (its headline, which names no
        driver) is kept with it. If what is left is still too long, it keeps
        the stretch from the sleep of the wake judged onwards (anchor, its
        monotonic stamp; without one the last suspend) plus the very end of
        the boot, or simply the end when that sleep is within it.
        """
        wanted = re.compile(terms)
        lines, last_key, repeats, frames = [], None, 0, None
        before = None   # the last line read and not kept: a WARN's headline

        def fold():
            if repeats:
                lines[-1] += f" (repeated {repeats} more times)"

        for raw in text.splitlines():
            found = cls._DIAG_KERNEL_LINE_RE.match(raw)
            if not found:
                continue
            stamp, message = found.groups()
            if message.startswith("Call Trace:"):
                frames = 0
            elif frames is not None:
                # Only a WARN or an oops ends its trace with "end trace". A
                # hung task, a soft lockup, an RCU stall or dump_stack() (what
                # a wedged rtw88 tends to print) stops at </TASK> or with no
                # marker at all, so the first line that is not a frame ends
                # it too, and is read like any other line. Waiting for the
                # marker used to swallow the rest of the boot: the suspend
                # and every driver error after the trace.
                if "---[ end trace" in message or message.strip() == "</TASK>":
                    frames = None
                elif cls._diag_is_frame(message):
                    frames += 1
                    if frames > cls._DIAG_TRACE_FRAMES:
                        before = None
                        continue
                else:
                    frames = None
            if message.startswith("Modules linked in:"):
                before = None
                continue
            if not wanted.search(message) or cls._DIAG_CHATTER_RE.search(message):
                before = (stamp, message)
                continue
            headline, before = before, None
            if (message.startswith("WARNING:") and headline is not None
                    and "[ cut here ]" not in headline[1]):
                fold()
                lines.append(f"{headline[0]} {headline[1]}"[:cls._DIAG_LINE_MAX])
                last_key, repeats = None, 0
            key = re.sub(r"\d+", "#", message)
            if key == last_key:
                repeats += 1
                continue
            fold()
            lines.append(f"{stamp} {message}"[:cls._DIAG_LINE_MAX])
            last_key, repeats = key, 0
        fold()
        cap = cap or cls._DIAG_KERNEL_LINES
        if len(lines) <= cap:
            return lines
        starts = [i for i, line in enumerate(lines) if "PM: suspend entry" in line]
        if anchor is not None:
            def stamp_of(line):
                found = cls._DIAG_STAMP_RE.match(line)
                return float(found.group(1)) if found else -1.0
            starts = [next((i for i, line in enumerate(lines) if stamp_of(line) >= anchor),
                           len(lines))]
        if not starts or len(lines) - starts[-1] <= cap:
            return [f"({len(lines) - cap} earlier lines left out)"] + lines[-cap:]
        start = starts[-1]
        head = lines[start:start + cap - 10]
        tail_from = len(lines) - 10
        out = [f"({start} earlier lines left out)"] if start else []
        out += head
        out.append(f"({tail_from - start - len(head)} lines left out)")
        return out + lines[tail_from:]

    # NetworkManager, iwd and wpa_supplicant name the network in quotes (see
    # _mask_quoted, which the plugin log goes through too) and after
    # "ssid"/"SSID".
    # A value after "ssid" is a network name whether it is quoted or not.
    # iwd logs "event: connect-info, ssid: Marriott Bonvoy Guest, bss: ...,
    # signal: ..., load: ..." unquoted, so the value runs to the field that
    # follows it or the end of the line; the one-word form is left only for
    # a value with spaces in the middle of a line that names no field after.
    _DIAG_SSID_WORD_RE = re.compile(
        r"(?i)(\bssid[=:] ?)(?!<ssid>|<name>)"
        r"(?:\"[^\"]*\"|'[^']*'|.*?(?=, (?:bss|signal|freq|load|security):| freq=|$))"
    )
    _DIAG_SSID_WORD_LAST_RE = re.compile(r"(?i)(\bssid[ =:]+)(?!<ssid>|<name>)\S+")
    # A connection profile's file is named after the network: NetworkManager's
    # keyfiles and iwd's network files.
    _DIAG_PROFILE_FILE_RE = re.compile(
        r"(system-connections/)[^/\n]+?(\.nmconnection)\b|(/var/lib/iwd/)[^/\n]+?(\.(?:psk|open|8021x))\b"
    )
    _DIAG_SERVICE_LINES = 40
    # Where a boot that ended in a reboot starts shutting the services down,
    # and where one of them starts again (a backend switch restarts them
    # mid-boot, which is not the end of it).
    _DIAG_SHUTDOWN_RE = re.compile(r"caught SIGTERM|shutting down|: Stopping ")
    _DIAG_SERVICE_START_RE = re.compile(
        r"NetworkManager \(version|: Starting |: Started |Successfully initialized wpa_supplicant"
    )
    # The lines that say what happened to a connection, kept ahead of routine
    # ones when there are too many.
    _DIAG_SERVICE_EVENT_RE = re.compile(
        r"(?i)state change|fail|error|<warn>|CTRL-EVENT|disconnect|deauth|timed? ?out|reason"
    )

    @classmethod
    def _diag_service_lines(cls, text: str, limit: int, names: tuple = (),
                            events_first: bool = False, first: frozenset = frozenset()) -> list:
        """The last lines of a service log, each bounded, network names out.

        names are the network names known on this device (saved profiles and
        the SSIDs in view), replaced wherever they appear before anything
        else is, so a name with a quote in it goes whole. events_first: for
        the network services' log, see _diag_pick_service_lines; first are
        raw lines it keeps ahead of the rest (the stretch around the wake).
        """
        known = sorted({n for n in names if isinstance(n, str) and len(n) >= 3},
                       key=len, reverse=True)
        named = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])")
                 for n in known]
        # A VPN or virtual device's name, from where NetworkManager names
        # it, goes wherever else it turns up too ("policy: set ... (NAME)").
        devices = sorted({f.group(2) for f in _NM_DEVICE_NAME_RE.finditer(text)
                          if not _KEPT_DEVICE_RE.fullmatch(f.group(2)) and len(f.group(2)) >= 3},
                         key=len, reverse=True)
        named_devices = [re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(n) + r"(?![A-Za-z0-9_.-])")
                         for n in devices]
        lines, picked = [], []
        for raw in text.splitlines():
            if raw in first:
                picked.append(len(lines))
            line = cls._DIAG_PROFILE_FILE_RE.sub(
                lambda f: (f"{f.group(1)}<name>{f.group(2)}" if f.group(1)
                           else f"{f.group(3)}<name>{f.group(4)}"), raw)
            for pattern in named:
                line = pattern.sub("<name>", line)
            line = _mask_device_names(line)
            for pattern in named_devices:
                line = pattern.sub("<device>", line)
            line = _mask_quoted(line)
            line = cls._DIAG_SSID_WORD_RE.sub(r"\1<ssid>", line)
            line = cls._DIAG_SSID_WORD_LAST_RE.sub(r"\1<ssid>", line)
            lines.append(line[:cls._DIAG_LINE_MAX])
        if events_first:
            return cls._diag_pick_service_lines(lines, limit, picked)
        if len(lines) > limit:
            return [f"({len(lines) - limit} earlier lines left out)"] + lines[-limit:]
        return lines

    @classmethod
    def _diag_pick_service_lines(cls, lines: list, limit: int, first: list = ()) -> list:
        """The network services' lines worth reading, at most limit of them.

        A boot that ended in a reboot ends in the shutdown, and its last
        lines were all a report showed. So the shutdown is left out, and of
        what comes before it the lines about connections (state changes,
        failures, supplicant events) are kept ahead of routine ones. first
        (indexes into lines, the stretch around the wake judged) comes before
        both, its events first, leaving the last ten places for the rest.
        """
        out = []
        last_start = max((i for i, line in enumerate(lines)
                          if cls._DIAG_SERVICE_START_RE.search(line)), default=-1)
        stop = next((i for i, line in enumerate(lines)
                     if i > last_start and cls._DIAG_SHUTDOWN_RE.search(line)), None)
        if stop is not None:
            lines, dropped = lines[:stop], len(lines) - stop
        else:
            dropped = 0
        if len(lines) > limit:
            first = sorted(i for i in set(first) if i < len(lines))
            budget = max(0, limit - 10)
            ahead = ([i for i in first if cls._DIAG_SERVICE_EVENT_RE.search(lines[i])]
                     + [i for i in first if not cls._DIAG_SERVICE_EVENT_RE.search(lines[i])])
            keep = set(ahead[:budget])
            events = [i for i, line in enumerate(lines)
                      if cls._DIAG_SERVICE_EVENT_RE.search(line) and i not in keep]
            if limit > len(keep):
                keep.update(events[-(limit - len(keep)):])
            for i in range(len(lines) - 1, -1, -1):
                if len(keep) >= limit:
                    break
                keep.add(i)
            out.append(f"({len(lines) - len(keep)} earlier or routine lines left out)")
            lines = [lines[i] for i in sorted(keep)]
        out += lines
        if dropped:
            out.append(f"({dropped} lines of the shutdown left out)")
        return out

    # The dispatcher's marker for the driver parameters as they were when the
    # driver loaded (defaults/dispatcher.sh.tmpl).
    _DIAG_AT_LOAD = "driver parameters at load:"
    _DIAG_PARAMS_NOTE = (
        "Each value is the module parameter as it reads now. The driver acts on it "
        "at its own moment: rtw88's disable_aspm at each PCIe power change, except "
        "its CLKREQ part, set only when the driver loads; its "
        "disable_lps_deep when the interface next starts, rtw89's at probe and "
        "resume, mt7921e's only at probe. So a Y written after that is not yet in "
        "force; plugin_load_values_* say what the driver loaded with."
    )

    def _diagnostic_extras(self, driver: str, iface: str) -> dict:
        """What a report needs beyond the device: versions, toggles, backend,
        the driver's power state, and the kernel's account of the last wake.

        The previous boot's kernel lines are included because the usual way
        out of WiFi that never came back is a reboot, and the report is
        copied after it. Each part is read on its own, so one that fails
        does not take the rest of the report with it.
        """
        extras = {
            "plugin_version": getattr(decky, "DECKY_PLUGIN_VERSION", ""),
            "decky_version": getattr(decky, "DECKY_VERSION", ""),
        }
        try:
            settings = _load_settings()
            extras["settings"] = {k: settings.get(k) for k in self._DIAG_SETTINGS}
        except Exception as e:
            extras["settings"] = f"unreadable: {e}"
        try:
            # The one running now, and the one configured for the next boot
            # when that differs (see _backend_in_use).
            configured = self._get_current_backend()
            in_use = self._backend_in_use(configured)
            extras["wifi_backend"] = in_use or "unknown"
            if configured and in_use and configured != in_use:
                extras["wifi_backend_next"] = configured
        except Exception as e:
            extras["wifi_backend"] = f"unreadable: {e}"

        def read(path: str) -> str:
            try:
                with open(path, "r") as f:
                    return f.read().strip()
            except FileNotFoundError:
                return "missing"
            except OSError as e:
                return f"unreadable: {e.strerror}"

        # With the radio gone after a bad wake there may be no interface to
        # detect the driver from, so the one recorded at load stands in.
        if driver not in DRIVER_PROFILES:
            try:
                driver = _load_settings().get("driver", "") or driver
            except Exception:
                pass
        profile = DRIVER_PROFILES.get(driver, {})
        extras["driver_params"] = {
            path: read(path) for path in profile.get("sysfs_power_fixes", [])
        }
        if extras["driver_params"]:
            # What the file says is not always what the driver is doing:
            # each one reads its parameter at its own moment.
            extras["driver_params_note"] = self._DIAG_PARAMS_NOTE
        extras["modprobe_conf"] = read(MODPROBE_CONF_PATH)
        # Options in that file only take effect when the driver loads, so
        # whether it predates THIS boot says if they were in force from its
        # start. It says nothing about the boot before, the one a report is
        # usually about: for that, see plugin_load_values_previous_boot.
        try:
            mtime = os.stat(MODPROBE_CONF_PATH).st_mtime
            extras["modprobe_conf_modified"] = time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime(mtime))
            boot = _boot_time()
            extras["modprobe_conf_before_this_boot"] = (
                None if boot is None else mtime < boot
            )
        except OSError:
            extras["modprobe_conf_modified"] = None
            extras["modprobe_conf_before_this_boot"] = None
        extras["dispatcher_installed"] = os.path.isfile(DISPATCHER_PATH)

        extras["pcie"], link = self._diag_pcie(iface, read)
        if link:
            extras["pcie_link"] = link

        rfkill = []
        try:
            for entry in sorted(os.listdir(self.RFKILL_DIR)):
                base = os.path.join(self.RFKILL_DIR, entry)
                rfkill.append(f"{read(os.path.join(base, 'type'))} "
                              f"soft={read(os.path.join(base, 'soft'))} "
                              f"hard={read(os.path.join(base, 'hard'))}")
        except OSError:
            pass
        extras["rfkill"] = rfkill

        extras["nm_devices"], extras["nm_general_state"] = self._diag_nm_state()

        # Every network name this device knows, for the service logs below:
        # replaced whole, before the rules for quoted text, so a name with a
        # quote of its own cannot leave half of itself behind.
        names = list(getattr(self, "_diag_ssids", ()) or ())
        saved = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-e", "no", "-f", "NAME", "connection", "show"],
            timeout=3,
        )
        if saved.get("success"):
            names += [n for n in saved.get("stdout", "").split("\n") if n.strip()]
        try:
            names.append(_load_settings().get("band_preference_ssid", "") or "")
        except Exception:
            pass
        names = tuple(names)

        # The plugins directory sits in the desktop user's home, so it is
        # listed through the same no-follow walk as the settings directory.
        plugins_dir = os.path.dirname(getattr(decky, "DECKY_PLUGIN_DIR", "") or "")
        extras["decky_plugins"] = []
        if plugins_dir:
            try:
                dfd = _open_dir_no_follow(plugins_dir)
                try:
                    extras["decky_plugins"] = sorted(os.listdir(dfd))
                finally:
                    os.close(dfd)
            except OSError:
                pass

        # What the fixes were during the boot that failed. The dispatcher logs
        # the driver parameters before it writes them, and this boot's
        # baseline says what they were before anything of ours ran.
        extras["pcie_baseline"] = _read_pcie_baseline()
        extras["mem_sleep"] = read("/sys/power/mem_sleep")
        # Valve's BIOS updates for the Steam Deck have changed how the PCIe
        # link and sleep behave, so a wake report needs the firmware it ran.
        extras["bios_version"] = read(os.path.join(DMI_DIR, "bios_version"))[:64]
        extras["bios_date"] = read(os.path.join(DMI_DIR, "bios_date"))[:32]
        journal = ["/usr/bin/journalctl", "--no-hostname", "-o", "short-monotonic",
                   "--no-pager"]
        for key, boot in (("this_boot", "0"), ("previous_boot", "-1")):
            # Root's lines only: any process can log under this tag (a
            # Flatpak app included), and the plugin and the dispatcher both
            # log as root, so a forged "at load" line cannot stand in a report.
            log = self._run_cmd(journal + ["-b", boot, "-t", "wifi-optimizer", "_UID=0", "-n", "200"],
                                timeout=5)
            extras["plugin_log_" + key] = (
                self._diag_service_lines(log.get("stdout", ""), 30, names)
                if log.get("success")
                else [f"unavailable: {(log.get('stderr') or '')[:120]}"])
            # The dispatcher's first run of a boot notes the driver parameters
            # as the driver loaded them, which is whether the fixes were in
            # force for that whole boot. Later lines read Y because the
            # dispatcher wrote it, and after a few wakes the first line has
            # left the tail above, so it is looked for on its own.
            found = self._run_cmd(
                journal + ["-b", boot, "-t", "wifi-optimizer", "_UID=0", "--grep",
                           self._DIAG_AT_LOAD, "-n", "5"],
                timeout=5,
            )
            source = found if found.get("success") else log
            extras["plugin_load_values_" + key] = [
                line for line in self._diag_service_lines(source.get("stdout", ""), 200, names)
                if self._DIAG_AT_LOAD in line
            ][-3:] or ["none logged"]
        # The previous boot's logs are read from their end, and a driver that
        # kept failing after a wake fills that end: the sleep and the wake
        # itself scrolled out of exactly the boots a report is about. So the
        # wake is found first, with one cheap read, and the stretch around it
        # is read on its own and put ahead of the end. The wake is the one the
        # wake notice judges (the first after WiFi was last up), not the last:
        # someone whose WiFi did not come back often sleeps again to retry.
        anchor = self._diag_wake_anchor()
        network_cmd = journal + ["-b", "-1", "-u", "NetworkManager", "-u", "iwd",
                                 "-u", "wpa_supplicant"]
        log = self._run_cmd(network_cmd + ["-n", "2000"], timeout=5)
        if log.get("success"):
            first, rest = self._diag_window(network_cmd + ["-n", "2000"], anchor)
            extras["network_log_previous_boot"] = self._diag_network_with_window(
                log.get("stdout", ""), self._diag_join(first, rest), names, first)
        else:
            extras["network_log_previous_boot"] = [
                f"unavailable: {(log.get('stderr') or '')[:120]}"]

        terms = self._DIAG_KERNEL_TERMS
        for name in (iface, driver):
            if name and name not in ("none", "unknown"):
                terms += "|" + re.escape(name)
        for key, boot in (("kernel_log_this_boot", "0"), ("kernel_log_previous_boot", "-1")):
            # --no-hostname: the hostname can be a person's name.
            kernel_cmd = ["/usr/bin/journalctl", "-k", "-b", boot, "--no-hostname",
                          "-o", "short-monotonic", "-n", self._DIAG_JOURNAL_MAX, "--no-pager"]
            log = self._run_cmd(kernel_cmd, timeout=5)
            if not log.get("success"):
                extras[key] = [f"unavailable: {(log.get('stderr') or '')[:120]}"]
                continue
            first, rest = self._diag_window(kernel_cmd, anchor) if boot == "-1" else ("", "")
            extras[key] = self._diag_kernel_with_window(
                log.get("stdout", ""), self._diag_join(first, rest), terms,
                self._diag_first_stamp(first))
        return extras

    def _diag_pcie(self, iface: str, read) -> tuple:
        """(the card's PCIe power files, its link registers and its port's),
        for the diagnostics. read reads one sysfs file as text."""
        pcie, link = {}, {}
        # The card the fixes are for, not a USB adapter that happens to be
        # the interface in use.
        card = self._driver_interface()
        # With no netdev for it (what a card that died on a wake leaves), its
        # wiphy, and the PCI device under it, often outlive the netdev, and
        # their link registers are the evidence a report needs.
        phy = None if card else self._managed_phy()
        card = card or (None if phy else iface)
        device = None
        if phy:
            device = os.path.realpath(os.path.join(SYSFS_IEEE80211, phy, "device"))
        elif card and card != "none" and IFACE_RE.match(card):
            device = os.path.realpath(os.path.join(SYSFS_NET, card, "device"))
        if device:
            for name in ("l0s_aspm", "l1_aspm", "l1_1_aspm", "l1_2_aspm",
                         "l1_1_pcipm", "l1_2_pcipm"):
                path = os.path.join(device, "link", name)
                if os.path.exists(path):
                    pcie[name] = read(path)
            # Clock PM is the one PCIe power feature the fixes leave on, so
            # it is read too, with the device's power state (D0 is awake).
            clkpm = os.path.join(device, "link", "clkpm")
            if os.path.exists(clkpm):
                pcie["clkpm"] = read(clkpm)
            pcie["power_state"] = read(os.path.join(device, "power_state"))
            pcie["power_control"] = read(os.path.join(device, "power", "control"))
            pcie["runtime_status"] = read(os.path.join(device, "power", "runtime_status"))
            # What the link itself is doing, from both ends: whether ASPM and
            # its L1 substates are on in the card's and the port's own
            # registers, which the files above do not show when the kernel
            # hides them, and which is what the fixes' PCIe writes change.
            try:
                link = self._diag_pcie_link(device)
            except Exception as e:
                link = {"error": f"unreadable: {e}"}
        return pcie, link

    def _diag_nm_state(self) -> tuple:
        """(NetworkManager's device list, its general state), for the
        diagnostics."""
        # DEVICE,TYPE,STATE only: the CONNECTION column is the network's name.
        # "-e no" because terse mode otherwise escapes the colons in a device
        # named by its address (a paired phone), which the redaction would miss.
        devices = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-e", "no", "-f", "DEVICE,TYPE,STATE",
             "device", "status"],
            timeout=3,
        )
        # Only the kinds of device that bear on WiFi: a VPN or a virtual
        # device is often named after something personal (a WireGuard device
        # takes its config file's name). Split from the right, so a device
        # named by its address keeps its colons out of the TYPE column.
        if devices.get("success"):
            kept = "\n".join(
                line for line in (devices.get("stdout") or "").splitlines()
                if len(line.rsplit(":", 2)) == 3
                and line.rsplit(":", 2)[1] in ("wifi", "ethernet", "loopback", "wifi-p2p"))
        else:
            kept = devices.get("stderr", "")
        # NetworkManager that missed the resume stays asleep with every
        # device unmanaged, which the device list alone does not show.
        nm_state, nm_radio = self._nm_general_state()
        return kept, {"state": nm_state or "unread", "wifi_radio": nm_radio or "unread"}

    # The previous boot's suspend lines, with -o short-unix and --no-hostname.
    _DIAG_ANCHOR_RE = re.compile(
        r"^([0-9]{9,11})(?:\.[0-9]+)? (?:\S+ )?kernel: PM: suspend (entry|exit)")
    # Read around the wake: from just before its sleep to five minutes after
    # it. The journal stamps the resume with the time of the wake, so a
    # window counted from the sleep alone would miss it.
    _DIAG_WINDOW_BEFORE = 5
    _DIAG_WINDOW_AFTER = 300
    # Where the first of the window's two reads ends, after the wake.
    _DIAG_WINDOW_RESUME = 5
    # Lines of the end of the boot kept beside that stretch.
    _DIAG_TAIL_LINES = 15
    # A short-monotonic line's stamp: "[  1234.567890] ...".
    _DIAG_STAMP_RE = re.compile(r"^\[\s*([0-9]+(?:\.[0-9]+)?)\]")

    def _diag_wake_anchor(self) -> tuple | None:
        """(when the previous boot's sleep began, when it woke from that
        sleep or None), in Unix seconds, or None: no sleep, no previous boot,
        or a journalctl without --grep.

        The wake is the one the wake notice judges, from the same events read
        (_wake_events_read, _wake_candidate): the first after WiFi was last
        up. With no such wake (WiFi never came up that boot), the last sleep.
        """
        events = self._wake_events_read()
        if events.get("success"):
            parsed = _wake_events(events.get("stdout", ""))
            found = _wake_candidate(parsed)
            if found:
                return int(parsed[found[0]][0]), int(parsed[found[1]][0])
        found = self._run_cmd(
            ["/usr/bin/journalctl", "-k", "-b", "-1", "--no-hostname", "-o", "short-unix",
             "--grep", "PM: suspend (entry|exit)", "-n", "4", "--no-pager"],
            timeout=5,
        )
        if not found.get("success"):
            return None
        hits = [(int(hit.group(1)), hit.group(2)) for hit in map(
            self._DIAG_ANCHOR_RE.match, (found.get("stdout") or "").splitlines()) if hit]
        entries = [i for i, (_t, kind) in enumerate(hits) if kind == "entry"]
        if not entries:
            return None
        woke = next((t for t, kind in hits[entries[-1] + 1:] if kind == "exit"), None)
        return hits[entries[-1]][0], woke

    def _diag_window(self, cmd: list, anchor: tuple | None) -> tuple:
        """cmd's lines around the wake anchor names, in two reads: (from
        just before the sleep to just after the wake, the rest of the five
        minutes after it), "" for a part not read.

        Two reads, each under cmd's own -n: journalctl keeps the LAST n lines
        of a range, so one read of the whole five minutes lost the sleep and
        the resume itself to a driver that flooded the log after the wake.
        """
        if not anchor:
            return "", ""
        slept, woke = anchor
        woke = woke or slept
        parts = []
        for since, until in ((slept - self._DIAG_WINDOW_BEFORE, woke + self._DIAG_WINDOW_RESUME),
                             (woke + self._DIAG_WINDOW_RESUME, woke + self._DIAG_WINDOW_AFTER)):
            log = self._run_cmd(cmd + ["--since", f"@{since}", "--until", f"@{until}"], timeout=5)
            parts.append(log.get("stdout", "") if log.get("success") else "")
        return parts[0], parts[1]

    @staticmethod
    def _diag_join(*texts) -> str:
        """Several reads of one journal as one, each line once, in order."""
        lines = dict.fromkeys(line for text in texts for line in text.splitlines()
                              if line.strip() and line.strip() != "-- No entries --")
        return "\n".join(lines)

    @classmethod
    def _diag_first_stamp(cls, text: str) -> float | None:
        """The monotonic stamp of the sleep in a window's first read: its
        "PM: suspend entry", else the read's first line. None when empty."""
        stamps = []
        for line in text.splitlines():
            found = cls._DIAG_STAMP_RE.match(line)
            if not found:
                continue
            if " PM: suspend entry" in line:
                return float(found.group(1))
            stamps.append(float(found.group(1)))
        return stamps[0] if stamps else None

    @staticmethod
    def _diag_split_window(tail: str, window: str) -> tuple:
        """(the window's lines the tail does not have, whether the two
        meet). They meet when the window ends inside the tail: then nothing
        lies between them."""
        tail_lines = set(tail.splitlines())
        lines = [line for line in window.splitlines() if line.strip()]
        if not lines:
            return [], True
        return [line for line in lines if line not in tail_lines], lines[-1] in tail_lines

    _DIAG_GAP = "(lines between the wake and the end of the boot left out)"

    def _diag_kernel_with_window(self, tail: str, window: str, terms: str,
                                 anchor: float | None = None) -> list:
        """_diag_kernel_lines for the tail, with the stretch around the wake
        ahead of it when the tail does not reach back that far. anchor is
        the monotonic stamp of the wake's sleep: when there are too many
        lines, the ones from it on are kept."""
        extra, meet = self._diag_split_window(tail, window)
        if not extra:
            return self._diag_kernel_lines(tail, terms, anchor=anchor)
        if meet:
            return self._diag_kernel_lines("\n".join(extra) + "\n" + tail, terms, anchor=anchor)
        cap = self._DIAG_KERNEL_LINES
        return (self._diag_kernel_lines("\n".join(extra), terms, cap - self._DIAG_TAIL_LINES,
                                        anchor=anchor)
                + [self._DIAG_GAP]
                + self._diag_kernel_lines(tail, terms, self._DIAG_TAIL_LINES))

    def _diag_network_with_window(self, tail: str, window: str, names: tuple,
                                  first: str = "") -> list:
        """The network services' lines the same way. first is the window's
        first read (the sleep to just after the wake): its lines are kept
        ahead of the rest when there are too many."""
        limit = self._DIAG_SERVICE_LINES
        keep = frozenset(line for line in first.splitlines() if line.strip())
        extra, meet = self._diag_split_window(tail, window)
        if not extra:
            return self._diag_service_lines(tail, limit, names, events_first=True, first=keep)
        if meet:
            return self._diag_service_lines("\n".join(extra) + "\n" + tail, limit, names,
                                            events_first=True, first=keep)
        tail_n = self._DIAG_TAIL_LINES
        return (self._diag_service_lines("\n".join(extra), limit - tail_n, names,
                                         events_first=True, first=keep)
                + [self._DIAG_GAP]
                + self._diag_service_lines(tail, tail_n, names, events_first=True))

    # The link registers lspci decodes, and their continuation lines.
    _PCIE_LINK_KEYS = ("LnkCap:", "LnkCtl:", "LnkSta:", "L1SubCap:", "L1SubCtl1:", "L1SubCtl2:")
    _PCIE_BDF_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]\Z")
    _PCIE_FIELD_RE = re.compile(r"^[A-Za-z][\w ]*:\s")
    # The same registers raw, where there is setpci but no lspci.
    _PCIE_SETPCI_REGS = (
        ("LnkCtl", "CAP_EXP+0x10.w"), ("LnkSta", "CAP_EXP+0x12.w"),
        ("L1SubCtl1", "ECAP_L1PM+0x08.l"), ("L1SubCtl2", "ECAP_L1PM+0x0c.l"),
    )

    def _diag_pcie_link(self, device: str) -> dict:
        """The PCIe link registers of the WiFi device and of the port above
        it (its parent in sysfs), read only. {} when it is not a PCI device."""
        bdf = os.path.basename(device)
        if not self._PCIE_BDF_RE.match(bdf):
            return {}
        out = {"wifi_device": bdf, "wifi": self._pcie_link_lines(bdf)}
        port = os.path.basename(os.path.dirname(device))
        if self._PCIE_BDF_RE.match(port):
            out["upstream_port"] = port
            out["upstream"] = self._pcie_link_lines(port)
        return out

    def _pcie_link_lines(self, bdf: str) -> list:
        """LnkCap, LnkCtl, LnkSta and the L1 substate lines for one device,
        from `lspci -vv -s`, or the raw words from setpci without lspci."""
        lspci = _first_existing([os.path.join(d, "lspci") for d in TOOL_DIRS])
        if lspci:
            result = self._run_cmd([lspci, "-vv", "-s", bdf], timeout=3)
            if not result.get("success"):
                return [f"unavailable: {(result.get('stderr') or '')[:120]}"]
            lines = self._pcie_link_filter(result.get("stdout", ""))
            if not any(line.startswith(self._PCIE_LINK_KEYS) for line in lines):
                lines.append("no link registers listed")
            return lines
        setpci = _first_existing([os.path.join(d, "setpci") for d in TOOL_DIRS])
        if not setpci:
            return ["unavailable: no lspci or setpci"]
        lines = []
        for label, register in self._PCIE_SETPCI_REGS:
            result = self._run_cmd([setpci, "-s", bdf, register], timeout=3)
            value = (result.get("stdout") or "").strip()
            lines.append(f"{label}: {value if result.get('success') and value else 'unreadable'}")
        return lines

    # lspci's first line for a device: "01:00.0 Network controller: ...".
    # A device that fell off the bus reads "(rev ff)" there, and "!!! Unknown
    # header type 7f" below it, with no link registers at all.
    _PCIE_HEADER_RE = re.compile(r"^(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7] ")

    @classmethod
    def _pcie_link_filter(cls, text: str) -> list:
        """The link lines of `lspci -vv`, each joined with the more indented
        lines that continue it, after lspci's first line (the device, whose
        "(rev ff)" says it no longer answers) and any "Unknown header type"
        line."""
        out, indent = [], None
        for raw in text.splitlines():
            stripped = raw.strip()
            lead = len(raw) - len(raw.lstrip())
            if (not out and cls._PCIE_HEADER_RE.match(stripped)) or "Unknown header type" in stripped:
                out.append(" ".join(stripped.split()))
                indent = None
            elif stripped.startswith(cls._PCIE_LINK_KEYS):
                out.append(" ".join(stripped.split()))
                indent = lead
            elif (indent is not None and stripped and lead > indent
                  and not cls._PCIE_FIELD_RE.match(stripped)):
                out[-1] += " " + " ".join(stripped.split())
            else:
                indent = None
        return [line[:cls._DIAG_LINE_MAX] for line in out]

    # How long a collection stays fresh enough for Save diagnostics to write.
    _DIAG_SAVE_REUSE_SECONDS = 120

    async def save_diagnostic_info(self) -> dict:
        """Write diagnostics to a file in the settings directory as a
        fallback when clipboard is unavailable.

        The panel offers this right after a Copy diagnostics whose clipboard
        failed, so the collection that just finished is written, not a second
        one (about a minute more). Only a collection that worked is written:
        a file holding an error is no report."""
        try:
            last = getattr(self, "_last_diagnostics", None)
            if last and time.monotonic() - last[0] < self._DIAG_SAVE_REUSE_SECONDS:
                info = last[1]
            else:
                info = await self.get_diagnostic_info()
            if not isinstance(info, dict) or info.get("success") is False:
                return {
                    "success": False,
                    "error": "collect_failed",
                    "message": "Couldn't collect the diagnostics. Try again in a moment.",
                }
            diag_path = _diagnostics_path()
            if not _write_no_follow(diag_path, json.dumps(info, indent=2)):
                return {
                    "success": False,
                    "error": "write_failed",
                    "message": "Couldn't write the diagnostics file.",
                }
            return {"success": True, "path": diag_path}
        except Exception as e:
            decky.logger.error(f"save_diagnostic_info error: {e}")
            return {"success": False, "error": "write_failed",
                    "message": "Couldn't write the diagnostics file."}

    # ---- Status ----

    async def get_status(self) -> dict:
        """Collect status off the event loop.

        The body below makes roughly a dozen blocking subprocess calls and the
        frontend polls it every few seconds, so running it inline stalled the
        event loop for as long as NM took to answer. One thread hop covers the
        whole collection rather than wrapping each call individually.
        """
        # Collection used to be serialized by the blocked event loop. Now that
        # it runs in a worker thread nothing stops the next poll starting while
        # this one is still out, and on an unresponsive NetworkManager a dozen
        # of them pile up, each spawning its own subprocesses.
        #
        # A counter rather than a flag, because with a flag the first caller
        # to finish clears it for everyone still running. Callers arriving
        # before any result exists are handled separately, below.
        #
        # The reply is the live values from the last completed pass, which may
        # be as old as that pass took - but its settings are re-read, because
        # the panel renders every toggle from them and handing back a snapshot
        # taken before the user's change makes the toggle spring back.
        if getattr(self, "_collect_depth", 0) > 0:
            # A collection that never returns would otherwise hold this
            # branch forever: the panel blank with nothing to say or, once a
            # pass has completed, frozen on its values as if all were well.
            # _run_cmd no longer waits on a child stuck against a wedged
            # driver, but a pass made of many such calls still takes minutes.
            started = getattr(self, "_collect_started_at", 0.0)
            if started and (time.monotonic() - started) > 90:
                self._log_throttled(
                    "collect_wedged",
                    "Status collection has not returned in 90s; reporting a "
                    "read failure rather than leaving the panel empty",
                )
                return self._with_activity({
                    "success": False,
                    "error": "unexpected",
                    "message": "Couldn't read WiFi status.",
                    "connected": False,
                    "support_tier": self._get_support_tier(),
                    "version": getattr(decky, "DECKY_PLUGIN_VERSION", "?"),
                    "settings": _load_settings(),
                    "live": {},
                    "drift": {},
                    "external": {},
                })

            last = getattr(self, "_last_status", None)
            if last is not None:
                # deepcopy, not dict(): a shallow copy shares live and drift
                # with the cached object, so anything that later mutates them
                # after this early return would corrupt every future reply.
                cached = copy.deepcopy(last)
                try:
                    cached["settings"] = _load_settings()
                except Exception:
                    pass
                self._report_settings_recovery(cached)
                return self._with_activity(cached)
            # No previous result to hand back. Falling through here is what
            # the guard exists to prevent: the first collection of a cold boot
            # routinely outlives the poll interval, so every caller would
            # start its own. Report not-ready instead; the panel renders a
            # disconnected state correctly.
            return self._with_activity({
                "success": True,
                # Not the same as knowing it is disconnected. The panel holds
                # its banners and live rows while this is set, because saying
                # "not connected" at the one moment the plugin knows least is
                # the disconnect flash it already guards against elsewhere.
                "initializing": True,
                "connected": False,
                "support_tier": self._get_support_tier(),
                "version": getattr(decky, "DECKY_PLUGIN_VERSION", "?"),
                "settings": _load_settings(),
                "live": {},
                "drift": {},
                "external": {},
            })

        self._collect_depth = getattr(self, "_collect_depth", 0) + 1
        if self._collect_depth == 1:
            self._collect_started_at = time.monotonic()
        try:
            status, pending, actions = await asyncio.to_thread(self._collect_status)
        finally:
            self._collect_depth = max(0, getattr(self, "_collect_depth", 1) - 1)
        if status.get("live", {}).get("pins_inactive_iwd"):
            # Proposed with both pins treated as off, so the fresh read of
            # settings the applier makes must not stop them as still on.
            for action in actions:
                if (action["kind"] == "pin_cleanup"
                        and action["property"] in self._ELIMINATION_PINS):
                    action["iwd"] = True
        if pending or actions:
            try:
                self._apply_status_actions(status, pending, actions)
            except Exception as e:
                # Never let reconciliation break a status read; the frontend
                # has no useful response to an RPC error here and would just
                # keep showing stale values with no signal. Throttled: with
                # settings that cannot be saved this fails on every poll.
                self._log_throttled("status_actions", f"status reconciliation error: {e}")

        # The settings in `status` are the worker thread's snapshot, taken
        # before any of this ran, and the frontend renders every toggle
        # straight off them. A poll that started before the user touched a
        # toggle would otherwise report the pre-touch value and the toggle
        # would appear to spring back. Hand back what is on disk now.
        if status.get("success"):
            try:
                status["settings"] = _load_settings()
                status.setdefault("live", {}).update(
                    self._streaming_live(status["settings"])
                )
            except Exception as e:
                decky.logger.error(f"settings refresh error: {e}")

        self._report_settings_recovery(status)
        self._last_status = status
        return self._with_activity(status)

    def _with_activity(self, status: dict) -> dict:
        """status, with what the panel needs to know about work in progress.

        settings_unwritable: the last settings save failed, so a toggle that
        springs back is explained rather than looking ignored.
        profile_change_in_flight: a change that rewrites profiles or the
        backend is running, and the other profile controls would be refused.
        Worked out on every reply, cached ones included, since both change
        between polls.
        """
        status["settings_unwritable"] = bool(_SETTINGS_STATE["save_failed"])
        status["profile_change_in_flight"] = (
            self._profile_change_in_flight() or self._backend_switch_running()
        )
        return status

    @staticmethod
    def _report_settings_recovery(status: dict):
        """Tell the panel the settings file was unreadable and set aside.

        Reported on every poll until the panel acknowledges it, rather than
        once: get_status is also called by the plugin itself (Fix now, the
        stranded pin watch), and a one-shot flag those calls consumed would
        never reach the user.

        Worked out from the set-aside file and the acknowledgement saved in
        settings, not from anything held in memory, so a plugin reload or a
        Steam restart in between cannot lose it. The settings are the ones
        this status already carries, so the poll reads nothing extra.
        """
        identity = _corrupt_settings_identity()
        settings = status.get("settings")
        acknowledged = (settings.get("corrupt_acknowledged")
                        if isinstance(settings, dict) else None)
        if identity is not None and identity != acknowledged:
            status["settings_recovered"] = True
        else:
            status.pop("settings_recovered", None)

    async def acknowledge_settings_recovered(self) -> dict:
        """The panel has shown the notice; stop reporting it."""
        try:
            # The file is looked at before the settings are loaded. Loading
            # can itself set aside a newly unreadable file, and that one has
            # not been shown to anyone yet, so it must not be the one
            # recorded as seen.
            identity = _corrupt_settings_identity()
            if identity is None:
                return {"success": True}
            settings = _load_settings()
            if settings.get("corrupt_acknowledged") != identity:
                settings["corrupt_acknowledged"] = identity
                _save_settings(settings)
            return {"success": True}
        except Exception as e:
            decky.logger.error(f"acknowledge_settings_recovered error: {e}")
            return self._unexpected_response(e)

    def _apply_status_actions(self, status: dict, pending: dict, actions: list):
        """Carry out what _collect_status proposed, on the event loop.

        Every guard is re-checked against a FRESH read of settings rather than
        the worker thread's snapshot, which may be up to ~20s old. Without
        that, a poll begun before the user touched a toggle would undo the
        change they just made.

        This deliberately runs ON the event loop rather than in a thread.
        Most setters are await-free coroutines, so the loop is what
        serializes them against this. The profile setters that do await (the
        lock, the band, Allow WPA2) hold the reconciliation guard across the
        whole call instead. Moving this to a thread would make it concurrent
        with all of them, which a lock could still handle - but that is an
        eight-setter refactor, not a property of the current arrangement. The cost is
        that it holds the loop, so every call here is given a short timeout
        and nothing runs in steady state - actions exist only while something
        has actually drifted.
        """
        settings = _load_settings()
        # The leftover cleanup's one nmcli call per poll.
        self._leftover_call_made = False

        # Never pin an address while the radio is on a band the profile does
        # not want. Writing both leaves a profile demanding one band and an
        # access point that only exists on the other, which nothing can
        # satisfy and which the plugin would then keep reasserting.
        #
        # This is judged from the band the device is CURRENTLY on, not from
        # whether a band reassertion happens to be proposed in the same pass.
        # Setting the band does not move an existing association, so the very
        # next poll sees the band as correct, proposes nothing for it, and
        # would otherwise pin the old band's access point anyway.
        def band_conflict(action: dict) -> bool:
            return self._band_conflicts(
                settings,
                status.get("live", {}).get("frequency", ""),
                action.get("active_ssid"),
            )

        for action in actions:
            kind = action["kind"]

            if kind == "bssid_repoint" and band_conflict(action):
                continue

            if kind == "priority":
                self._apply_priority(action, settings, status, pending)
            elif kind == "ipv6":
                self._apply_ipv6(action, settings, status, pending)
            elif kind == "ipv6_adopt":
                # Not while a profile change (Reset above all) is under way:
                # it works from settings it read before, and would never see
                # a profile adopted here.
                if (settings.get("ipv6_disabled") and not settings.get("ipv6_uuids")
                        and not self._profile_change_in_flight()):
                    pending["ipv6_uuids"] = [action["uuid"]]
            elif kind == "band":
                self._apply_band(action, settings, status, pending)
            elif kind == "cake":
                self._apply_cake(action, settings, status, pending)
            elif kind == "pin_cleanup":
                self._apply_pin_cleanup(action, settings, status, pending)
            elif kind == "bssid_repoint":
                self._apply_bssid_repoint(action, settings, status, pending)
            elif kind in ("dns_cleanup", "wpa2_restore"):
                self._apply_leftover_cleanup(action, settings, pending)

        if pending:
            settings.update(pending)
            _save_settings(settings)

    def _apply_priority(self, action: dict, settings: dict, status: dict,
                        pending: dict):
        """Give this profile precedence over the other copies of the network.

        `priority_set` is a one-shot flag, so an install already marked done
        can never revisit the decision on its own - which is the state every
        existing install is in, ties and all. That is why the duplicate check
        is allowed to force a recheck.

        Returning early leaves the action undone and the next poll free to
        propose it again.
        """
        uuid = action["uuid"]
        if settings.get("priority_set") and not action.get("force"):
            return
        if self._reassert_exhausted("priority"):
            return
        bumped = self._nmcli_modify(
            uuid, "connection.autoconnect-priority",
            str(action.get("value", self._PRIORITY_BASE)), timeout=2
        )
        self._record_reassert("priority", bumped["success"])
        if bumped["success"]:
            pending["priority_set"] = True
            if uuid == getattr(self, "_dup_checked_for", None):
                self._dup_outranks = True
        else:
            # Don't record it as done when it wasn't, or the profile
            # never gets the priority that stops NM preferring a
            # duplicate on boot.
            self._log_throttled(
                "priority",
                f"Couldn't set autoconnect priority on {uuid}: "
                f"{_tool_text(bumped.get('stderr', '')[:120])}",
            )

    def _apply_ipv6(self, action: dict, settings: dict, status: dict,
                    pending: dict):
        """Put IPv6 back to disabled after something else turned it on.

        Returning early leaves the action undone and the next poll free to
        propose it again.
        """
        uuid = action["uuid"]
        if not settings.get("ipv6_disabled"):
            status["drift"].pop("ipv6", None)
            return
        if self._reassert_exhausted("ipv6"):
            return
        healed = self._nmcli_modify(uuid, "ipv6.method", "disabled", timeout=2)
        self._record_reassert("ipv6", healed["success"])
        self._log_throttled(
            "ipv6",
            f"IPv6 drifted to {action['observed']!r} on {uuid}, "
            f"reasserting disabled: "
            f"{'ok' if healed['success'] else 'failed'}",
        )

    def _apply_band(self, action: dict, settings: dict, status: dict,
                    pending: dict):
        """Write the preferred band back to the profile in use.

        Returning early leaves the action undone and the next poll free to
        propose it again.
        """
        uuid = action["uuid"]
        if not settings.get("band_preference_enabled"):
            status["drift"].pop("band_preference", None)
            return
        if settings.get("band_preference") != action["value"]:
            status["drift"].pop("band_preference", None)
            return
        # A setter mid-change on this profile, or a backend switch: a switch
        # to iwd clears the band first, and writing it back before the new
        # backend is in force leaves a pin NetworkManager cannot use there.
        if self._profile_change_in_flight() or self._backend_switch_running():
            return
        if self._reassert_exhausted("band"):
            return
        # The setter checks a profile can be held to 6 GHz before writing it,
        # but the profile drifting here may be another saved copy of the
        # network, which it never saw. One that cannot join a 6 GHz access
        # point is left alone, with the drift still reported.
        if action["value"] == "6GHz" and self._six_ghz_refusal_cached(uuid):
            return
        healed = self._nmcli_modify(
            uuid, "802-11-wireless.band", action["value"], timeout=2
        )
        self._record_reassert("band", healed["success"])
        if healed["success"]:
            pending["pins_owned"] = _with_pin(settings, "802-11-wireless.band")
            known = list(settings.get("band_preference_uuids", []))
            if uuid not in known:
                known.append(uuid)
                pending["band_preference_uuids"] = self._capped(
                    known, "802-11-wireless.band"
                )
        self._log_throttled(
            "band",
            f"Band drifted to {action['observed']!r} on {uuid}, "
            f"reasserting {action['value']!r}: "
            f"{'ok' if healed['success'] else 'failed'}",
        )

    def _apply_cake(self, action: dict, settings: dict, status: dict,
                    pending: dict):
        """Put the queueing discipline back on the interface.

        Nothing else clears this drift: Optimize Safe covers the safe tier
        only, so without a reassertion the warning would stand forever with no
        control that resolves it.

        Returning early leaves the action undone and the next poll free to
        propose it again.
        """
        # The gate is checked again here, against settings read now: the poll
        # that proposed this may have started before auto mode was turned on,
        # and putting CAKE back then leaves it on with the panel saying paused.
        if not settings.get("cake_enabled") or not self._volatile_gate_open(settings):
            status["drift"].pop("cake", None)
            return
        # This runs on the event loop, and a kernel without sch_cake
        # would otherwise retry three subprocesses every three seconds
        # forever, blocking every other call for as long as it takes.
        # Give up after a few consecutive failures and leave the drift
        # flag standing, which is the honest report anyway.
        if self._reassert_exhausted("cake"):
            return
        iface = action["iface"]
        if not getattr(self, "_cake_module_loaded", False):
            modprobe = _first_existing(
                ["/usr/bin/modprobe", "/usr/sbin/modprobe"]
            )
            if modprobe:
                self._run_cmd([modprobe, "sch_cake"], timeout=2)
            self._cake_module_loaded = True
        applied = self._run_cmd([
            "/usr/bin/tc", "qdisc", "replace", "dev", iface, "root",
            "cake", "unlimited", "diffserv4", "nat", "ack-filter",
        ], timeout=2)
        self._record_reassert("cake", applied["success"])
        if applied["success"]:
            self._run_cmd(
                ["/usr/bin/ip", "link", "set", iface, "txqueuelen", "256"],
                timeout=2,
            )
            status["live"]["cake_applied"] = True
            status["drift"].pop("cake", None)
        self._log_throttled(
            "cake",
            f"CAKE drifted on {iface}, reasserting: "
            f"{'ok' if applied['success'] else 'failed'}",
        )

    def _apply_pin_cleanup(self, action: dict, settings: dict, status: dict,
                           pending: dict):
        """Clear one pin we left behind, or release one that is stranding us.

        Returning early leaves the action undone and the next poll free to
        propose it again.
        """
        uuid = action["uuid"]
        list_key = action["list_key"]
        prop = action["property"]
        enabled_key = next(
            e for e, l, _, _v in self._PIN_PROPERTIES if l == list_key
        )
        # A stranded release is the one case where the feature being
        # ON is not a reason to leave the value alone - it is the
        # reason the value is there and the reason we are offline.
        stranded = bool(action.get("stranded"))
        if settings.get(enabled_key) and not stranded and not action.get("iwd"):
            return
        # A setter is mid-change on this profile. Today the band
        # setter writes the pin and saves the flag with no await
        # between them, so a poll cannot land in that gap and see a
        # pin whose flag is not saved yet - but cleanup is a SECOND
        # writer to the same property, and that invariant is one
        # edit away from not holding. Skipping costs one poll.
        if self._profile_change_in_flight():
            return
        if self._reassert_exhausted(f"pin_cleanup:{list_key}"):
            return
        done = self._nmcli_modify(
            uuid, prop, action.get("off_value", ""), timeout=2
        )
        detail = done.get("stderr", "")
        # A profile that no longer exists is resolved, not failed.
        # Counting it as a failure pushed cleanup into its cooldown
        # while it was in fact draining the list.
        resolved = done["success"] or "unknown connection" in detail.lower()
        self._record_reassert(f"pin_cleanup:{list_key}", resolved)
        # Build on what an earlier action in this pass already decided for
        # the list, or the last one written wins and undoes the others.
        tracked = pending.get(list_key, settings.get(list_key, []))
        if resolved:
            pending[list_key] = [u for u in tracked if u != uuid]
            off_value = action.get("off_value", "")
            if stranded:
                # Record the moment, not a bare flag: the panel should
                # still be explaining this once WiFi returns, which is
                # the only time the user is there to read it.
                self._pin_released_at = time.time()
                self._released_at = {
                    **getattr(self, "_released_at", {}), prop: time.time()
                }
                # The setting is still on and should land on whichever access
                # point works once we are back. With the panel closed only the
                # background watch can make that happen, so tell it to.
                self._repoll_after_release = True
                # And say so on THIS poll. The collector reads the
                # timestamp before the actions run, so leaving it to
                # the next poll would make the release silent at the
                # exact moment it happened.
                status.setdefault("external", {})["pin_released"] = True
                self._log_throttled(
                    f"pin_release:{list_key}",
                    f"Released {prop} on {uuid}: the device had been "
                    f"offline for over "
                    f"{self._STRANDED_SECONDS}s and this setting was "
                    f"the most likely reason",
                )
            else:
                self._log_throttled(
                    f"pin_cleanup:{list_key}",
                    f"Cleared a leftover {prop} from {uuid}"
                    if not off_value else
                    f"Returned {prop} to {off_value} on {uuid}",
                )
        else:
            # Move it to the back. Only the head is attempted each
            # poll, so one profile that cannot be cleared would
            # otherwise keep every other entry from ever being tried.
            rest = [u for u in tracked if u != uuid]
            if rest:
                pending[list_key] = rest + [uuid]

    def _apply_leftover_cleanup(self, action: dict, settings: dict, pending: dict):
        """Undo custom DNS, or Allow WPA2 here, on one profile Reset,
        uninstall or the DNS toggle could not reach at the time.

        Both used to be recorded as left for a retry that nothing made. One
        profile per poll, with a short timeout and the reassertion cooldown,
        since this runs on the event loop. Returning early leaves the action
        undone and the next poll free to propose it again.
        """
        uuid = action["uuid"]
        kind = action["kind"]
        list_key = "dns_uuids" if kind == "dns_cleanup" else "wpa2_restore_uuids"
        tracked = pending.get(list_key, settings.get(list_key, []))
        if uuid not in tracked:
            return
        # DNS turned back on meanwhile: the list is the live one again.
        if kind == "dns_cleanup" and settings.get("dns_enabled"):
            return
        if self._profile_change_in_flight() or self._backend_switch_running():
            return
        if self._reassert_exhausted(kind):
            return
        # One nmcli call per poll, of a second at most, whatever is left:
        # this runs on the event loop, and a profile that kept failing made
        # up to three two-second calls on every poll.
        if getattr(self, "_leftover_call_made", False):
            return
        self._leftover_call_made = True
        done = self._leftover_cleanup_step(kind, uuid)
        if done is None:
            return  # part way; the next poll takes the next step
        self._record_reassert(kind, done)
        rest = [u for u in tracked if u != uuid]
        if done:
            pending[list_key] = rest
            self._log_throttled(
                kind,
                f"Gave {uuid} its automatic DNS back" if kind == "dns_cleanup"
                else f"Put {uuid} back to WPA3 only",
            )
        elif rest:
            # To the back, so one that keeps failing does not hold up the rest.
            pending[list_key] = rest + [uuid]

    # The nmcli writes that give a profile its automatic DNS back, in
    # _clear_dns's order: the router's DNS let back in before ours come out.
    _DNS_CLEANUP_STEPS = (("ipv4.ignore-auto-dns", "no"), ("ipv6.ignore-auto-dns", "no"),
                          ("ipv4.dns", ""))

    def _leftover_cleanup_step(self, kind: str, uuid: str) -> bool | None:
        """One nmcli call of the leftover cleanup of uuid: True when it is
        done (or the profile is gone), False when the call failed, None when
        it went through and another step is left for the next poll."""
        steps = self.__dict__.setdefault("_leftover_steps", {})
        key = (kind, uuid)
        step = steps.get(key, 0)

        def gone(result: dict) -> bool:
            return "unknown connection" in (result.get("stderr") or "").lower()

        if kind == "dns_cleanup":
            prop, value = self._DNS_CLEANUP_STEPS[min(step, len(self._DNS_CLEANUP_STEPS) - 1)]
            result = self._nmcli_modify(uuid, prop, value, timeout=1)
            if gone(result):
                steps.pop(key, None)
                return True
            if not result["success"]:
                return False
            if step + 1 >= len(self._DNS_CLEANUP_STEPS):
                steps.pop(key, None)
                return True
            steps[key] = step + 1
            return None
        # Allow WPA2 here: read the profile's security, then (next poll)
        # put sae back only while it still says what we wrote.
        if step == 0:
            result = self._run_cmd(
                ["/usr/bin/nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt",
                 "con", "show", "uuid", uuid],
                timeout=1,
            )
            if gone(result):
                return True
            if not result["success"]:
                return False
            _, sep, value = result.get("stdout", "").partition(":")
            if not sep or value.strip() != "wpa-psk":
                return True
            steps[key] = 1
            return None
        steps.pop(key, None)
        done = self._nmcli_modify(uuid, "802-11-wireless-security.key-mgmt", "sae", timeout=1)
        return done["success"] or gone(done)

    def _apply_bssid_repoint(self, action: dict, settings: dict, status: dict,
                             pending: dict):
        """Move the address pin to the profile NetworkManager is actually using.

        Returning early leaves the action undone and the next poll free to
        propose it again.
        """
        uuid = action["uuid"]
        # The lock may have been switched off while this poll was in
        # flight. Re-pointing then would restore a BSSID the user just
        # cleared, leaving the profile pinned with the toggle showing
        # off and nothing left to clear it.
        if not settings.get("bssid_lock_enabled"):
            # The user turned the lock off while this poll was in
            # flight. Nothing has drifted; leaving the flag set shows
            # a drift warning for a setting that is no longer on.
            status["drift"].pop("bssid_lock", None)
            return
        # A setter is deliberately changing this profile right now.
        # A band change clears the BSSID on purpose so NM can find an
        # AP on the other band, and only re-locks once it associates;
        # writing the old BSSID back mid-flight can leave the profile
        # demanding a band and an AP that cannot both be satisfied,
        # which stops it associating at all. The lock setter holds
        # this too, because it waits for an association across an
        # await and this is the write that would race it.
        # Nor during a backend switch, which clears this pin before a
        # switch to iwd, for the reason _apply_band gives.
        if self._profile_change_in_flight() or self._backend_switch_running():
            return
        if self._reassert_exhausted("bssid_repoint"):
            return
        retarget = self._nmcli_modify(
            uuid, "802-11-wireless.bssid", action["value"], timeout=2
        )
        self._record_reassert("bssid_repoint", retarget["success"])
        if retarget["success"]:
            pending["bssid_lock_value"] = action["value"]
            known = list(settings.get("bssid_lock_uuids", []))
            # Keep the profile we are moving AWAY from. It still
            # carries the address we wrote, and overwriting the single
            # slot below is the only record of it - drop it here and
            # nothing can ever clear that profile again.
            previous = settings.get("bssid_lock_connection_uuid", "")
            if previous and previous != uuid and previous not in known:
                known.append(previous)
            pending["bssid_lock_connection_uuid"] = uuid
            pending["pins_owned"] = _with_pin(settings, "802-11-wireless.bssid")
            if uuid not in known:
                known.append(uuid)
            pending["bssid_lock_uuids"] = self._capped(known, "802-11-wireless.bssid")
            status["live"]["bssid_lock"] = action["value"]
            status["drift"].pop("bssid_lock", None)
            self._log_throttled(
                "bssid_repoint_ok",
                f"BSSID lock re-pointed to active profile {uuid} "
                f"at {action['value']}",
            )
        else:
            self._log_throttled(
                "bssid_repoint",
                f"Couldn't re-point BSSID lock to {uuid}: "
                f"{_tool_text(retarget.get('stderr', '')[:120])}",
            )

    _LOG_THROTTLE_SECONDS = 60

    def _log_throttled(self, key: str, message: str):
        """Log, but not on every poll.

        Drift reassertion runs every few seconds for as long as the drift
        lasts. When it keeps failing - a profile NM will not let us modify,
        say - logging each attempt buries the plugin log in tens of thousands
        of identical lines a day, in exactly the situation where someone needs
        to read it. Identical messages are collapsed; a changed message always
        gets through so a transition is never hidden.
        """
        seen = getattr(self, "_log_throttle_state", None)
        if seen is None:
            seen = {}
            self._log_throttle_state = seen
        now = time.monotonic()
        last_message, last_at = seen.get(key, (None, 0.0))
        if message == last_message and (now - last_at) < self._LOG_THROTTLE_SECONDS:
            return
        seen[key] = (message, now)
        decky.logger.info(message)

    def _reassert_exhausted(self, key: str) -> bool:
        """Whether this reassertion has given up for now.

        Giving up permanently would be wrong: whatever made the profile
        unmodifiable - NetworkManager restarting, a network change, a module
        finally loading - can go away, and nothing else would ever retry. So
        the limit expires, and one attempt is allowed through afterwards.
        """
        state = getattr(self, "_reassert_failures", None)
        if not state:
            return False
        count, last_at = state.get(key, (0, 0.0))
        if count < self._MAX_REASSERT_FAILURES:
            return False
        return (time.monotonic() - last_at) < self._REASSERT_COOLDOWN_SECONDS

    def _record_reassert(self, key: str, succeeded: bool):
        state = getattr(self, "_reassert_failures", None)
        if state is None:
            state = {}
            self._reassert_failures = state
        if succeeded:
            state[key] = (0, 0.0)
        else:
            count, _ = state.get(key, (0, 0.0))
            state[key] = (count + 1, time.monotonic())

    # (settings flag, the uuids we wrote it to, the property, what "off" means)
    #
    # The last field is not decoration. Clearing a BSSID or a band means
    # writing nothing and letting NetworkManager decide, but IPv6 has no such
    # empty state - "not disabled by us" is the method NM would have used
    # anyway, so turning the toggle off has to name it.
    _PIN_PROPERTIES = (
        ("bssid_lock_enabled", "bssid_lock_uuids", "802-11-wireless.bssid", ""),
        ("band_preference_enabled", "band_preference_uuids", "802-11-wireless.band", ""),
        ("ipv6_disabled", "ipv6_uuids", "ipv6.method", "auto"),
    )

    # The pins nobody sets by hand, so a stray one is ours by elimination and
    # clearing it is safe. IPv6 is deliberately absent: switching it off is an
    # ordinary thing to have done deliberately somewhere else, so a profile we
    # have no record of writing is reported rather than overridden.
    _ELIMINATION_PINS = ("802-11-wireless.band", "802-11-wireless.bssid")

    # How long the device must be CONTINUOUSLY offline before a pin we wrote
    # is treated as the reason it is offline. Comfortably longer than a radio
    # cycle and the association budget, so an ordinary reconnect - or the lock
    # setter's own deliberate cycle - never trips it.
    _STRANDED_SECONDS = 60

    # How long the panel keeps explaining a release after it happens. Without
    # this the notice would clear the instant WiFi returned, which is exactly
    # when the user is there to read it.
    _RELEASE_NOTICE_SECONDS = 600

    # How often the background watch looks at the connection while one of our
    # pins is on. One nmcli call each time; nothing more runs unless offline.
    _STRANDED_WATCH_SECONDS = 20

    # How many recently pinned profiles per kind the release looks at.
    _STRANDED_CANDIDATES = 6

    async def _stranded_watch(self):
        """Run the stranded-pin check whether or not the panel is open.

        The release above only happened inside a status poll, and the panel
        is the only thing that polls. The device it exists for is one that
        woke up unable to reach the pinned access point, where SteamOS asks
        for the password and nobody thinks to open a WiFi plugin. So it
        never ran for the people it was written for.
        """
        while True:
            await asyncio.sleep(self._STRANDED_WATCH_SECONDS)
            try:
                await self._wake_lock_check()
            except Exception as e:
                decky.logger.error(f"lock after wake error: {e}")
            try:
                await self._stranded_watch_once()
            except Exception as e:
                decky.logger.error(f"stranded watch error: {e}")
            await self._send_pending_report_if_due()

    # A suspend shows as CLOCK_BOOTTIME pulling ahead of CLOCK_MONOTONIC
    # (only the first counts time asleep). More than this between two
    # passes of the watch is a sleep and a wake.
    _WAKE_JUMP_SECONDS = 5
    # How long after a wake the lock waits for NetworkManager to reconnect
    # before it gives that wake up.
    _WAKE_CONNECT_WAIT = 90

    @staticmethod
    def _suspend_offset() -> float | None:
        """CLOCK_BOOTTIME minus CLOCK_MONOTONIC: the time spent suspended
        since boot. None where there is no CLOCK_BOOTTIME (not Linux)."""
        boottime = getattr(time, "CLOCK_BOOTTIME", None)
        if boottime is None:
            return None
        try:
            return time.clock_gettime(boottime) - time.monotonic()
        except (OSError, AttributeError):
            return None

    async def _wake_lock_check(self):
        """Once per wake, move the access point lock to a clearly stronger
        access point of the same network, without the panel open.

        The lock never moves on its own otherwise (no surprise reconnect in
        the middle of a stream), and the notice that offers a move only runs
        while the panel polls. At a wake the device has just reconnected
        anyway and has most likely been carried somewhere else: measured on
        a Deck OLED, NetworkManager rejoined the pinned access point at -84
        dBm after a wake while another of the same network read 100. One
        attempt per wake, once NetworkManager is connected again (given up
        after _WAKE_CONNECT_WAIT seconds).
        """
        offset = self._suspend_offset()
        if offset is None:
            return
        last = getattr(self, "_suspend_offset_seen", None)
        self._suspend_offset_seen = offset
        if last is not None and offset - last > self._WAKE_JUMP_SECONDS:
            self._wake_lock_pending = time.monotonic()
        pending = getattr(self, "_wake_lock_pending", None)
        if pending is None:
            return
        if not _load_settings().get("bssid_lock_enabled"):
            self._wake_lock_pending = None
            return
        state, _radio = await asyncio.to_thread(self._nm_general_state)
        if (state or "").startswith("connected") and await self._move_lock_after_wake():
            self._wake_lock_pending = None
        elif time.monotonic() - pending > self._WAKE_CONNECT_WAIT:
            self._wake_lock_pending = None
            decky.logger.info(
                "After waking, the BSSID lock was left as it is: "
                + ("WiFi did not reconnect" if not (state or "").startswith("connected")
                   else "it could not be told whether a game or stream is running"))

    async def _move_lock_after_wake(self) -> bool:
        """_wake_lock_check's one attempt: the same path as Move the lock
        (_lock_onto_best with moving=True: fresh scans, the same margin,
        verify and roll back, the failure cooldown), only while the lock
        holds on a weak link, and never during a game, a stream, an update,
        a profile change or a backend switch. One log line says what
        happened or why not. False, with nothing done, while it cannot yet
        be told whether a game or stream is running: tried again at the
        next pass of the watch."""
        def leave(why: str) -> bool:
            decky.logger.info(f"After waking, the BSSID lock was left as it is: {why}")
            return True

        if self._profile_change_refused():
            return leave("another WiFi change, an update or a backend switch is under way")
        settings = _load_settings()
        playing = await asyncio.to_thread(self._game_or_stream_known, settings)
        if playing is None:
            return False
        if playing:
            return leave("a game or stream is running")
        # A profile change of its own, as Move the lock is: Reset and the
        # other profile setters wait, and the status poll adopts nothing.
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        try:
            if await asyncio.to_thread(self._backend_is_iwd):
                return leave("the lock needs the wpa_supplicant backend")
            held, why = await asyncio.to_thread(self._wake_lock_link)
            if why:
                return leave(why)
            iface, uuid = held
            result = await self._lock_onto_best(iface, uuid, moving=True, after_wake=True)
            if not result.get("success") and result.get("error") not in ("no_better_ap",):
                leave(f"the move did not go through ({result.get('error', 'unexpected')})")
            return True
        finally:
            self._forget_lock_weaker()
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    def _game_or_stream_known(self, settings: dict) -> bool | None:
        """Whether a game or stream is running, from a process search no
        older than PROC_SCAN_FRESH_SECONDS, or None when there is none to go
        by. In a worker thread: it waits up to PROC_SCAN_WAIT_SECONDS for a
        search started now. Never "not running" for want of an answer: the
        plain search answers from whatever it last had, and a move made on
        that could reconnect in the middle of a game."""
        if os.path.isfile(STREAM_FLAG_FILE) or os.path.isfile(STREAM_HOLD_FILE):
            return True
        self._proc_snapshot(PROC_SCAN_WAIT_SECONDS)
        _PROC_SCAN_LOCAL.no_wait = True
        _PROC_SCAN_LOCAL.unknown = False
        try:
            found = (self._find_stream([("game", "steamlaunch appid=")])
                     or self._find_stream(self._stream_patterns(settings)))
        finally:
            _PROC_SCAN_LOCAL.no_wait = False
        if found is not None:
            return True
        return None if _PROC_SCAN_LOCAL.unknown else False

    def _wake_lock_link(self) -> tuple:
        """((interface, profile), None) when the lock is holding on a link
        that is not strong: the profile in use is the one locked, its pin is
        the recorded access point and the device is on it. (None, why) when
        not. In a worker thread: nmcli and iw."""
        settings = _load_settings()
        value = settings.get("bssid_lock_value", "").upper()
        uuid = self._get_active_connection_uuid()
        if not uuid or uuid != settings.get("bssid_lock_connection_uuid", ""):
            return None, "the profile in use is not the locked one"
        iface = self._get_wifi_interface()
        if not iface or not IFACE_RE.match(iface):
            return None, "no WiFi interface"
        pinned = self._read_profile_field(uuid, "802-11-wireless.bssid").replace("\\", "")
        if not value or pinned.upper() != value:
            return None, "the profile's pin is not the recorded one"
        link = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
        if not link["success"]:
            return None, "the link could not be read"
        out = link.get("stdout", "")
        if self._parse_link(out).get("connected_bssid", "").upper() != value:
            return None, "the device is not on the locked access point"
        if self._link_is_strong(out):
            return None, "the link is strong"
        return (iface, uuid), None

    async def _stranded_watch_once(self):
        await self._unpin_if_iwd()
        settings = _load_settings()
        if not any(
            settings.get(enabled_key)
            for enabled_key, _lk, prop, _off in self._PIN_PROPERTIES
            if prop in self._ELIMINATION_PINS
        ):
            return
        state, radio = await asyncio.to_thread(self._nm_general_state)
        if state is None:
            return
        # Online in any form means no pin of ours is keeping it off. So does
        # asleep, or the radio switched off: that is the system's or the
        # user's doing, and the offline clock must not carry a stretch of it
        # into the next real outage. Resetting here matters, because nothing
        # else does while the panel is closed.
        if (state.startswith("connected") or state == "asleep"
                or radio != "enabled"):
            self._offline_since = None
            # Back online after we released a pin: one poll, so the setting
            # that is still on settles on the access point that works now.
            if state.startswith("connected") and getattr(
                self, "_repoll_after_release", False
            ):
                self._repoll_after_release = False
                await self.get_status()
            return
        # A full poll, exactly as the panel would make, so the release goes
        # through the same proposal, the same guards and the same notice.
        await self.get_status()

    def _propose_pin_cleanup(self, settings: dict) -> list[dict]:
        """One cleanup per property per poll for anything we could not unpin.

        Both properties need this, not just the address: a profile left
        demanding a band its network does not offer fails to associate the
        same way, and once the preference is off nothing else would look at
        it again.
        """
        out = []
        for enabled_key, list_key, prop, off_value in self._PIN_PROPERTIES:
            if settings.get(enabled_key):
                continue
            for stale_uuid in settings.get(list_key, [])[:1]:
                out.append({
                    "kind": "pin_cleanup", "uuid": stale_uuid,
                    "property": prop, "list_key": list_key,
                    "off_value": off_value,
                })
        # Custom DNS off with profiles still listed: ones Reset, uninstall or
        # the toggle could not give their automatic DNS back.
        if not settings.get("dns_enabled"):
            for stale_uuid in settings.get("dns_uuids", [])[:1]:
                out.append({"kind": "dns_cleanup", "uuid": stale_uuid})
        # And profiles Reset or uninstall could not put back to WPA3 only.
        for stale_uuid in settings.get("wpa2_restore_uuids", [])[:1]:
            out.append({"kind": "wpa2_restore", "uuid": stale_uuid})
        return out

    def _propose_active_pin_cleanup(
        self, settings: dict, uuid: str, list_key: str, live_value: str
    ) -> list[dict]:
        """Clear a pin left on the ACTIVE profile that nothing is tracking.

        _propose_pin_cleanup only walks the uuids we still remember, so a pin
        that outlived its tracking list is invisible to it - and invisible in
        the panel, which shows the preference as off. Nothing would ever look
        at that profile again, while NetworkManager still honours what is
        written there.

        Measured on a Deck: an active profile carrying 802-11-wireless.band=a
        while the plugin reported the band preference off and tracked no
        profiles at all. That restricts the device to 5 GHz with no way to
        see it, let alone undo it, from the panel that caused it.

        The active uuid is the one profile we can always check, and the value
        has already been read for drift, so this costs no extra subprocess.
        """
        enabled_key, _, prop, off_value = next(
            e for e in self._PIN_PROPERTIES if e[1] == list_key
        )
        if settings.get(enabled_key) or not live_value or not uuid:
            return []
        # Never written by us on this install, so it was set by hand.
        if prop not in settings.get("pins_owned", []):
            return []
        # Already queued by the tracked-list walk; doing it twice would just
        # spend the same cooldown on the same profile.
        if uuid in settings.get(list_key, []):
            return []
        return [{
            "kind": "pin_cleanup", "uuid": uuid,
            "property": prop, "list_key": list_key,
            "off_value": off_value,
        }]

    def _profile_pins(self, uuid: str, timeout: int = 3) -> dict:
        """The elimination-safe pins currently written to one profile.

        Both fields in one call, because this runs per rival profile and the
        point of reading them together is to not spend a subprocess each.
        """
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", ",".join(self._ELIMINATION_PINS),
             "con", "show", "uuid", uuid],
            timeout=timeout,
        )
        if not result["success"]:
            return {}
        out = {}
        for line in result.get("stdout", "").split("\n"):
            name, sep, value = line.partition(":")
            if not sep:
                continue
            # A BSSID arrives with its colons escaped, the same way the
            # active profile's value is read for drift.
            value = value.replace("\\", "").strip()
            if value:
                out[name.strip()] = value
        return out

    def _propose_rival_pin_cleanup(self, settings: dict) -> list[dict]:
        """Clear our pins from the OTHER saved copies of the network in use.

        NetworkManager keeps several profiles per SSID and is free to
        autoconnect with any of them. A pin left on a copy we are not
        currently using is invisible twice over - the panel reports the
        feature as off, and the profile it was written to is not the one
        being read - yet NetworkManager still honours it the moment that copy
        is chosen. Nothing else here would ever look at it again.

        Bounded to copies of the ACTIVE network on purpose. A pin on some
        other network is inert until the device goes there, whereas a copy
        that can be picked INSTEAD of us is doing harm now. It is also the
        only set already gathered: the rivals come from the throttled
        duplicate check, so this costs no extra subprocess of its own.

        Only the elimination-safe properties. Returning IPv6 to auto on a
        profile we cannot prove we wrote would override a choice made
        elsewhere, which is the distinction that decided the active-profile
        case too.
        """
        out = []
        cached = getattr(self, "_rival_pins", None)
        if not cached:
            return out
        for enabled_key, list_key, prop, off_value in self._PIN_PROPERTIES:
            if prop not in self._ELIMINATION_PINS:
                continue
            if settings.get(enabled_key):
                continue
            if prop not in settings.get("pins_owned", []):
                continue
            for rival_uuid, pins in cached.items():
                if not pins.get(prop):
                    continue
                # Already queued by the tracked-list walk; doing it twice
                # would spend the same cooldown on the same profile.
                if rival_uuid in settings.get(list_key, []):
                    continue
                # Optimistic: the next duplicate check re-reads the profile,
                # so a write that failed comes back rather than being
                # retried every poll from a cache that cannot learn.
                pins[prop] = ""
                out.append({
                    "kind": "pin_cleanup", "uuid": rival_uuid,
                    "property": prop, "list_key": list_key,
                    "off_value": off_value,
                })
                break
        return out

    def _propose_stranded_pin_release(
        self, settings: dict, connected: bool
    ) -> list[dict]:
        """Release a pin we wrote once it is the likely reason we are offline.

        `_propose_pin_cleanup` already runs before the not-connected return,
        for exactly this reason - but it skips any property whose feature is
        still ENABLED, which is precisely when the pin exists. So the one
        state it cannot help with is the one that actually strands a device:
        the lock is on, the access point it names is no longer there, and
        nothing reconsiders it. The user is left re-entering credentials,
        which makes a SECOND copy of the network and orphans the pin on the
        first.

        A pin is a preference, not a requirement. Being offline because of
        our own preference is strictly worse than being online without it,
        and clearing one only ever WIDENS what NetworkManager may associate
        to, so this cannot cost reachability. The feature stays on, so the
        ordinary path re-applies a reachable value on the next connection.

        But it must be the pin. Offline with the radio off, the router
        rebooting or the network out of range is not something a pin did,
        and releasing then re-pointed the lock to whatever access point came
        back first. So a pin is released only when this network IS in range
        on an access point the pin rules out.

        Bounded to profiles we recorded writing, and to the pins nothing else
        sets - the same boundary that decided the active and rival cases.
        """
        if not any(
            settings.get(enabled_key)
            for enabled_key, _lk, prop, _off in self._PIN_PROPERTIES
            if prop in self._ELIMINATION_PINS
        ):
            self._offline_since = None
            return []
        # Read NetworkManager's own verdict rather than trusting an active
        # profile. A profile waiting on a password is ACTIVATING, and that is
        # exactly the state a pin that cannot associate leaves it in, so
        # counting it as online reset the clock for as long as the prompt sat
        # there. One call also says whether the radio is on.
        state, radio = self._nm_general_state()
        if connected and (state is None or state.startswith("connected")):
            self._offline_since = None
            return []
        if (state is None or state == "asleep" or radio != "enabled"
                or self._profile_change_in_flight()
                or self._system_change_in_flight()):
            # None of these is a pin's doing, and a stretch of one must not
            # be carried into the next real outage either.
            self._offline_since = None
            return []
        now = time.monotonic()
        if getattr(self, "_offline_since", None) is None:
            self._offline_since = now
            return []
        if now - self._offline_since < self._STRANDED_SECONDS:
            return []
        visible = self._visible_networks()
        if not visible:
            return []
        out = []
        for uuid, props in self._stranded_candidates(settings).items():
            live = self._profile_pins(uuid)
            if not live:
                continue
            ssid = (self._get_profile_ssid(uuid, timeout=3) or "").replace("\\", "")
            aps = visible.get(ssid)
            if not aps:
                continue
            for enabled_key, list_key, prop, off_value in self._PIN_PROPERTIES:
                if prop not in props or not live.get(prop):
                    continue
                if not self._pin_rules_out(prop, live[prop], aps):
                    continue
                out.append({
                    "kind": "pin_cleanup", "uuid": uuid,
                    "property": prop, "list_key": list_key,
                    "off_value": off_value, "stranded": True,
                })
        return out

    def _nm_general_state(self) -> tuple:
        """(state, wifi radio) from NetworkManager, or (None, None) unread."""
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "STATE,WIFI", "general"], timeout=3
        )
        if not result["success"]:
            return None, None
        fields = self._nmcli_fields(result.get("stdout", "").strip())
        if len(fields) < 2:
            return None, None
        return fields[0].strip(), fields[1].strip()

    def _system_change_in_flight(self) -> bool:
        """A backend switch or an update is taking WiFi down on purpose."""
        switch = getattr(self, "_backend_switch", None) or {}
        return bool(switch.get("in_progress")) or self._update_running()

    def _stranded_candidates(self, settings: dict) -> dict:
        """uuid -> the pin properties we may release on it.

        Every profile we recorded pinning, not just the last one in use: the
        profile a device fails to rejoin is often not the one it last held,
        and an upgrade arrives with only the lock's own profile recorded. The
        lock's and the last profile are added when this install has written
        that kind of pin, which is the evidence the tracking lists stand in for.
        """
        owned = settings.get("pins_owned", [])
        extra = [settings.get("bssid_lock_connection_uuid", ""),
                 settings.get("last_connection_uuid", "")]
        out: dict = {}
        for enabled_key, list_key, prop, _off in self._PIN_PROPERTIES:
            if prop not in self._ELIMINATION_PINS or not settings.get(enabled_key):
                continue
            # Each costs two nmcli calls while offline, so the most recent
            # few per kind - capped per kind, so a long lock list cannot
            # crowd out every band candidate.
            uuids = list(settings.get(list_key, []))[-self._STRANDED_CANDIDATES:]
            if prop in owned:
                uuids += extra
            for uuid in uuids:
                if uuid:
                    out.setdefault(uuid, set()).add(prop)
        return out

    def _visible_networks(self) -> dict:
        """SSID -> [(bssid, freq_mhz, signal)] from NetworkManager's cached scan.

        Cached on purpose: NetworkManager keeps scanning while disconnected,
        and forcing a fresh one from here would hold up the status poll.
        """
        scan = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,FREQ", "dev", "wifi",
             "list", "--rescan", "no"],
            timeout=5,
        )
        if not scan["success"]:
            return {}
        out: dict = {}
        for line in scan.get("stdout", "").split("\n"):
            parts = self._nmcli_fields(line)
            if len(parts) < 4 or not parts[0]:
                continue
            found = re.match(r"\s*(\d+)", parts[3])
            signal = re.match(r"\s*(\d+)", parts[2])
            out.setdefault(parts[0], []).append((
                parts[1].upper(),
                int(found.group(1)) if found else 0,
                int(signal.group(1)) if signal else 0,
            ))
        return out

    # How much stronger another access point must be before a visible pinned
    # one counts as the reason we cannot connect.
    _STRANDED_SIGNAL_MARGIN = 20

    def _pin_rules_out(self, prop: str, value: str, aps: list) -> bool:
        """Whether this pin is what keeps us off a network that is in range.

        Not merely whether it excludes some access point: a router lists its
        other band and a mesh its other nodes, so that is nearly always true
        and would release on any outage at all - a changed password, a DHCP
        failure, the user disconnecting. An address pin is the reason when
        its access point is gone, or far weaker than one it rules out; a band
        pin when nothing on that band is in range.
        """
        if prop == "802-11-wireless.bssid":
            pinned = value.replace("\\", "").upper()
            mine = [sig for bssid, _f, sig in aps if bssid == pinned]
            if not mine:
                return True
            best_other = max((sig for bssid, _f, sig in aps if bssid != pinned),
                             default=0)
            return best_other - max(mine) >= self._STRANDED_SIGNAL_MARGIN
        if prop == "802-11-wireless.band":
            known = [freq for _b, freq, _s in aps if freq]
            return bool(known) and not any(
                self._freq_band(freq) == value for freq in known
            )
        return False

    @staticmethod
    def _nmcli_fields(line: str) -> list[str]:
        """Split one `nmcli -t` line on its UNESCAPED colons, unescaping as it goes.

        Terse output escapes a colon inside a value as `\\:`, and both SSIDs and
        BSSIDs contain them - a BSSID is nothing but colons. Splitting on a bare
        ":" therefore shreds every MAC address into six fields.
        """
        out, cur, esc = [], "", False
        for ch in line:
            if esc:
                cur += ch
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == ":":
                out.append(cur)
                cur = ""
            else:
                cur += ch
        out.append(cur)
        return out

    # Signal at which the access point we are already on is simply good
    # enough, and looking for a better one is not worth a scan. Scanning takes
    # the radio off-channel briefly, which is exactly the interruption this
    # plugin exists to avoid, so it is not something to do speculatively while
    # someone is streaming. Anything at or above this is a healthy link by any
    # reading; the case this feature exists for sits far below it.
    _STRONG_ENOUGH_DBM = -60

    # Long enough for nmcli to refresh a stale scan cache rather than time out.
    _SCAN_SECONDS = 15

    # How many scans to average before deciding anything. Measured on a Deck:
    # ONE access point read 35, 39, 45 and 61 across consecutive scans, so a
    # single sample carries about as much noise as the difference the decision
    # turns on. Two samples is the cheapest thing that makes the comparison
    # mean something, and this only runs when the user asks for the lock and
    # the link is already weak, so the seconds are affordable.
    _SCAN_SAMPLES = 2

    # How long to leave an access point alone after it accepted a scan but
    # refused an association. MEASURED on a Deck: a mesh node visible at
    # signal 56 answered no authentication at all - wpa_supplicant reported
    # CONN_FAILED and NetworkManager gave up with "association took too long".
    # A loud beacon from a distant node says the radio can HEAR it, not that
    # it can be heard BACK, and nothing in a scan distinguishes the two. So
    # the only way to know is to try, and the only way not to keep paying for
    # it is to remember.
    #
    # Fifteen minutes: long enough that a user toggling the lock again does
    # not repeat the same failed reconnect, short enough that carrying the
    # device to another room forgets it soon after.
    _AP_FAILURE_COOLDOWN = 15 * 60
    _MAX_TRACKED_AP_FAILURES = 16

    # How much stronger another access point must be, on the AVERAGED signal,
    # before locking prefers it over the one we are on. nmcli reports signal
    # as 0-100 quality.
    #
    # This is deliberately smaller than it would need to be for a single scan.
    # Raw readings of one access point varied by about 26 points on a measured
    # Deck; averaging over _SCAN_SAMPLES scans removes most of that, and a
    # margin sized for the noise rather than the averaged signal would refuse
    # every real improvement. Measured case: 39 against 57 after averaging,
    # roughly eleven decibels, which a twenty-point margin declined to act on.
    #
    # Erring small is the right direction here. The move happens once, when
    # the user asks for the lock, so a needless one costs a single reconnect;
    # refusing a real one costs a worse connection for as long as the lock
    # stays on. Nothing moves the lock on its own afterwards, so there is
    # nothing to flap: the most that happens later is a notice offering a
    # move, which the user has to accept (see _LOCK_WEAKER_MARGIN).
    _AP_UPGRADE_MARGIN = 12

    # How much stronger another access point must be, on nmcli's 0-100
    # signal, before the panel says the lock is holding the device on a
    # weaker one. Larger than _AP_UPGRADE_MARGIN on purpose. That margin is
    # applied to an average of several fresh scans, once, at a moment the
    # user chose. This one is applied to a single cached scan, over and over
    # for as long as the lock is on, and a notice that comes and goes with
    # scan-to-scan drift (about 26 points for one access point on a measured
    # Deck) would nag about nothing. A miss here costs little: the lock still
    # works, and turning it off and on again moves it on the smaller margin.
    _LOCK_WEAKER_MARGIN = 20

    # How often that is looked at, and how long the stronger access point has
    # to stay ahead before it is mentioned. Both are about not reacting to
    # one scan: a cached scan is cheap to read but only as fresh as the last
    # time anything scanned, so the finding has to hold on two reads at
    # least this far apart.
    _LOCK_WEAKER_CHECK_SECONDS = 30

    # With the lock on, nothing refreshes nmcli's scan list. Measured on a
    # Deck (SteamOS 3.8.28, NetworkManager 1.52.1, wpa_supplicant): with the
    # lock off the cached list kept 6-7 entries over three minutes and their
    # signals changed between reads; with it on, every other access point
    # read exactly the same on ten reads 25 s apart, only the associated one
    # moved, and later the list had expired down to the pinned access point
    # alone. A notice judged on that compares the live signal of the pinned
    # access point with old numbers from where the lock was set, so it could
    # fire wrongly or never. So the notice asks for a scan itself, at most
    # this often: a scan takes the radio off-channel for a moment, which is
    # a short latency spike, and it is never asked for during a stream.
    _LOCK_SCAN_SECONDS = 60

    # How long after asking for a scan before the cached list can be trusted.
    # Measured on the same Deck: `nmcli dev wifi rescan` returned in 21 ms,
    # because it only requests the scan, and about 8 s later the list held
    # all seven access points with fresh signals.
    _LOCK_SCAN_SETTLE_SECONDS = 8

    def _visible_access_points(
        self, iface: str, ssid: str, rescan: bool = False, cached_only: bool = False
    ) -> list[tuple[str, int, int, str]]:
        """(bssid, signal, freq_mhz, security) for this network, best first.

        Ordered by signal, then by 5 GHz over 2.4 GHz where the signal is
        equal. That tie-break is deliberate: a mesh commonly advertises both
        bands of the same node at full strength, and this plugin exists for
        game streaming, where the quieter, faster band is the better of two
        otherwise equal choices.

        BSSIDs come back upper-case from nmcli and lower-case from iw, so they
        are normalised here and every comparison against them must be too.
        """
        if not ssid:
            return []
        # nmcli serves a cached list, but refreshes it itself when the cache
        # is stale - and a real scan takes several seconds. Measured on a Deck:
        # the same call answered in 0.02 s warm and timed out at five seconds
        # cold, which is exactly the state the lock runs in, because turning it
        # on straight after turning it off follows a radio cycle. A budget the
        # common path cannot meet turned "look for a better access point" into
        # "silently find none".
        scan = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "SSID,BSSID,SIGNAL,FREQ,SECURITY",
             "dev", "wifi", "list", "ifname", iface]
            + (["--rescan", "yes"] if rescan
               else ["--rescan", "no"] if cached_only else []),
            timeout=self._SCAN_SECONDS,
        )
        if not scan["success"]:
            # Saying nothing here is how this failed invisibly: no candidates
            # looks identical to no better access point, and the lock pins
            # where it stands with nothing to explain why.
            decky.logger.error(
                f"Could not list access points on {iface}: "
                f"{_tool_text((scan.get('stderr', '') or 'timed out')[:160])}"
            )
            return []
        want = ssid.replace("\\", "")
        found: list[tuple[str, int, int, str]] = []
        for line in scan.get("stdout", "").split("\n"):
            parts = self._nmcli_fields(line)
            if len(parts) < 5:
                continue
            name, bssid, signal, freq, security = parts[:5]
            if name != want or not bssid:
                continue
            found_freq = re.match(r"\s*(\d+)", freq.strip())
            try:
                found.append((
                    bssid.upper(),
                    int(signal),
                    int(found_freq.group(1)) if found_freq else 0,
                    security.strip().upper(),
                ))
            except ValueError:
                continue
        found.sort(key=lambda ap: (ap[1], ap[2] >= 5000), reverse=True)
        return found

    @staticmethod
    def _ap_accepts(security: str, key_mgmt: str) -> bool:
        """Whether an access point can accept a profile's key management.

        One SSID does not mean one set of credentials. A network can be
        served by a modern access point offering WPA3 alongside an older one
        that only speaks WPA2, and a profile saved as WPA3-only cannot
        associate with the latter no matter how strong its signal is.

        Measured: a device sat on a mesh node at signal 61 while the router
        advertised the same SSID at 100 on two radios, WPA2 only, against a
        key-mgmt of sae. Ranking by signal alone chose an access point it
        could never join.

        Unknown or unreadable key management returns True rather than
        filtering everything out, which keeps an unfamiliar setup working the
        way it did before.
        """
        sec, km = security.upper(), (key_mgmt or "").strip().lower()
        if km == "sae":
            return "WPA3" in sec
        if km.startswith("wpa-psk"):
            return "WPA1" in sec or "WPA2" in sec
        if km == "owe":
            return "OWE" in sec
        if km.startswith("wpa-eap"):
            return "802.1X" in sec
        if km == "none":
            return sec == ""
        return True

    def _profile_key_mgmt(self, uuid: str) -> str:
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt",
             "con", "show", "uuid", uuid],
            timeout=5,
        )
        if not result["success"]:
            return ""
        _, sep, value = result.get("stdout", "").partition(":")
        return value.strip() if sep else ""

    async def _await_association(self, iface: str) -> dict:
        """Poll iw until the link reports an access point, or the budget runs out.

        Returns {"iface", "bssid", "link", "error"}. The interface comes back
        because a radio cycle can rename it, and the caller must go on using
        the name that answered rather than the one it started with.

        This awaits rather than blocking: half a minute of blocked event loop
        would freeze the whole panel. Callers must hold the reconciliation
        guard across it, since a status poll can now run in the middle.
        """
        deadline = time.monotonic() + self._ASSOCIATION_WAIT_SECONDS
        link_out = ""
        bssid = ""
        read_error = ""
        while True:
            # Off the event loop, as each read can take its full timeout
            # against a slow driver, every half second for the whole wait.
            link_result = await asyncio.to_thread(
                self._run_cmd, ["/usr/bin/iw", "dev", iface, "link"]
            )
            if link_result["success"]:
                read_error = ""
                link_out = link_result.get("stdout", "")
                for line in link_out.split("\n"):
                    if "Connected to" in line:
                        parts = line.split()
                        if len(parts) >= 3:
                            bssid = parts[2]
                        break
            else:
                # A command that FAILED is not an interface that has not
                # associated yet. Reporting it as "still reconnecting" sends
                # the user off to wait for something that is never going to
                # arrive, and buries the real error - which is how a missing
                # binary or a wedged iw presented as a BSSID that could not
                # be read.
                read_error = (
                    link_result.get("stderr", "")
                    or f"iw exited {link_result.get('returncode')}"
                )
                # A missing binary is the one failure that cannot heal, so
                # waiting out the full budget only delays the report.
                # Everything else can: a radio cycle can take the interface
                # away underneath us, and the re-resolve below recovers.
                if "Command not found" in read_error:
                    break
                # Nor does an iw stuck in the driver within the wait: every
                # read after it is refused at once, sixty times over.
                if read_error.endswith("is still stuck"):
                    break
            if bssid or time.monotonic() >= deadline:
                break
            iface = await asyncio.to_thread(self._get_wifi_interface) or iface
            await asyncio.sleep(0.5)
        return {
            "iface": iface, "bssid": bssid,
            "link": link_out, "error": read_error,
        }

    def _link_is_strong(self, link: str) -> bool:
        """Whether the link we already have is good enough to leave alone.

        Read from the iw output the caller already has, so deciding not to
        scan costs nothing. An unreadable or absent signal line returns False
        and we fall through to looking properly, which is the safe direction.
        """
        found = re.search(r"^\s*signal:\s*(-?\d+)", link, re.MULTILINE)
        if not found:
            return False
        return int(found.group(1)) >= self._STRONG_ENOUGH_DBM

    # Looking for duplicate saved copies of a network costs a few nmcli calls,
    # so it is not worth doing on every status poll.
    _DUPLICATE_CHECK_SECONDS = 300

    def _rival_profiles(self, uuid: str, ssid: str) -> list[tuple[str, str]]:
        """Other saved profiles for the same network: (uuid, key_mgmt).

        NetworkManager will happily keep several profiles for one SSID, and
        with equal autoconnect priority it has no basis to prefer any of them.
        When they differ in security that is not a tidiness problem: each can
        reach a DIFFERENT SET of access points, so which one NM happens to
        pick decides whether the strong access point is even a candidate. To
        the user that looks like connecting to the good one and then falling
        back, and like being erratic in one room and fine everywhere else.

        Listed by NAME first, which costs one call, because duplicates of one
        network almost always share it. The SSID is then confirmed per
        candidate rather than trusted, since a name is only a label.
        """
        listing = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "UUID,NAME,TYPE", "con", "show"],
            timeout=5,
        )
        if not listing["success"]:
            return []
        mine = ""
        rows = []
        for line in listing.get("stdout", "").split("\n"):
            parts = self._nmcli_fields(line)
            if len(parts) < 3 or parts[2] != "802-11-wireless":
                continue
            rows.append((parts[0], parts[1]))
            if parts[0] == uuid:
                mine = parts[1]
        out = []
        for other, name in rows:
            if other == uuid or name != mine:
                continue
            if self._get_profile_ssid(other, timeout=3) != ssid:
                continue
            out.append((other, self._profile_key_mgmt(other)))
        return out

    # NetworkManager's ceiling for connection.autoconnect-priority.
    _PRIORITY_MAX = 999
    # What a network with a single saved copy gets. Keeping the historical
    # number means existing installs are not rewritten for no reason.
    _PRIORITY_BASE = 100

    def _profile_priority(self, uuid: str, timeout: int = 3) -> int:
        """A profile's autoconnect priority, or 0 if it cannot be read."""
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "connection.autoconnect-priority",
             "con", "show", "uuid", uuid],
            timeout=timeout,
        )
        if not result["success"]:
            return 0
        _, sep, value = result.get("stdout", "").partition(":")
        if not sep:
            return 0
        try:
            return int(value.strip())
        except ValueError:
            return 0

    def _precedence_priority(
        self, uuid: str, rivals: list | None = None
    ) -> int | None:
        """The priority this profile needs in order to actually outrank its
        duplicates, or None when nothing needs writing.

        Writing a FIXED number on whichever profile is active looks like it
        establishes precedence and does the opposite. NetworkManager keeps
        several profiles per SSID and each one is the active profile at some
        point, so each in turn gets the same number - and the result is the
        tie we started with, resolved on last-used timestamp, which flips
        whenever the other copy connects. That is the whole reason one network
        can behave differently from one hour to the next.

        Only ever raises OUR profile; a rival is read but never written, so
        this cannot disturb a value somebody else set deliberately. With no
        rivals it only moves a profile off the default of 0. It also
        converges: once this profile outranks the others NetworkManager stops
        switching, so nothing bumps again.
        """
        mine = self._profile_priority(uuid)
        if rivals is None:
            ssid = self._get_profile_ssid(uuid)
            rivals = self._rival_profiles(uuid, ssid) if ssid else []
        if not rivals:
            # Only a profile still on NetworkManager's default of 0. Writing
            # the base over any other value lowered a priority someone had
            # raised on purpose, or raised one they had lowered (a metered
            # hotspot set below home), just from opening the panel there.
            return self._PRIORITY_BASE if mine == 0 else None
        highest = max(self._profile_priority(other) for other, _km in rivals)
        if mine > highest:
            return None
        return min(highest + 1, self._PRIORITY_MAX)

    @staticmethod
    def _parse_reg(text: str) -> dict:
        """Split `iw reg get` into the global domain and each phy's own.

        The global block is printed FIRST and is the one people read, but a
        SELF-MANAGED phy carries its own regulatory domain and the global
        entry does not govern it. On a Steam Deck OLED the global block says
        `country 00: DFS-UNSET` with every band PASSIVE-SCAN, while the phy
        sits at `country US: DFS-FCC` with the full 5925-7125 range - so
        anyone reading the raw output reasonably concludes their 6 GHz is
        crippled when it is not. That misreading is a support-noise
        generator, and it is the reason this parser exists rather than the
        raw text being shown.
        """
        out = {"global": "", "phys": [], "self_managed": False, "governing": ""}
        section = None
        for raw in (text or "").split("\n"):
            line = raw.strip()
            if line.startswith("global"):
                section = {"phy": "", "self_managed": False, "country": ""}
                out["_global_section"] = section
                continue
            if line.startswith("phy#"):
                name = line.split()[0].replace("#", "")
                section = {
                    "phy": name,
                    "self_managed": "self-managed" in line,
                    "country": "",
                }
                out["phys"].append(section)
                continue
            if line.startswith("country") and section is not None and not section["country"]:
                # "country US: DFS-FCC" -> "US"
                rest = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
                section["country"] = rest.split(":", 1)[0].strip()
        g = out.pop("_global_section", None)
        out["global"] = (g or {}).get("country", "")
        managed = [p for p in out["phys"] if p["self_managed"] and p["country"]]
        out["self_managed"] = bool(managed)
        # A phy listed with a country of its own, though not self-managed, is
        # one whose driver registered the region it was built for (rtw88
        # reads one from the chip): it keeps that whatever the global domain
        # says, and the global domain follows the user anyway.
        own = [p for p in out["phys"] if not p["self_managed"] and p["country"]]
        # What actually governs this radio: the self-managed phy when there is
        # one, then a phy with its own country, otherwise the global domain.
        out["governing"] = (managed[0]["country"] if managed
                            else own[0]["country"] if own else out["global"])
        return out

    @staticmethod
    def _region_words(code: str, country: str = "") -> str:
        """A region code as the panel says it. cfg80211's own codes are not
        countries: 00 is the world default, 98 the overlap of two regions
        (the card's own and the one asked for) and 99 the card's own."""
        if code == "00":
            return "the world default region"
        if code == "98":
            return (f"a mix of its own region and {country}" if country
                    else "a mix of its own region and the one chosen")
        if code == "99":
            return "its own region"
        return f"its own region ({code})" if code else "its own region"

    def _regdomain_info(self) -> dict:
        """The regulatory picture, already interpreted.

        `changeable` is the field that matters to the UI: on a self-managed
        phy setting a region is COSMETIC, and saying so is the difference
        between a feature that does nothing and a feature that explains why.
        """
        result = self._run_cmd(["/usr/bin/iw", "reg", "get"], timeout=3)
        parsed = self._parse_reg(result.get("stdout", "") if result["success"] else "")
        parsed["readable"] = bool(result["success"])
        parsed["changeable"] = bool(result["success"]) and not parsed["self_managed"]
        return parsed

    def _recent_ap_failures(self, settings: dict) -> set:
        """Access points that refused an association recently.

        The stored value comes from a file the desktop user owns, so both the
        address and the timestamp are validated rather than trusted; anything
        malformed is simply not a reason to skip an access point.
        """
        now = time.time()
        out = set()
        for bssid, when in (settings.get("ap_move_failures") or {}).items():
            if not isinstance(bssid, str) or not BSSID_RE.match(bssid):
                continue
            if not isinstance(when, (int, float)):
                continue
            if 0 < now - when < self._AP_FAILURE_COOLDOWN:
                out.add(bssid.upper())
        return out

    def _record_ap_failure(self, bssid: str):
        """Remember that this access point would not accept us."""
        if not bssid or not BSSID_RE.match(bssid):
            return
        settings = _load_settings()
        failures = dict(settings.get("ap_move_failures") or {})
        failures[bssid.upper()] = int(time.time())
        # Keep the newest few. Unbounded growth in a file parsed on every
        # status poll is its own problem.
        if len(failures) > self._MAX_TRACKED_AP_FAILURES:
            failures = dict(
                sorted(failures.items(), key=lambda kv: kv[1], reverse=True)
                [: self._MAX_TRACKED_AP_FAILURES]
            )
        settings["ap_move_failures"] = failures
        # Best effort. It is called just before a failed lock move is rolled
        # back, and a save that raised skipped the rollback, leaving the
        # profile pinned to the access point that had just refused us.
        try:
            _save_settings_with_timestamp(settings)
        except OSError as e:
            self._log_throttled("ap_failure_save", f"Could not record the access point failure: {e}")

    def _sampled_access_points(
        self, iface: str, ssid: str
    ) -> list[tuple[str, int, int, str]]:
        """Access points with their signal averaged over several scans.

        Signal from a single scan is too noisy to decide on - see
        _SCAN_SAMPLES. Each sample forces a real scan rather than accepting
        whatever nmcli last cached, because two reads of one cache are one
        sample wearing two hats.

        An access point missing from a sample is not counted in its own
        average; it stays a candidate on the evidence that exists.
        """
        totals: dict[str, list[int]] = {}
        facts: dict[str, tuple[int, str]] = {}
        for _ in range(max(1, self._SCAN_SAMPLES)):
            for bssid, signal, freq, security in self._visible_access_points(
                iface, ssid, rescan=True
            ):
                totals.setdefault(bssid, []).append(signal)
                facts.setdefault(bssid, (freq, security))
        averaged = [
            (bssid, round(sum(seen) / len(seen)), facts[bssid][0], facts[bssid][1])
            for bssid, seen in totals.items() if seen
        ]
        averaged.sort(key=lambda ap: (ap[1], ap[2] >= 5000), reverse=True)
        return averaged

    def _lock_candidates(
        self, aps: list, ssid: str, settings: dict, key_mgmt: str
    ) -> list[tuple[str, int, int, str]]:
        """The access points of this network the lock could pin this profile to.

        Shared by choosing where to lock and by noticing, later, that the
        lock is holding the device somewhere much weaker. If the two used
        different rules, the panel would offer a move the lock then declines,
        or point at an access point it can never join.

        An access point that cannot accept this profile's credentials is not
        a candidate however strong it is. A band preference, when set, is a
        constraint and not a suggestion: choosing an access point on the other
        band would write a profile whose band and address contradict, which is
        the one state that stops it associating at all. And an access point
        that refused us recently is left alone until its cooldown runs out.
        """
        if key_mgmt:
            aps = [ap for ap in aps if self._ap_accepts(ap[3], key_mgmt)]
        aps = self._on_preferred_band(aps, ssid, settings)
        refused = self._recent_ap_failures(settings)
        if refused:
            aps = [ap for ap in aps if ap[0] not in refused]
        return aps

    def _on_preferred_band(self, aps: list, ssid: str, settings: dict) -> list:
        """The access points left once a band preference for this network applies."""
        if settings.get("band_preference_enabled") and settings.get(
            "band_preference_ssid"
        ) == ssid:
            want = settings.get("band_preference")
            aps = [ap for ap in aps if self._freq_band(ap[2]) == want]
        return aps

    def _lock_target(
        self, iface: str, ssid: str, current: str, settings: dict,
        link: str = "", uuid: str = ""
    ) -> tuple[tuple[str, int, int, str] | None, int | None]:
        """The AP worth moving to (or None), and the signal of the one we are on.

        The lock pins whichever access point you happen to be on. On a mesh
        that is often not the one you want - a handheld carried between rooms
        keeps a distant node long after a much closer one appears - and pinning
        it turns a passing bad choice into a permanent one. So the lock looks
        first, and only moves when the difference is worth a reconnect.

        The current signal comes back alongside so a move can be logged with
        both sides of the comparison it was made on. It is None when the scan
        cannot see the access point we are on.
        """
        if self._link_is_strong(link):
            return None, None
        aps = self._sampled_access_points(iface, ssid)
        key_mgmt = self._profile_key_mgmt(uuid) if uuid and aps else ""
        aps = self._lock_candidates(aps, ssid, settings, key_mgmt)
        if not aps:
            return None, None
        best = aps[0]
        here = next((ap for ap in aps if ap[0] == current.upper()), None)
        # Unknown current signal means the scan cannot see what we are on, so
        # there is nothing to compare and no case for moving.
        if here is None:
            return None, None
        if best[0] == current.upper():
            return None, here[1]
        if best[1] - here[1] < self._AP_UPGRADE_MARGIN:
            return None, here[1]
        return best, here[1]

    def _preferred_access_point(
        self, iface: str, ssid: str, current: str, settings: dict,
        link: str = "", uuid: str = ""
    ) -> tuple[str, int, int, str] | None:
        """The AP worth moving to, or None to stay where we are.

        _lock_target without the current signal, for a caller that only
        needs to know where the lock would go.
        """
        return self._lock_target(iface, ssid, current, settings, link, uuid)[0]

    def _band_unreachable(self, uuid: str, band: str) -> dict | None:
        """The refusal to return when this network has nothing on the band.

        The lock checks this before it cycles the radio and the band setter
        did not, so turning it on where the network has nothing on that band
        disconnected the device until the stranded-pin release gave up on it
        a minute later.
        """
        iface = self._get_wifi_interface()
        # Only a scan that answered and found nothing is a reason to refuse.
        # One that could not be read says nothing either way, and refusing on
        # it would block the setting for no reason the user could act on.
        if not iface or self._band_presence(iface, uuid, band) is not False:
            return None
        name = BAND_NAMES.get(band, band)
        decky.logger.info(
            f"Refusing band {band!r}: no {name} access point for this "
            f"network in the scan of {iface}"
        )
        return {
            "success": False,
            "error": "band_unavailable",
            "message": f"No {name} access point for this network is in range, so this would disconnect you.",
        }

    @staticmethod
    def _freq_band(freq) -> str:
        """The band property value that admits this frequency, "" if none does.

        NetworkManager's band "a" is the 5 GHz band only. A 6 GHz access point
        fails its compatibility check against a profile pinned to "a", so a
        6 GHz association is "6GHz", never "a", and counting it as 5 GHz let
        the plugin write a band that pushes the device off 6 GHz.
        """
        found = re.match(r"\s*(\d+)", str(freq or ""))
        if not found:
            return ""
        mhz = int(found.group(1))
        if 2400 <= mhz < 2500:
            return "bg"
        # The top of NetworkManager's own 5 GHz table, the list band "a" hands
        # wpa_supplicant to associate within.
        if 4900 <= mhz <= 5825:
            return "a"
        # The 6 GHz band, 5925 to 7125 MHz.
        if 5925 <= mhz <= 7125:
            return "6GHz"
        return ""

    def _band_is_reachable(self, iface: str, uuid: str, want: str) -> bool:
        """Whether this network has a visible AP on the wanted band.

        Checked before cycling the radio, because a cycle that cannot get
        back leaves the user disconnected - worse than the state the cycle
        was meant to improve.
        """
        return self._band_presence(iface, uuid, want) is True

    def _band_presence(self, iface: str, uuid: str, want: str) -> bool | None:
        """True or False from a scan that answered, None when none did.

        The same scan budget as the access point list. A cold list makes
        nmcli rescan, which takes several seconds; at five seconds it timed
        out inside Decky and read as "nothing on that band" on a Deck that
        was connected on it at the time.
        """
        ssid = self._get_profile_ssid(uuid)
        if not ssid:
            return None
        scan = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "SSID,FREQ", "dev", "wifi", "list",
             "ifname", iface],
            timeout=self._SCAN_SECONDS,
        )
        if not scan["success"]:
            decky.logger.error(
                f"Could not list access points on {iface}: "
                f"{_tool_text((scan.get('stderr', '') or 'timed out')[:160])}"
            )
            return None
        for line in scan.get("stdout", "").split("\n"):
            name, _, freq = line.rpartition(":")
            # Unescape BOTH sides. _get_profile_ssid returns nmcli's terse
            # value with its escapes intact, so comparing it against an
            # unescaped scan result fails for any SSID containing : or \.
            if name.replace("\\", "") != ssid.replace("\\", ""):
                continue
            if self._freq_band(freq) == want:
                return True
        return False

    def _band_conflicts(
        self, settings: dict, frequency: str, ssid: str | None = None
    ) -> bool:
        """Whether pinning an address now would contradict the band setting.

        Writing both a band and an address from the other band leaves a
        profile no access point satisfies, so it never associates again.
        Setting a band does not move an existing association, so this must be
        judged from the band the radio is actually on.

        It must also respect the network the preference is scoped to. Without
        that, a preference set at home suppresses the address pin on every
        other network forever - the drift flag never clears, the lock never
        applies, and the refusal tells the user a network requires a band it
        was never asked to.
        """
        if not settings.get("band_preference_enabled"):
            return False
        # An unrecorded scope means we do not know which network this belongs
        # to. Enforcing it everywhere is what stranded profiles in the first
        # place, and the cost of not enforcing is only that the preference
        # does not apply until a successful read claims a network.
        scoped_to = settings.get("band_preference_ssid", "")
        if not scoped_to:
            return False
        if ssid is not None and ssid != scoped_to:
            return False
        # Take the leading number whatever follows it. iw has reported this as
        # "5180", "5180 MHz" and "5180.0" across versions, and a form we
        # cannot read must refuse rather than guess. A 6 GHz association
        # satisfies neither band, so it conflicts with both.
        return self._freq_band(frequency) != settings.get("band_preference")

    def _propose_band(self, settings: dict, uuid: str, live_band: str,
                      active_ssid, status: dict, actions: list):
        """Report band drift, and propose writing it back when that is safe."""
        # On another network the preference is not ours to enforce, and the
        # toggle reading plain "on" there claims a band that is not held.
        scoped_to = settings.get("band_preference_ssid", "")
        if settings.get("band_preference_enabled") and scoped_to:
            here = active_ssid()
            if here is not None and here != scoped_to:
                status["live"]["band_other_network"] = True
        expected_band = settings.get("band_preference", "a")
        if (
            not self._band_preference_applies_here(settings, active_ssid)
            or live_band == expected_band
        ):
            return
        # Not within the hold after the band was released for not joining:
        # the release notice says it comes back later, and a drift banner
        # beside it offered Fix now to pin it again at once.
        released = getattr(self, "_released_at", {}).get("802-11-wireless.band", 0)
        if released and time.time() - released < self._RELEASE_NOTICE_SECONDS:
            return
        status["drift"]["band_preference"] = True
        if self._band_reassert_is_safe(status, expected_band, active_ssid() or ""):
            actions.append({
                "kind": "band", "uuid": uuid, "value": expected_band,
                "observed": live_band,
            })

    def _band_reassert_is_safe(self, status: dict, want: str, ssid: str) -> bool:
        """Whether writing the preferred band back cannot strand the device.

        Writing a band does not move the association, so the harm comes at
        the next reconnect: a device on 2.4 GHz because 5 GHz is out of reach
        gets a profile that demands 5 GHz, fails to rejoin, has the pin
        released after a minute, and then has it written straight back. So
        not for a while after a release, and otherwise only while the device
        is on the wanted band or the network shows an access point on it.
        """
        released = getattr(self, "_released_at", {}).get(
            "802-11-wireless.band", 0
        )
        if released and time.time() - released < self._RELEASE_NOTICE_SECONDS:
            return False
        if self._freq_band(status.get("live", {}).get("frequency", "")) == want:
            return True
        return any(
            self._freq_band(freq) == want
            for _bssid, freq, _signal in self._visible_networks().get(
                ssid.replace("\\", ""), []
            )
        )

    # Why the access point lock and the band preference stand down under iwd.
    _NEEDS_WPA_SUPPLICANT = (
        "needs the wpa_supplicant WiFi backend. Under iwd, NetworkManager "
        "cannot bring up a network pinned this way, so it would ask for the "
        "password instead of connecting. Switch to wpa_supplicant in Advanced "
        "to use it."
    )

    def _backend_is_iwd(self) -> bool:
        return self._backend_in_use() == "iwd"

    def _backend_in_use(self, configured: str | None = None) -> str | None:
        """The WiFi backend running now: 'iwd', 'wpa_supplicant' or None.

        On SteamOS the configured one, as before. Elsewhere a change of
        backend only lands at the next boot after Reset or an uninstall (our
        file goes, the old backend keeps running), so the configuration can
        name one that is not running. There the service decides: when exactly
        one of the two is active, that one is in use. Reading the config
        instead wrote pins under a live iwd and refused the lock under a live
        wpa_supplicant. configured: _get_current_backend(), when the caller
        already has it.
        """
        if configured is None:
            configured = self._get_current_backend()
        try:
            if _load_settings().get("distro_id") == "steamos":
                return configured
        except Exception:
            return configured
        return self._running_backend() or configured

    def _running_backend(self) -> str | None:
        """The one of iwd and wpa_supplicant whose service is active, when
        exactly one is; None when neither or both are, or it cannot tell."""
        active = [
            name for name in ("iwd", "wpa_supplicant")
            if (self._run_cmd(["/usr/bin/systemctl", "is-active", name], timeout=3)
                .get("stdout") or "").strip() == "active"
        ]
        return active[0] if len(active) == 1 else None

    def _pins_for_backend(self, settings: dict, status: dict) -> dict:
        """The settings this pass acts on, with both pins off under iwd.

        NetworkManager's iwd backend lists every access point with a made-up
        address on a fixed 2.4 GHz frequency, and only brings a profile up
        through one it finds compatible. A profile pinned to a real address,
        or to 5 GHz, matches none of them, so every activation NetworkManager
        makes for it fails and SteamOS asks for the password again. iwd's own
        autoconnect gets past that check, which is why it looks fine until
        anything reconnects. So under iwd neither pin is written, and the ones
        we wrote are cleared; the settings stay on for when wpa_supplicant is
        back. The status still shows the user's real settings.
        """
        if not (settings.get("bssid_lock_enabled") or settings.get("band_preference_enabled")
                or settings.get("bssid_lock_uuids") or settings.get("band_preference_uuids")):
            return settings
        if not self._backend_is_iwd():
            return settings
        status["live"]["pins_inactive_iwd"] = True
        return {**settings, "bssid_lock_enabled": False, "band_preference_enabled": False}

    async def _unpin_if_iwd(self):
        """Clear every lock and band pin of ours once iwd is found in use.

        The status pass clears them one profile at a time, and only while the
        panel polls. A switch to iwd made outside the plugin (Steam's own
        developer setting, or an install already on iwd when it upgrades)
        would otherwise leave the profile in use pinned, and the next
        reconnect NetworkManager makes for it fails.
        """
        settings = _load_settings()
        if not any(settings.get(k) for k in (
            "bssid_lock_enabled", "band_preference_enabled",
            "bssid_lock_uuids", "band_preference_uuids",
        )):
            return
        if not await asyncio.to_thread(self._backend_is_iwd):
            self._iwd_unpinned = False
            return
        if getattr(self, "_iwd_unpinned", False) or self._profile_change_in_flight():
            return
        await asyncio.to_thread(self._clear_lock_and_band_pins)
        self._iwd_unpinned = True
        decky.logger.info("iwd is in use: cleared our access point and band pins")

    def _clear_lock_and_band_pins(self):
        """Take our address and band pins off every profile we wrote them to.

        Run before switching to iwd, where a pinned profile cannot be brought
        up by NetworkManager. The tracking lists are kept, so the pins come
        back on their own if the switch fails or wpa_supplicant returns.
        """
        settings = _load_settings()
        owned = settings.get("pins_owned", [])
        last = settings.get("last_connection_uuid", "")
        lock = set(settings.get("bssid_lock_uuids", []))
        if settings.get("bssid_lock_connection_uuid"):
            lock.add(settings["bssid_lock_connection_uuid"])
        band = set(settings.get("band_preference_uuids", []))
        # The profile last in use, when this install is known to write that
        # kind of pin: it is the one most likely to be reconnected next.
        if last and "802-11-wireless.bssid" in owned:
            lock.add(last)
        if last and "802-11-wireless.band" in owned:
            band.add(last)
        for uuid in lock:
            self._nmcli_modify(uuid, "802-11-wireless.bssid", "", timeout=2)
        for uuid in band:
            self._nmcli_modify(uuid, "802-11-wireless.band", "", timeout=2)

    def _profile_change_in_flight(self) -> bool:
        # True while a setter is deliberately rewriting the active profile -
        # a band change or an access point lock. Reconciliation must not
        # "correct" a profile mid-change back to what it used to say.
        #
        # The count is released in a finally, including on cancellation, and a
        # process that dies mid-change takes the whole instance with it - so
        # there is nothing a deadline here could catch that this does not.
        return getattr(self, "_profile_change_depth", 0) > 0

    def _profile_change_refused(self) -> bool:
        """Whether a setter that rewrites profiles must not start now.

        Another one mid-change interleaves its radio cycle and writes with
        ours. An update reloads the plugin when it finishes, which would cut
        this off between its writes and its reconnect. A backend switch
        restarts NetworkManager under it, and a switch to iwd clears the very
        pins the lock and band write.
        """
        return (self._profile_change_in_flight() or self._update_running()
                or self._backend_switch_running())

    def _backend_switch_running(self) -> bool:
        return bool((getattr(self, "_backend_switch", None) or {}).get("in_progress"))

    def _collect_backend_state(self, status: dict):
        """Whether the WiFi backend can be switched here, and which one is up.

        System-wide, so it is reported whether or not there is a connection -
        the panel offers the control either way.

        Uses the memoised probe result only. Probing here would fork from the
        worker thread and write to the instance, which the collector promises
        not to do; _main warms it at startup instead.
        """
        backend_available = (
            getattr(self, "_steamos_manager_available", False)
            or self._get_backend_method_cached()
        )
        status["live"]["backend_tool_available"] = backend_available
        if backend_available:
            configured = self._get_current_backend()
            in_use = self._backend_in_use(configured)
            status["live"]["wifi_backend"] = in_use or ""
            # The one NetworkManager is set to start at the next boot, when
            # that is not the one running (contract C5).
            if configured and in_use and configured != in_use:
                status["live"]["wifi_backend_next"] = configured

    def _propose_precedence(self, settings: dict, uuid: str,
                            pending: dict, actions: list):
        """Decide whether this profile needs to outrank its duplicates.

        NetworkManager keeps several profiles per SSID and, with nothing to
        separate them, falls back to which was used last - which is how one
        network comes to behave like two, and why a device lands on 2.4 GHz
        after a reboot. A higher autoconnect priority on the profile actually
        in use settles it.

        Arriving on a different profile reopens the question: the flag records
        that a decision was made for the profile named beside it, so both move
        together or a new profile inherits the old one's answer.

        The priority itself is computed here, in the worker thread, because
        deciding it means reading the rival profiles; the handler runs on the
        event loop and would hold it for the duration.
        """
        if uuid and uuid != settings.get("last_connection_uuid"):
            settings["last_connection_uuid"] = uuid
            settings["priority_set"] = False
            pending["last_connection_uuid"] = uuid
            pending["priority_set"] = False

        if uuid and not settings.get("priority_set"):
            target = self._precedence_priority(uuid)
            if target is None:
                # Already outranks them. Nothing to write, and recording it
                # stops the next poll asking the same question.
                pending["priority_set"] = True
            else:
                actions.append(
                    {"kind": "priority", "uuid": uuid, "value": target}
                )

    def _band_preference_applies_here(self, settings: dict, active_ssid) -> bool:
        """Whether the band preference is ours to enforce on this network.

        Only on the network it was set on. Enforcing it everywhere writes a
        band into profiles for networks that may not offer it, which then stop
        connecting - and the panel gives no sign that is what happened. The
        address pin is scoped the same way.

        Without a recorded network it is enforced nowhere. That is the same
        rule the conflict test uses: no record means no scope, not every scope.
        The SSID is only read once both of those are settled, so a preference
        that is off costs no subprocess.
        """
        if not settings.get("band_preference_enabled"):
            return False
        band_ssid = settings.get("band_preference_ssid", "")
        if not band_ssid:
            return False
        return active_ssid() == band_ssid

    def _collect_host_tuning(self, settings: dict, uuid: str, iface: str,
                             status: dict, actions: list, timeout: int = 5):
        """Whether the tunings that live on the HOST are still in place.

        The settings above belong to a NetworkManager profile and drift when
        something rewrites the profile. These three are different: the receive
        buffers are a kernel value, the queueing discipline is attached to the
        interface, and the dispatcher is a file on disk - so they are lost to
        an OS update or a reboot rather than to NetworkManager.
        """
        # Each is judged only from an answer, as power save is: a sysctl or
        # tc that did not answer (one stuck behind a wedged driver above all)
        # says nothing about what is in place, and reading it as drift
        # offered a Fix now that could not fix it.
        rmem = self._run_cmd(
            ["/usr/bin/sysctl", "-n", "net.core.rmem_max"], timeout=timeout
        )
        current_rmem = (rmem.get("stdout") or "").strip()
        rmem_known = bool(rmem.get("success")) and bool(current_rmem)
        # In auto mode with no stream running these are off on purpose.
        gate_open = self._volatile_gate_open(settings)
        status["live"]["buffer_tuning_applied"] = current_rmem == TUNED_RMEM_MAX
        if (settings.get("buffer_tuning_enabled") and rmem_known
                and current_rmem != TUNED_RMEM_MAX and gate_open):
            status["drift"]["buffer_tuning"] = True

        cake_active = self._get_cake_status(iface)
        status["live"]["cake_applied"] = cake_active is True
        if settings.get("cake_enabled") and cake_active is False and gate_open:
            status["drift"]["cake"] = True
            # Nothing else clears this. Optimize Safe covers the safe tier
            # only, so without a reassertion here the drift warning stays up
            # forever with no control that resolves it. The dispatcher already
            # reapplies CAKE on every reconnect, so doing it here matches
            # behaviour the user has already opted into.
            actions.append({"kind": "cake", "uuid": uuid, "iface": iface})

        status["live"]["dispatcher_installed"] = os.path.isfile(DISPATCHER_PATH)
        status["live"]["last_enforced"] = _read_small_int(ENFORCED_FILE)

    def _read_addressing(self, iface: str, timeout: int = 5) -> dict:
        """The address and resolvers NetworkManager reports for the device.

        Reported for the panel only, and nothing here drifts: these follow
        from the connection rather than from any setting of ours.
        """
        live = {}
        # Format: IP4.ADDRESS[1]:192.168.1.100/24
        ip_out = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "IP4.ADDRESS", "dev", "show", iface],
            timeout=timeout,
        ).get("stdout", "")
        if ":" in ip_out:
            live["ip_address"] = ip_out.split(":", 1)[1].split("/")[0].strip()
        live["dns"] = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "IP4.DNS", "dev", "show", iface],
            timeout=timeout,
        ).get("stdout", "")
        return live

    # NetworkManager reports a clash both ways, and on the night this was
    # measured one of two clashes appeared only as the DHCP form.
    def _report_network_problems(self, status: dict, uuid: str, iface: str,
                                 ssid: str, settings: dict):
        """Problems with the network itself, not with any setting of ours.

        Each is checked on its own throttle and only reported; nothing here
        writes or drifts.
        """
        live = status["live"]
        self._recheck_address_conflict(live.get("ip_address", ""))
        if getattr(self, "_address_conflict", None):
            status["address_conflict"] = dict(self._address_conflict)
        self._recheck_security_reach(uuid, iface, ssid, live.get("connected_bssid", ""),
                                     settings, live.get("bssid_lock", ""))
        if getattr(self, "_wpa2_blocked", None):
            status["wpa2_only_stronger"] = dict(self._wpa2_blocked)

    _CONFLICT_RE = re.compile(
        r"conflict detected for IP address (\S+) with host ([0-9A-Fa-f:]{17})"
        r"|new lease, address=([^,\s]+), acd conflict"
    )
    _CONFLICT_CHECK_SECONDS = 60

    def _recheck_address_conflict(self, ip: str):
        """Whether NetworkManager saw another device holding our address.

        When a router hands out an address that is already in use, the Deck
        shows WiFi as connected and nothing works, and a WiFi plugin is the
        obvious thing to blame. Measured on a Deck: every boot one evening
        logged "conflict detected for IP address 192.168.1.9 with host ...",
        it kept happening with the plugin uninstalled, and it was the router.
        NetworkManager only logs it, so read the log, for the address we hold
        now, over the last ten minutes. Throttled, since it is a journal read.
        """
        now = time.monotonic()
        if now - getattr(self, "_conflict_checked_at", 0.0) < self._CONFLICT_CHECK_SECONDS \
                and getattr(self, "_conflict_checked_ip", None) == ip:
            return
        self._conflict_checked_at = now
        self._conflict_checked_ip = ip
        self._address_conflict = None
        if not ip:
            return
        # A clash logged before the device took the address it holds now was
        # about an earlier lease: after the router was put right and the
        # device moved off the address and back, the warning stayed up for up
        # to ten minutes. So the window starts when the address last changed,
        # as this process saw it. Not for the first address it sees: that one
        # was usually taken at boot, before the plugin loaded, which is
        # exactly when a clash is logged.
        seen = getattr(self, "_conflict_ip_seen", None)
        if seen is None or seen[0] != ip:
            seen = (ip, None if seen is None else time.time())
            self._conflict_ip_seen = seen
        since = "-10min"
        if seen[1] is not None and seen[1] > time.time() - 600:
            since = f"@{int(seen[1])}"
        log = self._run_cmd(
            ["/usr/bin/journalctl", "-u", "NetworkManager", "--since", since,
             "-o", "cat", "--no-pager"],
            timeout=5,
        )
        for found in self._CONFLICT_RE.finditer(log.get("stdout", "")):
            if ip in (found.group(1), found.group(3)):
                other = (found.group(2) or "").upper()
                known = (self._address_conflict or {}).get("other", "")
                self._address_conflict = {"ip": ip, "other": other or known}

    def _classify_ipv6(self, settings: dict, uuid: str, live_ipv6: str,
                       status: dict, actions: list):
        """Decide whether the profile's IPv6 state is ours or someone else's.

        OURS and not holding is drift, and gets reasserted. Off when we are not
        the ones turning it off, with no record of us having done so, is
        reported as set ELSEWHERE and changed by nothing.

        The address and band pins are cleaned up in that situation and IPv6
        deliberately is not: nobody sets a BSSID or a band by hand, so a stray
        one is ours by elimination, but switching IPv6 off is an ordinary thing
        for someone to have done deliberately. Turning it back on would be
        this plugin overriding a choice it did not make.

        The distinction matters to the panel's wording too - "drifted" means
        our setting stopped holding, and saying that here would blame the
        plugin for a state it did not create and cannot explain.
        """
        if settings.get("ipv6_disabled"):
            tracked = settings.get("ipv6_uuids", [])
            # Only the networks it was turned off on. Reasserting on whatever
            # profile is active wrote "disabled" into every network the panel
            # was opened on, without recording it, so nothing could undo it
            # and the panel later blamed something else. A profile already
            # off with nothing recorded is a setting from before tracking
            # existed: adopt it so the toggle can undo it.
            if uuid and not tracked and live_ipv6 == "disabled":
                actions.append({"kind": "ipv6_adopt", "uuid": uuid})
            elif uuid and uuid not in tracked:
                status["live"]["ipv6_other_network"] = True
            elif live_ipv6 != "disabled":
                status["drift"]["ipv6"] = True
                actions.append({
                    "kind": "ipv6", "uuid": uuid, "observed": live_ipv6,
                })
        elif (
            live_ipv6 == "disabled"
            and uuid not in settings.get("ipv6_uuids", [])
        ):
            status["external"]["ipv6"] = True

    def _check_dns_drift(self, settings: dict, uuid: str, status: dict,
                         timeout: int = 5):
        """Whether custom DNS still holds on the network it was set for.

        Something else rewriting the profile (the network settings, a
        reconnect through another tool) silently put the router's DNS back
        with the toggle still reading on. Reported only: putting DNS back
        means a reconnect, which a status poll must never cause, so it is
        Fix now that repairs it, through set_dns. A read that fails or comes
        back incomplete says nothing, rather than drift.
        """
        if not settings.get("dns_enabled") or not uuid:
            return
        # Only where it was set. An empty list is a setting from before the
        # list existed, which applied to whichever network was in use.
        tracked = settings.get("dns_uuids", [])
        if tracked and uuid not in tracked:
            status["live"]["dns_other_network"] = True
            return
        want = str(settings.get("dns_servers", "")).replace(",", " ").split()
        if not want:
            return
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f",
             "ipv4.dns,ipv4.ignore-auto-dns,ipv6.ignore-auto-dns",
             "con", "show", "uuid", uuid],
            timeout=timeout,
        )
        if not result.get("success"):
            return
        fields = {}
        for line in result.get("stdout", "").splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip()
        if "ipv4.dns" not in fields or "ipv4.ignore-auto-dns" not in fields:
            return
        # Newer nmcli separates servers with commas, older with spaces, and
        # terse output escapes any colon with a backslash.
        have = fields["ipv4.dns"].replace("\\", "").replace(",", " ").split()
        if not set(want) <= set(have) or fields["ipv4.ignore-auto-dns"] != "yes":
            status["drift"]["dns"] = True
        # The router's IPv6 DNS let back in is drift too: on a dual-stack
        # network it is used alongside ours. A profile set before custom DNS
        # covered IPv6 shows here once, and Fix now brings it up to date.
        if fields.get("ipv6.ignore-auto-dns", "yes") != "yes":
            status["drift"]["dns"] = True

    def _recheck_security_reach(self, uuid: str, iface: str, ssid: str,
                                current_bssid: str, settings: dict | None = None,
                                pinned: str = ""):
        """Notice a stronger access point the saved security cannot use.

        A profile saved as WPA3-only cannot join an access point of the same
        network that only offers WPA2, however close it is. That is common
        when an older router or an extender sits alongside a newer mesh, and
        it looks to the user like the Deck preferring the far access point
        for no reason. Measured on a Deck: the office radio at -33 dBm, WPA2
        only, never chosen; the mesh node across the house at -74 always.

        Reported only past the same margin the lock uses to move, so a
        roughly equal access point is not worth a notice. Reads nmcli's
        cached scan on the duplicate check's throttle. With no BSSID pinned
        that adds no scans, since background scans keep the list current.
        A pin (pinned, the profile's BSSID) stops those scans and leaves the
        other access points at whatever they read when it was set, so then
        the check waits for a fresh list (_lock_scan_is_fresh), keeping its
        last finding meanwhile.
        """
        now = time.monotonic()
        if (
            now - getattr(self, "_reach_checked_at", 0.0)
            <= self._DUPLICATE_CHECK_SECONDS
            and getattr(self, "_reach_checked_for", None) == uuid
        ):
            return
        if getattr(self, "_reach_checked_for", None) != uuid:
            # Nothing found for another profile applies to this one.
            self._wpa2_blocked = None
            self._reach_checked_for = uuid
        # Checked before any scan is asked for: a profile that already
        # allows WPA2 has nothing to find.
        if self._profile_key_mgmt(uuid) != "sae" or not ssid:
            self._reach_checked_at = now
            self._wpa2_blocked = None
            return
        fresh = (self._lock_scan_is_fresh(settings or {}, iface, now, "_reach_scan_used")
                 if pinned else True)
        if fresh is None:
            # Not known yet whether a stream is running: the search asked
            # for now answers by the next poll, so look again then.
            self._reach_checked_at = now - self._DUPLICATE_CHECK_SECONDS - 1
            return
        if not fresh:
            # Look again once a scan asked for now has had time to land.
            self._reach_checked_at = (now - self._DUPLICATE_CHECK_SECONDS
                                      + self._LOCK_SCAN_SETTLE_SECONDS + 2)
            return
        self._reach_checked_at = now
        # Decided in full before it is recorded: allow_wpa2 reads the finding
        # from the event loop while this runs in the status worker.
        self._wpa2_blocked = self._wpa2_finding(iface, ssid, current_bssid,
                                                settings or {})

    def _wpa2_finding(self, iface: str, ssid: str, current_bssid: str,
                      settings: dict) -> dict | None:
        """The WPA2-only access point clearly stronger than where we are, if any.

        Only among those on the preferred band, when one is set for this
        network: one on another band is not somewhere the device may go, and
        no WPA2-only access point is on 6 GHz at all.
        """
        # The cached list: this runs inside the status poll, and letting nmcli
        # decide to rescan there held the poll for seconds.
        aps = self._on_preferred_band(
            self._visible_access_points(iface, ssid, cached_only=True), ssid, settings
        )
        blocked = [
            ap for ap in aps
            if not self._ap_accepts(ap[3], "sae")
            and self._ap_accepts(ap[3], "wpa-psk")
        ]
        usable = [ap for ap in aps if self._ap_accepts(ap[3], "sae")]
        # Before NetworkManager 1.58.1 a wpa-psk profile cannot connect on
        # its own to an access point that offers WPA3 only. Allowing WPA2
        # where one is in sight trades it for the WPA2 one: the device would
        # then only ever join that. The version is the one _main read, as
        # this runs in the status worker; not read yet counts as too old.
        version = getattr(self, "_nm_version_seen", None)
        if not version or version < NM_6GHZ_WPA_PSK:
            if any(not self._ap_accepts(ap[3], "wpa-psk") for ap in usable):
                return None
        here = next(
            (ap[1] for ap in usable if ap[0] == current_bssid.upper()),
            usable[0][1] if usable else None,
        )
        if not blocked or here is None:
            return None
        if blocked[0][1] - here >= self._AP_UPGRADE_MARGIN:
            return {"signal": blocked[0][1], "current_signal": here}
        return None

    def _report_lock_weaker(self, settings: dict, status: dict, uuid: str,
                            iface: str, ssid: str, link: str):
        """Say when the lock is holding the device on a much weaker access point.

        Reported only. Moving would reconnect, possibly in the middle of a
        game stream, and keeping the link from bouncing between access points
        is the reason the lock exists, so the move is the user's to make.
        """
        self._recheck_lock_weaker(settings, status["live"], uuid, iface, ssid, link)
        if getattr(self, "_lock_weaker", None):
            status["lock_weaker"] = dict(self._lock_weaker)

    def _forget_lock_weaker(self):
        """Drop the finding and anything seen towards it, so it starts over."""
        self._lock_weaker = None
        self._lock_weaker_since = None
        self._lock_weaker_checked_for = None

    def _recheck_lock_weaker(self, settings: dict, live: dict, uuid: str,
                             iface: str, ssid: str, link: str):
        """Notice a much stronger access point than the one the lock pins.

        The lock picks the best access point when it is turned on and then
        holds it. A handheld carried through a house leaves that one behind,
        and the only release, the stranded-pin safety net, needs the device
        to be offline first. Reported by the owner: carried to another room,
        the link went bad and he turned the lock off, suspecting it had held
        him on the wrong access point, because nothing said whether it had.

        Only while the lock is actually holding: on, written for the profile
        in use, and pinning the access point we are on now. Candidates follow
        the lock's own rules (_lock_candidates), so what is offered is what a
        move would choose. Reads nmcli's cached scan on its own short
        throttle, so a poll never waits on a rescan. The lock stops the scans
        that keep that list current, so it asks for one now and then itself
        (_lock_scan_is_fresh), and the finding has to hold on two reads taken
        after two separate scans before it is reported.
        """
        pinned = live.get("bssid_lock", "").upper()
        # nmcli reports addresses upper-case and iw lower-case.
        on = live.get("connected_bssid", "").upper()
        switch = getattr(self, "_backend_switch", None) or {}
        if (
            # Under iwd the status pass hands this the lock as off.
            not settings.get("bssid_lock_enabled")
            or uuid != settings.get("bssid_lock_connection_uuid")
            or not pinned or pinned != on or not ssid
            # The lock would not move off a healthy link, so offering it
            # would only produce a move that declines.
            or self._link_is_strong(link)
            or self._profile_change_in_flight()
            or self._update_running()
            or switch.get("in_progress")
        ):
            self._forget_lock_weaker()
            return
        now = time.monotonic()
        key = (uuid, pinned)
        if getattr(self, "_lock_weaker_checked_for", None) != key:
            # A different profile or pin: nothing seen so far applies to it.
            self._forget_lock_weaker()
        elif now - getattr(self, "_lock_weaker_checked_at", 0.0) \
                < self._LOCK_WEAKER_CHECK_SECONDS:
            return
        previous = getattr(self, "_lock_weaker_checked_at", 0.0)
        self._lock_weaker_checked_at = now
        self._lock_weaker_checked_for = key
        fresh = self._lock_scan_is_fresh(settings, iface, now)
        if fresh is None:
            # Not known yet whether a stream is running. The throttle is not
            # spent on it: the search asked for now answers by the next poll.
            # Spent, the next check came 30 s later, when the only search
            # (this one) was too old to go by again, and no scan was ever
            # asked for with auto mode off.
            self._lock_weaker_checked_at = previous
            return
        if not fresh:
            # A stale list can neither start, confirm nor clear the finding:
            # its other access points are wherever the lock was set, or gone.
            return
        # The cached list: this runs inside the status poll, and letting nmcli
        # decide to rescan there held the poll for seconds.
        aps = self._visible_access_points(iface, ssid, cached_only=True)
        here = next((ap[1] for ap in aps if ap[0] == pinned), None)
        better = [
            ap for ap in self._lock_candidates(
                aps, ssid, settings, self._profile_key_mgmt(uuid) if aps else ""
            )
            if ap[0] != pinned
        ]
        if here is None or not better \
                or better[0][1] - here < self._LOCK_WEAKER_MARGIN:
            # Gone, recovered, or never there: start over.
            self._lock_weaker = None
            self._lock_weaker_since = None
            return
        since = getattr(self, "_lock_weaker_since", None)
        if since is None:
            # One read is not evidence. Remember it and wait for the next.
            self._lock_weaker_since = now
            return
        if now - since >= self._LOCK_WEAKER_CHECK_SECONDS:
            self._lock_weaker = {"signal": better[0][1], "current_signal": here}

    def _lock_scan_is_fresh(self, settings: dict, iface: str, now: float,
                            used: str = "_lock_scan_used") -> bool | None:
        """Whether nmcli's cached list is fresh enough to judge the lock on.

        Asks for a scan when one is due, and then says no: the answer takes a
        few seconds to land (_LOCK_SCAN_SETTLE_SECONDS), and asking returns at
        once rather than holding the poll. A read counts once it is past that
        and before the next scan is due, and only one read per scan counts,
        since two reads of one scan are one sample. Each check keeps its own
        record of that (used) and they share the scans, so a second check
        never adds one. Under a stream no scan is asked for, so nothing is
        fresh and nothing new is said until it ends.

        None (falsy, as not fresh) when a scan is due and whether a stream
        is running is not known yet: the caller looks again at the next poll
        rather than after its own throttle, since the search started now has
        answered by then and an answer older than PROC_SCAN_FRESH_SECONDS is
        not gone by.
        """
        asked = getattr(self, "_lock_scan_at", None)
        if asked is None or now - asked >= self._LOCK_SCAN_SECONDS:
            # Without waiting on the process search: this runs in the
            # status read, which a search stuck in the kernel would hold
            # for good. Not known yet means ask again at the next poll.
            streaming = self._stream_running_now(settings)
            if streaming is None:
                return None
            if not streaming:
                # Failures are ignored: one already in progress is fine, and
                # the attempt still counts against the limit, so a refusal is
                # not retried on every poll.
                self._run_cmd(
                    ["/usr/bin/nmcli", "dev", "wifi", "rescan", "ifname", iface],
                    timeout=3,
                )
                self._lock_scan_at = now
            return False
        if now - asked < self._LOCK_SCAN_SETTLE_SECONDS:
            return False
        if getattr(self, used, None) == asked:
            return False
        setattr(self, used, asked)
        return True

    def _stream_running(self, settings: dict) -> bool:
        """Whether a game stream looks to be running, auto mode on or not.

        Auto mode's flag says so when it is on, and the hold means the user
        has said so. With auto mode off nothing keeps that flag, so the same
        process search it uses is run here; scanning in the middle of a
        stream is the one thing this must not do.
        """
        if os.path.isfile(STREAM_FLAG_FILE) or os.path.isfile(STREAM_HOLD_FILE):
            return True
        return self._find_stream(self._stream_patterns(settings)) is not None

    def _stream_running_now(self, settings: dict) -> bool | None:
        """_stream_running for the status read, which never waits on the
        process search (one stuck in the kernel held every status read
        after it): it goes by the last search's answer while that is recent
        (PROC_SCAN_FRESH_SECONDS), starting a new search for the next poll,
        and answers None while there is none to go by."""
        if os.path.isfile(STREAM_FLAG_FILE) or os.path.isfile(STREAM_HOLD_FILE):
            return True
        _PROC_SCAN_LOCAL.no_wait = True
        _PROC_SCAN_LOCAL.unknown = False
        try:
            found = self._find_stream(self._stream_patterns(settings))
        finally:
            _PROC_SCAN_LOCAL.no_wait = False
        if found is not None:
            return True
        return None if _PROC_SCAN_LOCAL.unknown else False

    async def allow_wpa2(self) -> dict:
        """Let a WPA3-only profile use the WPA2-only access points of its network.

        NetworkManager's wpa-psk key management offers WPA2 and WPA3 alike,
        and the supplicant still chooses WPA3 wherever an access point offers
        it, so this only widens what can be joined. If the profile cannot
        connect afterwards it is put back as it was.
        """
        if self._profile_change_refused():
            return self._busy_result()
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        held = True
        try:
            uuid, err = self._resolve_uuid("Connect to WiFi first.")
            if err:
                return err
            if self._profile_key_mgmt(uuid) != "sae":
                return {"success": False, "error": "not_needed",
                        "message": "This network already allows WPA2."}
            # Held to 6 GHz, where every access point uses WPA3, there is
            # nothing to gain, and a wpa-psk profile cannot join 6 GHz on
            # NetworkManager 1.58.0 or with management frames off.
            settings = _load_settings()
            if (settings.get("band_preference_enabled")
                    and settings.get("band_preference") == "6GHz"
                    and settings.get("band_preference_ssid")
                    and settings.get("band_preference_ssid") == self._get_profile_ssid(uuid)):
                return {"success": False, "error": "band_6ghz",
                        "message": "This network is held to 6 GHz, which only uses WPA3."}
            # Only for what the panel reported: a stronger WPA2-only access
            # point of this network, seen for this profile.
            if (not getattr(self, "_wpa2_blocked", None)
                    or getattr(self, "_reach_checked_for", None) != uuid):
                return {"success": False, "error": "not_needed",
                        "message": "No stronger WPA2-only access point is in range now, "
                                   "so nothing was changed."}
            # Recorded before the write, so Reset and uninstall can put WPA3
            # only back: nothing else in the panel undoes this. Recorded only
            # after the reconnect wait (up to half a minute), a Decky restart
            # or shutdown inside it left the profile on WPA2 with no record.
            settings = _load_settings()
            had_record = uuid in settings.get("wpa2_uuids", [])
            known = [u for u in settings.get("wpa2_uuids", []) if u != uuid] + [uuid]
            settings["wpa2_uuids"] = self._capped(known, "802-11-wireless-security.key-mgmt")
            settings["wpa2_restore_uuids"] = [
                u for u in settings.get("wpa2_restore_uuids", []) if u != uuid
            ]
            _save_settings(settings)

            def drop_record():
                # Put back as it was: the profile is WPA3 only again.
                if had_record:
                    return
                settings = _load_settings()
                settings["wpa2_uuids"] = [u for u in settings.get("wpa2_uuids", []) if u != uuid]
                _save_settings(settings)

            done = self._nmcli_modify(
                uuid, "802-11-wireless-security.key-mgmt", "wpa-psk"
            )
            if not done["success"]:
                drop_record()
                self._log_detail("allow_wpa2", done["stderr"])
                return {"success": False, "error": "nmcli_failed",
                        "message": "Couldn't change the network's security. Try again in a moment.",
                        "detail": done["stderr"]}
            if not await self._reconnected_to(uuid):
                restored = self._nmcli_modify(uuid, "802-11-wireless-security.key-mgmt", "sae")
                await self._reconnected_to(uuid)
                if restored["success"]:
                    drop_record()
                    decky.logger.error(
                        f"Allowing WPA2 on {uuid} did not reconnect; put back to WPA3 only"
                    )
                else:
                    # Still wpa-psk: the record stays, so Reset, uninstall or
                    # the leftover cleanup puts WPA3 only back later.
                    decky.logger.error(
                        f"Allowing WPA2 on {uuid} did not reconnect, and WPA3 only could "
                        f"not be put back: {_tool_text(restored.get('stderr', '')[:120])}"
                    )
                return {"success": False, "error": "reconnect_failed",
                        "message": "Couldn't reconnect, so the network was left as it was."}
            decky.logger.info(f"Allowed WPA2 alongside WPA3 on {uuid}")
            self._wpa2_blocked = None
            self._reach_checked_at = 0.0
            settings = _load_settings()
            result = {"success": True, "reconnected": True}
            # The lock picks its access point from those the profile can join,
            # so with the stronger one now eligible, let it choose again.
            if settings.get("bssid_lock_enabled"):
                # Hand the guard to the lock setter rather than hold it
                # across the call, or it refuses as busy. Nothing awaits
                # between the release and the setter taking it.
                self._profile_change_depth = max(0, self._profile_change_depth - 1)
                held = False
                relocked = await self.set_bssid_lock(True)
                if not relocked.get("success"):
                    # WPA2 is allowed, which is what was asked; but the lock is
                    # still on the access point it held, and the notice that
                    # led here is gone, so say so.
                    result["message"] = (
                        "WPA2 is allowed here now, but the access point lock "
                        "couldn't move, so it stayed where it was. Turn the lock "
                        "off and on to choose again."
                    )
                elif relocked.get("message"):
                    # A relock that went through with something to say (it
                    # stayed on the access point in use because the stronger
                    # one would not take): passed on as it is.
                    result["message"] = relocked["message"]
            return result
        except Exception as e:
            decky.logger.error(f"allow_wpa2 error: {e}")
            return self._unexpected_response(e)
        finally:
            if held:
                self._profile_change_depth = max(
                    0, getattr(self, "_profile_change_depth", 1) - 1
                )

    # How long a setter waits for its profile to come back after a reconnect.
    # `nmcli con up` can outlast its own call's timeout while the activation
    # carries on, so the answer is read from NetworkManager, not from it.
    _RECONNECT_WAIT_SECONDS = 30

    async def _reconnected_to(self, uuid: str) -> bool:
        if not await self._reconnect(uuid):
            return False
        for _ in range(self._RECONNECT_WAIT_SECONDS):
            if await asyncio.to_thread(self._connection_activated, uuid):
                return True
            await asyncio.sleep(1)
        return False

    def _connection_activated(self, uuid: str) -> bool:
        """Whether this profile is fully up, not merely being brought up.

        An active profile is listed from the moment it starts activating,
        including while it waits on a password or the handshake, so taking
        the listing as success counted a move to an access point that went
        on to refuse us.
        """
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "UUID,STATE", "con", "show", "--active"]
        )
        if not result["success"]:
            return False
        return f"{uuid}:activated" in result.get("stdout", "").split("\n")

    def _recheck_rival_profiles(self, settings: dict, uuid: str,
                                active_ssid, actions: list):
        """Look again at the other saved copies of the network in use.

        Decides two things and records them for the rest of the poll: whether
        the copies disagree about security, and whether the active profile is
        still tied with them on autoconnect priority.

        Several saved copies of one network, disagreeing about security, is a
        condition the user cannot see and cannot diagnose, and it makes
        everything else here look intermittent.

        Throttled, because it costs a few nmcli calls per rival and nothing
        about it changes quickly - so the findings are kept on the instance and
        re-used by the polls in between.
        """
        now = time.monotonic()
        if (
            now - getattr(self, "_dup_checked_at", 0.0)
            <= self._DUPLICATE_CHECK_SECONDS
            and getattr(self, "_dup_checked_for", None) == uuid
        ):
            return
        self._dup_checked_at = now
        self._dup_checked_for = uuid
        mine = self._profile_key_mgmt(uuid)
        rivals = self._rival_profiles(uuid, active_ssid() or "")
        self._dup_conflict = bool(
            rivals and any(km and km != mine for _u, km in rivals)
        )
        # Read on the same throttle as the duplicate check itself. These
        # profiles are not the active one, so nothing else in the poll has
        # looked at what is written to them.
        self._rival_pins = {
            other: self._profile_pins(other) for other, _km in rivals
        }
        # An install marked done long ago can still be sitting on a tie - and
        # with duplicates present that tie is what makes one network behave
        # like two. Reuse the rivals already read here rather than enumerating
        # them again. Whether the profile in use already outranks them is
        # also what the panel may claim about it.
        settled = self._precedence_priority(uuid, rivals=rivals) if rivals else None
        self._dup_outranks = bool(rivals) and settled is None
        if settled is not None and settings.get("priority_set"):
            actions.append({
                "kind": "priority", "uuid": uuid,
                "value": settled, "force": True,
            })
        if self._dup_conflict:
            self._log_throttled(
                "duplicate_profiles",
                f"This network is saved more than once with different "
                f"security ({mine} here, "
                f"{', '.join(km for _u, km in rivals if km)} elsewhere); "
                f"which copy NetworkManager picks changes which access "
                f"points are reachable",
            )

    def _lock_held_elsewhere(self) -> bool:
        """Whether the access point lock is on, for a network other than the
        one in use (or one that cannot be read, which counts as other)."""
        settings = _load_settings()
        if not settings.get("bssid_lock_enabled"):
            return False
        uuid = self._get_active_connection_uuid()
        if not uuid:
            return False
        ssid = self._get_profile_ssid(uuid)
        return not self._lock_is_for_this_network(settings, uuid, ssid)

    def _lock_is_for_this_network(self, settings: dict, uuid: str,
                                 active_ssid: str | None,
                                 timeout: int = 5) -> bool:
        """Whether the address pin was set for the network we are on now.

        The lock follows whichever profile NetworkManager actually uses, so
        the profile it was written to is often not the one in use - a second
        saved copy of the same network still counts as the locked network, and
        matching on the profile alone would report drift that nothing clears.

        A DIFFERENT network does not: leaving home with the lock still enabled
        would otherwise pin the user to the first access point of whatever
        network they joined next, which they never asked for and which blocks
        roaming on it. An SSID that cannot be read answers no, because holding
        off is the safe way to be wrong here.
        """
        locked_uuid = settings.get("bssid_lock_connection_uuid", "")
        if locked_uuid == uuid:
            return True
        if not locked_uuid:
            return False
        locked_ssid = self._get_profile_ssid(locked_uuid, timeout=timeout)
        return bool(locked_ssid and active_ssid and locked_ssid == active_ssid)

    @staticmethod
    def _parse_link(link_out: str) -> dict:
        """The live values `iw dev X link` reports, as the panel names them.

        Only what is present: a field the link does not mention is left out
        rather than published as empty, so the panel can tell "no signal
        reading" from "a signal reading of nothing".
        """
        live = {}
        for line in link_out.split("\n"):
            line = line.strip()
            if line.startswith("signal:"):
                live["signal_dbm"] = line.split(":", 1)[1].strip()
            elif "tx bitrate:" in line:
                live["tx_bitrate"] = line.split("tx bitrate:", 1)[1].strip()
            elif line.startswith("freq:"):
                live["frequency"] = line.split(":", 1)[1].strip()
            elif "Connected to" in line:
                parts = line.split()
                if len(parts) >= 3:
                    live["connected_bssid"] = parts[2]
        return live

    @staticmethod
    def _parse_channel(info_out: str) -> str | None:
        """Channel and width as the panel shows them, or None if not reported.

        Raw: "channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz" becomes
        "36 (80 MHz)". A line that cannot be taken apart is passed through
        whole rather than dropped - an unfamiliar format is worth showing.
        """
        channel = None
        for line in info_out.split("\n"):
            line = line.strip()
            if not line.startswith("channel"):
                continue
            parts = line.split(",")
            chan_num = ""
            width = ""
            if parts:
                tokens = parts[0].split()
                if len(tokens) >= 2:
                    chan_num = tokens[1]
            for part in parts:
                part = part.strip()
                if part.startswith("width:"):
                    width = part.split(":", 1)[1].strip()
            if chan_num and width:
                channel = f"{chan_num} ({width})"
            elif chan_num:
                channel = chan_num
            else:
                channel = line
        return channel

    # Where the kernel lists radio switches: each has a type, and soft and hard
    # block flags.
    RFKILL_DIR = "/sys/class/rfkill"

    def _report_not_connected(self, status: dict, iface: str | None, timeout: int):
        """The status's live part when WiFi is not connected: whether the
        dispatcher is installed, and why there is no connection, so a dead or
        missing card is not told to connect first (contract C1)."""
        status["live"]["dispatcher_installed"] = os.path.isfile(DISPATCHER_PATH)
        status["live"]["wifi_state"] = self._wifi_state(iface, timeout)

    def _report_connected(self, status: dict):
        """The status's live part when WiFi is connected (contract C1): the
        hold's record of a not-connected state is cleared, so the next wake
        starts its clock afresh, and a stuck iw is said even while
        NetworkManager still says connected (it reads its own state, not the
        card's). Otherwise wifi_state is left out."""
        self._wifi_state_seen = None
        if "iw" in self._stuck_tools():
            status["live"]["wifi_state"] = "not_responding"

    # How long "no_device", "unavailable" or "asleep" must hold before the
    # panel is told (contract C1). NetworkManager calls a card unavailable
    # while it has no supplicant yet (after a wake, during a backend
    # restart), and stopping iwd destroys the netdev on every radio but
    # rtw88 until it is recreated: normal moments, not a dead card.
    _WIFI_STATE_HOLD = 20
    # A sighting this much later than the last one of the same state is not
    # the same stretch: the panel was closed, or the device slept, between
    # them (two missed polls).
    _WIFI_STATE_GAP = 10

    def _wifi_state(self, iface: str | None, timeout: int = 2) -> str:
        """Why WiFi is not connected, for the panel (contract C1).

        "not_responding" when an earlier iw is still stuck in the driver and
        "radio_off" when WiFi is turned off (NetworkManager's radio or an
        rfkill block), both at once. "asleep" when NetworkManager is asleep
        or networking is off, "no_device" with no wireless interface and
        "unavailable" when NetworkManager lists the card as unavailable: those
        three only once the same one has held for _WIFI_STATE_HOLD seconds of
        monotonic time with no backend switch or update under way, and
        "disconnected" until then and otherwise. Uses only nmcli and sysfs:
        an iw run into a wedged driver is what would get stuck.
        """
        if "iw" in getattr(self, "_stuck_tools", lambda: set())():
            return "not_responding"
        state, radio = self._nm_general_state()
        if state == "asleep":
            return self._held_wifi_state("asleep")
        if not iface:
            # No interface named is no card only when sysfs lists no wireless
            # netdev at all: with two, the name comes from nmcli, and an
            # nmcli that failed says nothing about the card.
            if self._wireless_netdevs():
                return self._held_wifi_state(None)
            return self._held_wifi_state("no_device")
        if radio == "disabled" or self._wifi_rfkill_blocked():
            self._held_wifi_state(None)
            return "radio_off"
        devices = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-e", "no", "-f", "DEVICE,TYPE,STATE",
             "device", "status"],
            timeout=timeout,
        )
        for line in (devices.get("stdout") or "").splitlines() if devices.get("success") else []:
            parts = line.split(":")
            if len(parts) >= 3 and parts[0] == iface and parts[1] == "wifi":
                if parts[2].startswith("unavailable"):
                    return self._held_wifi_state("unavailable")
                break
        return self._held_wifi_state(None)

    def _held_wifi_state(self, state: str | None) -> str:
        """state once it has held long enough (see _wifi_state), else
        "disconnected". None records that no such state is showing.

        The record is (state, since, last seen). The clock starts again when
        the state changes, when the last sighting is more than
        _WIFI_STATE_GAP seconds old, and after any status pass that found
        WiFi connected (_report_connected), so a wake an hour after the last
        one does not inherit that one's clock."""
        now = time.monotonic()
        seen = getattr(self, "_wifi_state_seen", None)
        if state is None or self._system_change_in_flight():
            # A switch or an update takes WiFi down on purpose: the clock
            # starts again once it is over.
            self._wifi_state_seen = None
            return "disconnected"
        if (not seen or seen[0] != state or len(seen) < 3
                or now - seen[2] > self._WIFI_STATE_GAP):
            self._wifi_state_seen = (state, now, now)
            return "disconnected"
        self._wifi_state_seen = (state, seen[1], now)
        return state if now - seen[1] >= self._WIFI_STATE_HOLD else "disconnected"

    def _wifi_rfkill_blocked(self) -> bool:
        """Whether a WiFi radio switch is blocked, by software or by hardware."""
        try:
            entries = os.listdir(self.RFKILL_DIR)
        except OSError:
            return False
        for entry in entries:
            base = os.path.join(self.RFKILL_DIR, entry)

            def flag(name: str) -> str:
                try:
                    with open(os.path.join(base, name), "r") as f:
                        return f.read(16).strip()
                except OSError:
                    return ""

            if flag("type") == "wlan" and "1" in (flag("soft"), flag("hard")):
                return True
        return False

    def _status_read_failed(self) -> tuple[dict, dict, list]:
        """What to answer when the status read itself fell over.

        A COMPLETE shape, not a bare error dict. The panel reads settings,
        live, drift, connected and version unconditionally, so a bare error
        renders as "not connected" with every toggle off - telling the user
        their optimizations are gone rather than that the read failed.

        Nothing is proposed alongside it: a read that failed is no basis for
        deciding to write anything.
        """
        try:
            settings = _load_settings()
        except Exception:
            settings = copy.deepcopy(DEFAULT_SETTINGS)
        return (
            {
                "success": False,
                "error": "unexpected",
                "message": "Couldn't read WiFi status.",
                "connected": False,
                # Reads settings only, so it is still meaningful here -
                # hardcoding 3 made the panel announce unrecognised hardware
                # because an unrelated read failed.
                "support_tier": self._get_support_tier(),
                "version": getattr(decky, "DECKY_PLUGIN_VERSION", "?"),
                "settings": settings,
                "live": {},
                "drift": {},
                "external": {},
            },
            {},
            [],
        )

    def _power_save_drifted(self, settings: dict, ps_result: dict, ps_off: bool) -> bool:
        """Whether power save is on where the setting has it off. Only from
        an answer: a command that could not run (no iw on this system, or one
        still stuck) says nothing about power save, and reporting drift for
        it offered a Fix now that cannot fix it."""
        return bool(settings.get("power_save_disabled") and ps_result.get("success")
                    and not ps_off and self._volatile_gate_open(settings))

    def _collect_status(self) -> tuple[dict, dict, list]:
        # Short timeout for the queries that take one. Note this does not
        # bound the whole collection: the interface and connection lookups,
        # the backend probe and the qdisc check use their own longer defaults,
        # so a wedged NetworkManager can stretch a single pass to the better
        # part of a minute. That is survivable because collection runs in a
        # worker thread and the caller drops overlapping polls.
        T = 2

        try:
            settings = _load_settings()
            # Collection runs in a worker thread, so it must not mutate
            # anything. Settings changes and NetworkManager writes are both
            # PROPOSED here and carried out by get_status on the event loop,
            # where they are serialized against the setters. Doing them here
            # would race a setter and act on a snapshot up to ~20s stale.
            pending: dict = {}
            actions: list[dict] = []
            iface = self._get_wifi_interface()
            uuid = self._get_active_connection_uuid()
            connected = iface is not None and uuid is not None
            support_tier = self._get_support_tier()

            status = {
                "success": True,
                "connected": connected,
                "support_tier": support_tier,
                "version": decky.DECKY_PLUGIN_VERSION,
                "settings": settings,
                "live": {},
                "drift": {},
                "external": {},
            }

            self._collect_backend_state(status)
            settings = self._pins_for_backend(settings, status)

            # Leftover pins, proposed BEFORE the not-connected return. A pin
            # we failed to clear is what stops a profile associating, so the
            # user is most likely to be DISCONNECTED when it needs clearing -
            # and this needs neither an interface nor an active connection,
            # only a saved profile.
            actions.extend(self._propose_pin_cleanup(settings))
            # The case the cleanup above cannot reach: a pin whose feature is
            # still enabled, which is what actually strands a device.
            actions.extend(
                self._propose_stranded_pin_release(settings, connected)
            )
            released_at = getattr(self, "_pin_released_at", 0)
            if (
                released_at
                and time.time() - released_at < self._RELEASE_NOTICE_SECONDS
            ):
                status["external"]["pin_released"] = True

            if not connected:
                self._report_not_connected(status, iface, T)
                return status, pending, actions
            self._report_connected(status)

            self._propose_precedence(settings, uuid, pending, actions)

            # Three decisions below need the active profile's SSID, and two of
            # them often do not run, so it is read on first use and reused.
            resolved_ssid: str | None = None

            def active_ssid() -> str | None:
                """The SSID of the profile in use, read at most once a poll.

                A read that failed is deliberately not cached: None keeps
                meaning "could not tell", and the next caller is entitled to
                its own attempt, which is what the separate reads did.
                """
                nonlocal resolved_ssid
                if resolved_ssid is None:
                    resolved_ssid = self._get_profile_ssid(uuid, timeout=T)
                return resolved_ssid

            # Power save
            ps_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "get", "power_save"], timeout=T
            )
            ps_off = "Power save: off" in ps_result.get("stdout", "")
            status["live"]["power_save_off"] = ps_off
            if self._power_save_drifted(settings, ps_result, ps_off):
                status["drift"]["power_save"] = True

            # Link info
            link_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "link"], timeout=T
            )
            link_out = link_result.get("stdout", "")
            status["live"].update(self._parse_link(link_out))

            # Channel info - parse to "36 (80 MHz)" format
            info_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "info"], timeout=T
            )
            channel = self._parse_channel(info_result.get("stdout", ""))
            if channel is not None:
                status["live"]["channel"] = channel

            # BSSID lock. Read as 802-11-wireless.bssid:AA\:BB\:CC\:DD\:EE\:FF
            current_bssid_lock = self._read_profile_field(
                uuid, "802-11-wireless.bssid", timeout=T
            ).replace("\\", "")
            status["live"]["bssid_lock"] = current_bssid_lock
            if (current_bssid_lock and not settings.get("bssid_lock_enabled")
                    and "802-11-wireless.bssid" not in settings.get("pins_owned", [])):
                status["external"]["bssid_lock"] = True
            actions.extend(
                self._propose_active_pin_cleanup(
                    settings, uuid, "bssid_lock_uuids", current_bssid_lock
                )
            )
            if settings.get("bssid_lock_enabled") and not current_bssid_lock:
                # NetworkManager keeps more than one profile per SSID, and it
                # is free to autoconnect with a different one than the profile
                # the lock was written to. When that happens the lock is not
                # merely mis-reported, it is genuinely absent from the profile
                # in use, so background scanning is never actually suppressed.
                #
                # Re-point the lock at the active profile rather than reporting
                # drift that nothing clears. Writing the BSSID we are already
                # associated to does not disturb the link: NM applies it on the
                # next activation, so no reconnect is triggered here.
                # Resolved here, before the decision, because two things need
                # it: whether this is the locked network, and telling the
                # applier which network the pin would land on so it can honour
                # the band preference's scope. None means "could not tell",
                # and every consumer treats that as a reason to hold off.
                here = active_ssid()
                same_network = self._lock_is_for_this_network(
                    settings, uuid, here, timeout=T
                )

                live_bssid = status["live"].get("connected_bssid", "")
                if same_network:
                    if live_bssid:
                        actions.append({
                            "kind": "bssid_repoint", "uuid": uuid,
                            "value": live_bssid,
                            # Carried on the action rather than published in
                            # `live`: the applier is its only consumer and the
                            # panel has no use for it.
                            "active_ssid": here,
                        })
                    # Genuinely drifted: this IS the locked network and the
                    # lock is missing from the profile in use.
                    status["drift"]["bssid_lock"] = True
                else:
                    # A different network. The lock is not missing, it simply
                    # does not apply here, and saying otherwise offers a "Fix
                    # now" that would pin the user to the first access point
                    # of a network they never asked to lock.
                    status["live"]["bssid_lock_other_network"] = True

            status["live"].update(self._read_addressing(iface, timeout=T))

            # IPv6
            live_ipv6 = self._read_profile_field(uuid, "ipv6.method", timeout=T)
            status["live"]["ipv6_method"] = live_ipv6
            self._classify_ipv6(settings, uuid, live_ipv6, status, actions)
            self._check_dns_drift(settings, uuid, status, timeout=T)

            # Band preference
            live_band = self._read_profile_field(
                uuid, "802-11-wireless.band", timeout=T
            )
            status["live"]["band"] = live_band
            status["live"]["six_ghz_offered"] = self._six_ghz_offered(settings, status)
            if (live_band and not settings.get("band_preference_enabled")
                    and "802-11-wireless.band" not in settings.get("pins_owned", [])):
                status["external"]["band"] = True

            self._recheck_rival_profiles(settings, uuid, active_ssid, actions)
            if getattr(self, "_dup_conflict", False):
                status["external"]["duplicate_profiles"] = True
                # Only claimed once the copy in use really outranks the rest.
                status["duplicates_prioritized"] = bool(getattr(self, "_dup_outranks", False))
            self._report_network_problems(status, uuid, iface, active_ssid() or "", settings)
            self._report_lock_weaker(settings, status, uuid, iface,
                                     active_ssid() or "", link_out)
            actions.extend(
                self._propose_active_pin_cleanup(
                    settings, uuid, "band_preference_uuids", live_band
                )
            )
            actions.extend(self._propose_rival_pin_cleanup(settings))
            self._propose_band(settings, uuid, live_band, active_ssid,
                               status, actions)

            self._collect_host_tuning(settings, uuid, iface, status, actions, T)

            return status, pending, actions
        except Exception as e:
            decky.logger.error(f"get_status error: {e}")
            return self._status_read_failed()

    # ---- Optimization setters ----

    async def set_power_save(self, disabled: bool, *, reapply: bool = False) -> dict:
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is. reapply (keyword only, never
        # from the panel): a re-application of a setting already on, which
        # goes ahead only if the setting is still on when it gets its turn.
        disabled = disabled is True
        # One at a time. Turning it on awaits NetworkManager's reload, and an
        # off that finished during that wait was undone when the on path
        # went on to apply the fixes and save the setting on.
        async with self._power_save_guard():
            apply_lock = None
            try:
                if reapply and not self._reapply_wanted("power_save_disabled"):
                    # Read now, with the guard held: a Reset or the user's
                    # off that ran while this waited for its turn wins over
                    # settings the caller read before them.
                    return {"success": True, "skipped": True}
                # Uninstall under way: nothing is put on, and nothing of ours
                # is written again after it was removed.
                if disabled and getattr(self, "_tearing_down", False):
                    return self._busy_result()
                if disabled and self._paused_by_streaming():
                    return self._record_paused("power_save_disabled")
                # Turning it off can rewrite Valve's NetworkManager file, which a
                # backend switch rewrites too.
                if not disabled and self._backend_switch_running():
                    return self._busy_result()
                # Auto mode keeps power save to the session: nothing persistent.
                auto = bool(_load_settings().get("streaming_mode_enabled"))
                # Turning it off holds the apply lock from the save below until
                # everything is off, so a dispatcher run that read the old
                # settings cannot put the fixes back once they are off.
                if not disabled:
                    apply_lock = await asyncio.to_thread(_take_apply_lock)
                # Off is saved before anything comes off. The dispatcher and the
                # startup apply both act on the setting, so a reconnect landing
                # part way through put back what was being taken off, and the
                # fixes stayed on under a toggle that read off.
                was_on = False
                if not disabled:
                    settings = _load_settings()
                    was_on = bool(settings.get("power_save_disabled"))
                    if was_on:
                        settings["power_save_disabled"] = False
                        _save_settings(settings)
                # Whether our NetworkManager file is there to be removed. Only
                # while it is can a 2 in Valve's file be ours.
                ours = os.path.lexists(NM_CONF_PATH)

                # Every radio in use (contract C2), off the event loop: both
                # lookups can ask nmcli.
                status_if, managed_if = await asyncio.to_thread(self._power_save_radios)
                radios = [r for r in (status_if, managed_if) if r]

                # Apply immediately if connected - verify before saving. Turned
                # back on only where Steam's own WiFi power management setting
                # does not have it off, as Reset and the end of a stream do.
                iw_missing = False
                not_responding = False
                # Whether iw turned power save off on the managed card in this
                # call (contract C10): only then may the driver parameter that
                # needs it be written.
                ps_off = False
                if radios and (disabled or not self._valve_keeps_power_save_off(
                        _load_settings(), ours)):
                    result, ps_off = await asyncio.to_thread(
                        self._iw_power_save, disabled, status_if, managed_if)
                    if (not result["success"]
                            and result.get("stderr", "").startswith("Command not found")):
                        # No iw on this system (one CachyOS install had none).
                        # Nothing can change power save now, but the rest of the
                        # setting still applies, and asking the user to cycle
                        # WiFi would not bring iw back.
                        iw_missing = True
                        decky.logger.error("set_power_save: iw is not installed")
                    elif not result["success"] and self._card_not_responding(result):
                        # An iw stuck in the driver (a bad wake): cycling WiFi
                        # does not help, and refusing to turn the fix off left
                        # it on under a card that will not answer. Off goes
                        # on with everything else; on is refused.
                        self._log_detail("set_power_save", result["stderr"])
                        if disabled:
                            return {
                                "success": False,
                                "error": "not_responding",
                                "message": "The WiFi card isn't responding. Restart the device.",
                                "detail": result["stderr"],
                            }
                        not_responding = True
                    elif not result["success"]:
                        if was_on:
                            # Nothing was taken off, so it is still on.
                            settings = _load_settings()
                            settings["power_save_disabled"] = True
                            _save_settings(settings)
                        self._log_detail("set_power_save", result["stderr"])
                        return {
                            "success": False,
                            "error": "iw_failed",
                            "message": "Couldn't change WiFi power save. Turn WiFi off and on, then try again.",
                            "detail": result["stderr"],
                        }

                # Uninstall started while this ran: the files it removes are
                # not written again (it waits for this call only so long).
                tearing_down = getattr(self, "_tearing_down", False)
                # Write or remove NM config (persistent layer)
                before = _load_settings().get("valve_powersave_before")
                if disabled and not auto and not tearing_down:
                    # What Valve's file said before we first took power save
                    # over, so stopping can put it back. "" records "nothing".
                    if not os.path.exists(NM_CONF_PATH):
                        before = self._valve_powersave_record()
                    # NetworkManager keeps the config it read at startup, and
                    # turns power save back on at the next reconnect until it is
                    # told to read it again.
                    if _write_system_conf(NM_CONF_PATH, "[connection]\nwifi.powersave = 2\n"):
                        await asyncio.to_thread(self._reload_nm_conf)
                elif not disabled and ours:
                    # Only our own file can have put our value into Valve's. With
                    # none to remove (always so in streaming auto mode, which never
                    # writes it), a 2 there is the user's own Steam setting, and
                    # releasing it at the end of every stream turned it back on.
                    # Valve's line is put back before NetworkManager rereads its
                    # config, once, off the event loop: reloaded first, it read
                    # our 2 still there and kept power save off.
                    await asyncio.to_thread(self._release_power_save_files, _load_settings())
                    # Nothing of ours is left in Valve's file, so what it says
                    # now is the user's own. Recorded as such: a "?" here took a
                    # 2 the user later set in Steam for ours, and Reset then
                    # turned their setting back on.
                    before = self._valve_powersave_record()

                # Only when auto mode is putting the fixes on from off. Any other
                # call finds them already on, and would record our own values.
                record_all = auto and getattr(self, "_gate_opening", False)
                # In a worker thread: sysfs writes, and the managed card's
                # lookup can ask nmcli, against a driver that may be wedged.
                await asyncio.to_thread(
                    self._apply_driver_fixes, disabled, persist=not auto and not tearing_down,
                    record_all=record_all, ps_off=ps_off)
                not_applied = await asyncio.to_thread(
                    self._apply_pcie_aspm_fix, disabled, record_all=record_all)

                # Save settings only after success. A re-application leaves
                # "Last changed" alone: it is not a change of the user's.
                if not tearing_down:
                    settings = _load_settings()
                    settings["power_save_disabled"] = disabled
                    settings["valve_powersave_before"] = before
                    if reapply:
                        _save_settings(settings)
                    else:
                        _save_settings_with_timestamp(settings)

                result = {"success": True, "power_save_off": disabled}
                notes = []
                if not_responding:
                    notes.append(
                        "The WiFi card isn't responding, so power save couldn't be "
                        "turned back on now. Everything else is undone; restart the "
                        "device to finish."
                    )
                if iw_missing:
                    notes.append(
                        "The iw tool is missing on this system, so WiFi power save "
                        "can't be changed. The other fixes are "
                        + ("on." if disabled else "off.")
                    )
                if not_applied:
                    # WiFi power save itself is off, which is most of the benefit,
                    # but the toggle also promises the PCIe power states and some
                    # did not take. Said without their names, which mean nothing
                    # to the reader; _apply_pcie_aspm_fix logs them.
                    notes.append(
                        "Some PCIe power saving couldn't be turned off; the rest "
                        "is applied. Nothing to do."
                    )
                if notes:
                    result["message"] = " ".join(notes)
                return result
            except Exception as e:
                decky.logger.error(f"set_power_save error: {e}")
                return self._unexpected_response(e)
            finally:
                _drop_apply_lock(apply_lock)

    def _iw_power_save(self, disabled: bool, status_if: str | None,
                       managed_if: str | None) -> tuple:
        """iw's power save off (or on) on every radio in use (contract C2):
        the one the status reads it on, and the card the driver profile
        manages when that is another. (the result for the radio the status
        reads, which judges the call; whether it went off on the managed
        card, contract C10). Another radio failing is logged only: the
        status says so there."""
        state = "off" if disabled else "on"
        results = {}
        for radio in dict.fromkeys(r for r in (status_if, managed_if) if r):
            results[radio] = self._run_cmd(
                ["/usr/bin/iw", "dev", radio, "set", "power_save", state])
        judged = status_if or managed_if
        for radio, other in results.items():
            if radio != judged and not other["success"]:
                decky.logger.error(
                    f"set_power_save: iw on the other radio: {_tool_text(other['stderr'][:120])}")
        ps_off = bool(disabled and managed_if and results[managed_if]["success"])
        return results[judged], ps_off

    def _power_save_radios(self) -> tuple:
        """(the radio the status reads power save on, the card the driver
        profile manages) for iw's power save (contract C2). The same netdev
        twice when there is one radio; either can be None. Valid names only."""
        status_if = self._get_wifi_interface()
        managed_if = getattr(self, "_managed_interface", self._get_wifi_interface)()
        return tuple(r if r and IFACE_RE.match(r) else None for r in (status_if, managed_if))

    def _reapply_wanted(self, key: str) -> bool:
        """For a re-application (reapply=True): whether the setting is still
        on, the fixes are in force (not paused by auto mode) and no profile
        change (a Reset, a lock or band change) or backend switch is under
        way. From a read made now, never from the caller's earlier copy."""
        if self._profile_change_in_flight() or self._backend_switch_running():
            return False
        if getattr(self, "_tearing_down", False):
            return False
        settings = _load_settings()
        return bool(settings.get(key)) and self._volatile_gate_open(settings)

    @staticmethod
    def _not_responding_result(what: str, detail: str) -> dict:
        """The answer for an iw stuck in the driver, logged once here."""
        decky.logger.error(f"{what}: the card is not responding ({_tool_text(detail[:200])})")
        return {
            "success": False,
            "error": "not_responding",
            "message": "The WiFi card isn't responding. Restart the device.",
            "detail": detail,
        }

    @staticmethod
    def _card_not_responding(result: dict) -> bool:
        """Whether a failed iw call means the card is not answering at all:
        an earlier iw is still stuck in the driver, or this one overran."""
        stderr = result.get("stderr") or ""
        return stderr.endswith("is still stuck") or stderr == "Command timed out"

    def _power_save_guard(self) -> asyncio.Lock:
        """Held for the whole of set_power_save, which never calls itself."""
        if getattr(self, "_power_save_lock", None) is None:
            self._power_save_lock = asyncio.Lock()
        return self._power_save_lock

    def _valve_keeps_power_save_off(self, settings: dict, ours: bool) -> bool:
        """Whether Valve's file holds power save off by the user's choice
        (Steam's WiFi power management off), once ours is released: a 2
        there is theirs unless our file is in place and they had none."""
        if self._valve_powersave() != "2":
            return False
        return not ours or settings.get("valve_powersave_before") == "2"

    async def set_auto_fix(self, enabled: bool) -> dict:
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        enabled = enabled is True
        try:

            settings = _load_settings()

            installed = True
            removed = True
            if enabled:
                installed = self._install_dispatcher()
            else:
                removed = self._remove_dispatcher()

            # Only record the setting as on once the script is actually in
            # place. Saving first left the toggle showing on, and the header
            # claiming a recent change, while nothing had been installed.
            # And as off only once it is gone: a script that could not be
            # removed still runs on every connect, so the toggle should keep
            # saying so and let the user try again.
            settings["auto_fix_on_wake"] = installed if enabled else not removed
            _save_settings_with_timestamp(settings)
            if not enabled and not removed:
                # The toggle would otherwise read off while the script stays
                # on disk, still run by NetworkManager as root on every
                # connect - the opposite of what the user asked for.
                return {
                    "success": False,
                    "error": "write_failed",
                    "message": "Couldn't remove the auto-fix script. Restart the device, then try again.",
                }
            if enabled and not installed:
                # os.path.isfile is not proof of success here: a failed write
                # leaves the PREVIOUS script in place, so the check passes
                # while the old one is what actually runs.
                return {
                    "success": False,
                    "error": "write_failed",
                    "message": "Couldn't install the auto-fix script. The filesystem may be read-only.",
                }
            return {
                "success": True,
                "dispatcher_installed": os.path.isfile(DISPATCHER_PATH),
            }
        except Exception as e:
            decky.logger.error(f"set_auto_fix error: {e}")
            if isinstance(e, SettingsSaveError):
                return self._unexpected_response(e)
            return {
                "success": False,
                "error": "write_failed",
                "message": "Couldn't change the auto-fix script. Restart the device, then try again.",
            }

    async def _release_lock_from_all_profiles(self) -> dict | None:
        """Take the address pin off every profile that still carries it.

        Returns an error dict if the pin could not be released, None if it was.

        Works on saved profiles, so it needs no active WiFi - being unable to
        associate is one of the likelier reasons somebody is turning the lock
        off, and refusing without a live connection would leave the pin in
        place with no way to clear it.

        The pin follows whichever profile NetworkManager uses, so over time it
        may have been written to several copies of the same network. All of
        them are cleared: one left pinned to an access point we are no longer
        honouring will fail to associate if NM ever picks it again, and no
        control would clear it.

        The pin writes run in a worker thread (up to sixteen nmcli calls held
        the event loop), and the settings that record them are read fresh
        just before the save, so nothing saved meanwhile is overwritten.
        Then the reconnect, in a worker thread too.
        """
        error, uuid = await asyncio.to_thread(self._release_lock_pins)
        if error:
            return error
        if not await self._reconnect(uuid):
            return dict(self._RADIO_OFF_RESULT)

        return None

    def _release_lock_pins(self) -> tuple:
        """_release_lock_from_all_profiles's pin writes and save, in a worker
        thread. (an error dict or None, the profile in use)."""
        # Disabling works on saved profiles - no active WiFi needed
        _iface, uuid, _err = self._require_wifi()
        if not uuid:
            uuid = self._get_saved_connection_uuid()
        if not uuid:
            return {
                "success": False,
                "error": "nmcli_failed",
                "message": "Connect to WiFi first.",
            }, None

        result = self._nmcli_modify(uuid, "802-11-wireless.bssid", "")
        if not result["success"]:
            self._log_detail("_release_lock_from_all_profiles", result["stderr"])
            return {
                "success": False,
                "error": "nmcli_failed",
                "message": "Couldn't release the access point lock. Try again in a moment.",
                "detail": result["stderr"],
            }, None

        settings = _load_settings()

        # The lock follows whichever profile NM actually uses, so over
        # time it may have been written to several profiles for the
        # same SSID. Clear all of them: one left pinned to an access
        # point we are no longer honouring will fail to associate if
        # NM ever picks it again, and no control would clear it.
        stale_uuids = [
            u for u in settings.get("bssid_lock_uuids", []) if u != uuid
        ]
        previous_uuid = settings.get("bssid_lock_connection_uuid", "")
        if previous_uuid and previous_uuid != uuid and previous_uuid not in stale_uuids:
            stale_uuids.append(previous_uuid)
        # The profile last in use too, once this install has written a BSSID
        # pin, as Reset and uninstall sweep it.
        last = settings.get("last_connection_uuid", "")
        if (last and last != uuid and last not in stale_uuids
                and "802-11-wireless.bssid" in settings.get("pins_owned", [])):
            stale_uuids.append(last)
        unresolved = []
        for stale_uuid in stale_uuids:
            stale = self._nmcli_modify(
                stale_uuid, "802-11-wireless.bssid", "", timeout=2
            )
            if stale["success"]:
                decky.logger.info(
                    f"Cleared stale BSSID lock from profile {stale_uuid}"
                )
                continue
            detail = stale.get("stderr", "")
            decky.logger.info(
                f"Could not clear stale BSSID lock from profile "
                f"{stale_uuid}: {_tool_text(detail[:120])}"
            )
            # A profile that is merely gone needs no further thought.
            # Anything else is still pinned, so keep the record of it
            # rather than forgetting the only reference to a profile
            # that will now fail to associate.
            if "unknown connection" not in detail.lower():
                unresolved.append(stale_uuid)

        # Read again for the save: the writes above took a while. A profile
        # recorded meanwhile, which none of them touched, stays recorded.
        tried = set(stale_uuids) | {uuid}
        settings = _load_settings()
        added = [u for u in settings.get("bssid_lock_uuids", []) if u not in tried]
        settings["bssid_lock_enabled"] = False
        settings["bssid_lock_value"] = ""
        settings["bssid_lock_connection_uuid"] = ""
        settings["bssid_lock_uuids"] = unresolved + [u for u in added if u not in unresolved]
        _save_settings_with_timestamp(settings)
        return None, uuid

    def _settle_band_before_locking(self, current: dict, iface: str, uuid: str,
                                    bssid: str, link_out: str,
                                    active_ssid: str | None) -> tuple:
        """Get onto the preferred band before pinning, or refuse to pin.

        Returns (error, iface, bssid, link_out): an error dict if locking must
        not go ahead, otherwise None and whatever the reconnect left us on -
        the interface can come back renamed and the access point will differ.

        Refusing rather than writing a profile that cannot associate. This is
        reachable from the drift banner's Fix now, which runs Optimize Safe and
        reconnects immediately after - so getting it wrong strands the user
        then and there rather than at their next wake.

        Runs in a worker thread: a scan here can take 15 s and a radio cycle
        more, which used to freeze every other call for as long. The setter
        holds the reconciliation guard across it, so no poll writes to the
        profile mid-change, and no second profile setter can start.
        """
        frequency = ""
        for line in link_out.split("\n"):
            line = line.strip()
            if line.startswith("freq:"):
                frequency = line.split(":", 1)[1].strip()
                break
        if self._band_conflicts(current, frequency, active_ssid):
            # Refusing alone would be a dead end: this is exactly the
            # state the drift banner reports, its Fix now lands here,
            # and nothing the panel offers moves the association.
            #
            # But cycling the radio can also fail to get back, and a
            # remedy must not be able to leave the user worse off than
            # it found them. Only reconnect if an access point on the
            # wanted band is actually visible for this network.
            want = current.get("band_preference")
            if not self._band_is_reachable(iface, uuid, want):
                name = BAND_NAMES.get(want, want)
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": (
                        f"No {name} access point is in range for this "
                        f"network, which it is set to require. Turn the "
                        f"band preference off to lock anyway."
                    ),
                }, iface, bssid, link_out
            decky.logger.info(
                "Band conflict before locking; reconnecting to let NM "
                "pick an access point on the preferred band"
            )
            if not self._hard_reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT), iface, bssid, link_out
            # Blocking is fine: this runs in a worker thread.
            time.sleep(3)
            iface = self._get_wifi_interface() or iface
            link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
            if not link_result["success"]:
                # Same trap as the wait above: an unread link is not a
                # band that could not be reached, and reporting it as
                # one sends the user to change a setting that was
                # never the problem.
                detail = (
                    link_result.get("stderr", "")
                    or f"iw exited {link_result.get('returncode')}"
                )
                decky.logger.error(
                    f"BSSID lock: could not re-read the link on "
                    f"{iface} after the band reconnect - {_tool_text(detail[:200])}"
                )
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": ("Couldn't read the WiFi link. Try again in a moment. "
                                "If it keeps failing, restart the device."),
                    "detail": detail,
                }, iface, bssid, link_out
            link_out = link_result.get("stdout", "")
            relinked = self._parse_link(link_out)
            frequency = relinked.get("frequency", "")
            bssid = relinked.get("connected_bssid", "")

            if not bssid or self._band_conflicts(
                current, frequency, active_ssid
            ):
                want = current.get("band_preference")
                name = BAND_NAMES.get(want, want)
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": (
                        f"Couldn't reach an access point on {name}, "
                        f"which this network is set to require. Turn "
                        f"the band preference off to lock anyway."
                    ),
                }, iface, bssid, link_out

        return None, iface, bssid, link_out

    async def set_bssid_lock(self, enabled: bool) -> dict:
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        enabled = enabled is True
        # Held for the whole call. The enable path awaits while it waits for
        # an association, so a status poll can now land in the middle of this
        # setter where previously none could - and the one it would run is
        # exactly the re-point that writes a BSSID to this profile.
        if self._profile_change_refused():
            return self._busy_result()
        self._profile_change_depth = getattr(
            self, "_profile_change_depth", 0
        ) + 1
        try:

            if enabled:
                if await asyncio.to_thread(self._backend_is_iwd):
                    return {"success": False, "error": "needs_wpa_supplicant",
                            "message": "The access point lock " + self._NEEDS_WPA_SUPPLICANT}
                # Enabling requires active WiFi to read current BSSID
                iface, uuid, err = self._require_wifi()
                if err:
                    return err
                return await self._lock_onto_best(iface, uuid, moving=False)

            err = await self._release_lock_from_all_profiles()
            if err:
                return err
            # Said in the log because the panel only shows the toggle as it is
            # now. After a bad stretch of WiFi the owner could not tell from
            # the log whether he had turned the lock off or something else had.
            decky.logger.info("BSSID lock turned off")
            return {"success": True, "bssid_locked": False, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_bssid_lock error: {e}")
            return self._unexpected_response(e)
        finally:
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    async def _lock_onto_best(self, iface: str, uuid: str, moving: bool,
                              after_wake: bool = False) -> dict:
        """Pin the best access point of this network, and check it took.

        Both turning the lock on and moving it come through here, so the two
        cannot drift apart on which access point they choose or on how they
        recover when it will not have us. The caller holds the reconciliation
        guard across the whole of it.

        Turning the lock on pins where we are when nothing is clearly better.
        A move does not: the user asked for a stronger access point, and
        cycling the radio only to land back on the same one is a reconnect
        for nothing.
        """
        # Wait for an association rather than demanding one instantly.
        # Turning the lock OFF ends with a radio cycle, so turning it
        # straight back on arrives while the interface is still
        # re-associating and there is no "Connected to" line yet - the
        # user sees an error for a network they are plainly on.
        #
        # MEASURED on a Deck OLED against wpa_supplicant, from the
        # NetworkManager journal: getting back to activated after a
        # radio cycle took 13 s, 13 s and 24 s, and the window where
        # NM already reports the connection active while iw still has
        # no "Connected to" line ran about 20 s. A six-second budget
        # was shorter than every one of those, so it still failed on
        # the hardware it was written for. wpa_supplicant is the slow
        # backend to reassociate and it is kept on purpose for
        # streaming, so this has to cover it rather than avoid it.
        #
        # This awaits instead of blocking: half a minute of blocked
        # event loop would freeze the whole panel. What kept the
        # blocking version safe was that no status poll could run
        # inside it; the reconciliation guard held across this call
        # buys the same thing, by stopping a poll re-pointing the
        # lock while the change is in flight.
        assoc = await self._await_association(iface)
        iface, bssid = assoc["iface"], assoc["bssid"]
        link_out, read_error = assoc["link"], assoc["error"]

        if not bssid:
            if read_error and self._card_not_responding({"stderr": read_error}):
                decky.logger.error(f"BSSID lock: the card is not responding - {_tool_text(read_error[:200])}")
                return {
                    "success": False,
                    "error": "not_responding",
                    "message": "The WiFi card isn't responding. Restart the device.",
                    "detail": read_error,
                }
            if read_error:
                decky.logger.error(
                    f"BSSID lock: could not read the link on {iface} "
                    f"- {_tool_text(read_error[:200])}"
                )
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": ("Couldn't read the WiFi link. Try again in a moment. "
                                "If it keeps failing, restart the device."),
                    "detail": read_error,
                }
            decky.logger.info(
                f"BSSID lock: {iface} did not associate within "
                f"{self._ASSOCIATION_WAIT_SECONDS:.0f}s; not locking"
            )
            return {
                "success": False,
                "error": "no_wifi",
                "message": "WiFi hasn't finished reconnecting. Wait a moment and try again.",
            }

        # Refuse rather than write a profile that cannot associate.
        # This is reachable from the drift banner's Fix now, which
        # runs Optimize Safe, and it reconnects immediately after -
        # so getting it wrong strands the user then and there rather
        # than at their next wake.
        current = _load_settings()
        active_ssid = self._get_profile_ssid(uuid)
        err, iface, bssid, link_out = await asyncio.to_thread(
            self._settle_band_before_locking,
            current, iface, uuid, bssid, link_out, active_ssid,
        )
        if err:
            return err
        # Look before pinning. Locking whichever access point we happen to
        # be on is how a passing bad choice becomes a permanent one, and on
        # a mesh that is the common case rather than the corner one.
        # A scan can take seconds, so it runs off the event loop.
        # The reconciliation guard is held across this whole setter,
        # which is what makes awaiting here safe.
        moved_to, here_signal = await asyncio.to_thread(
            self._lock_target,
            iface, active_ssid or "", bssid, _load_settings(), link_out, uuid,
        )
        # after_wake: the move a wake made on its own (_move_lock_after_wake).
        verb = "After waking, moving" if after_wake else "Moving"
        if moving and not moved_to:
            decky.logger.info(
                f"{'After waking, not' if after_wake else 'Not'} moving the BSSID lock: "
                f"nothing clearly stronger than {bssid} (signal {here_signal}) on a fresh look"
            )
            return {
                "success": False,
                "error": "no_better_ap",
                "message": (
                    "Looked again and found no access point clearly stronger "
                    "than this one, so the lock stayed where it is."
                ),
            }
        chosen = moved_to[0] if moved_to else bssid
        if moving:
            decky.logger.info(
                f"{verb} the BSSID lock from {bssid} (signal {here_signal}) "
                f"to {chosen} (signal {moved_to[1]})"
            )
        elif moved_to:
            decky.logger.info(
                f"Locking to {chosen} (signal {moved_to[1]}) rather "
                f"than {bssid} (signal {here_signal}), which this network "
                f"also offers more strongly elsewhere"
            )

        result = self._nmcli_modify(uuid, "802-11-wireless.bssid", chosen)
        if not result["success"]:
            return {
                "success": False,
                "error": "nmcli_failed",
                "message": "Couldn't lock to this access point. Try again in a moment.",
                "detail": result["stderr"],
            }

        settings = _load_settings()
        settings["bssid_lock_enabled"] = True
        settings["bssid_lock_value"] = chosen
        settings["bssid_lock_connection_uuid"] = uuid
        settings["pins_owned"] = _with_pin(settings, "802-11-wireless.bssid")
        known = list(settings.get("bssid_lock_uuids", []))
        if uuid not in known:
            known.append(uuid)
        settings["bssid_lock_uuids"] = self._capped(known, "802-11-wireless.bssid")
        _save_settings_with_timestamp(settings)
        if not await self._reconnect(uuid):
            return dict(self._RADIO_OFF_RESULT)

        if moved_to:
            return await self._verify_lock_move(iface, uuid, bssid, chosen, moving)
        return {"success": True, "bssid_locked": True, "reconnected": True}

    async def _verify_lock_move(self, iface: str, uuid: str, previous: str,
                                chosen: str, moving: bool) -> dict:
        """Check a moved lock landed, and put the previous pin back if not.

        Moving is the one path that can strand the user: the access point we
        pinned was visible in a scan, which is not the same as being
        associable. If it did not take, put back the one they were
        demonstrably on rather than leaving a profile pinned to somewhere
        unreachable, and remember the refusal so the next attempt does not
        repeat it.
        """
        landed = await self._await_association(iface)
        if landed["bssid"].upper() == chosen.upper():
            return {"success": True, "bssid_locked": True, "reconnected": True}
        if self._card_not_responding({"stderr": landed.get("error", "")}):
            # An iw stuck in the driver says nothing about the access point:
            # it is not recorded as refusing us, and the pin is left alone.
            # Restoring and reconnecting would only queue more commands
            # behind the stuck one.
            return self._not_responding_result("BSSID lock move", landed["error"])
        decky.logger.error(
            f"Could not associate to {chosen} after moving "
            f"the lock; restoring {previous} and leaving it "
            f"alone for a while"
        )
        self._record_ap_failure(chosen)
        put_back = self._nmcli_modify(uuid, "802-11-wireless.bssid", previous)
        restored = _load_settings()
        if not put_back["success"]:
            # Still pinned to the access point that refused us. Take the pin
            # off instead, and say the lock is off, rather than reconnect into
            # a profile no access point will accept.
            decky.logger.error(
                f"Could not restore the lock to {previous} on {uuid}: "
                f"{_tool_text(put_back.get('stderr', '')[:120])}; turning the lock off"
            )
            cleared = self._nmcli_modify(uuid, "802-11-wireless.bssid", "")
            if cleared["success"]:
                restored["bssid_lock_enabled"] = False
                restored["bssid_lock_value"] = ""
                restored["bssid_lock_connection_uuid"] = ""
                message = ("The stronger access point would not accept the "
                           "connection, and the lock could not go back, so it "
                           "was turned off.")
            else:
                # Left as it really is; the stranded-pin release takes it off
                # once the device has been offline long enough.
                restored["bssid_lock_value"] = chosen
                message = ("The stronger access point would not accept the "
                           "connection, and the lock could not be changed. "
                           "Turn the lock off to release it.")
            _save_settings_with_timestamp(restored)
            if not await self._reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)
            return {"success": False, "error": "move_failed",
                    "reconnected": True, "message": message}
        restored["bssid_lock_value"] = previous
        _save_settings_with_timestamp(restored)
        if not await self._reconnect(uuid):
            return dict(self._RADIO_OFF_RESULT)
        if moving:
            # A move the user asked for that did not happen is a failure to
            # them, whatever state it left behind, so it is reported as one.
            return {
                "success": False,
                "error": "move_failed",
                "reconnected": True,
                "message": (
                    "The stronger access point would not accept the "
                    "connection, so the lock went back to the one you "
                    "were on."
                ),
            }
        return {
            "success": True,
            "bssid_locked": True,
            "reconnected": True,
            "message": (
                "Locked to the access point you were already "
                "on. A stronger one is in range but could not "
                "be reached."
            ),
        }

    async def move_bssid_lock(self) -> dict:
        """Move the lock to a stronger access point of the same network.

        Only ever on request, from the panel's notice (see
        _recheck_lock_weaker). It chooses again with the same rules as
        turning the lock on, so it takes the best access point on a fresh
        look rather than the one a cached scan pointed at, and it rolls back
        the same way if that one will not have us.
        """
        if self._profile_change_refused():
            return self._busy_result()
        self._profile_change_depth = getattr(
            self, "_profile_change_depth", 0
        ) + 1
        try:
            if await asyncio.to_thread(self._backend_is_iwd):
                return {"success": False, "error": "needs_wpa_supplicant",
                        "message": "The access point lock " + self._NEEDS_WPA_SUPPLICANT}
            iface, uuid, err = self._require_wifi()
            if err:
                return err
            settings = _load_settings()
            # The lock on, and for this network. Moving it anywhere else would
            # pin a network the user never locked.
            if not settings.get("bssid_lock_enabled") or not self._lock_is_for_this_network(
                settings, uuid, self._get_profile_ssid(uuid)
            ):
                return {"success": False, "error": "not_locked",
                        "message": "The access point lock isn't on for this network."}
            return await self._lock_onto_best(iface, uuid, moving=True)
        except Exception as e:
            decky.logger.error(f"move_bssid_lock error: {e}")
            return self._unexpected_response(e)
        finally:
            # Whatever happened, the notice that led here no longer describes
            # the link, so it starts over and must hold on two reads again.
            self._forget_lock_weaker()
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    def _record_relock(self, bssid: str):
        """Record the address _relock_pin wrote. On the event loop: a save
        from a worker thread races every setter's save there."""
        settings = _load_settings()
        settings["bssid_lock_value"] = bssid
        _save_settings(settings)
        decky.logger.info(f"Re-locked BSSID to {bssid} after band change")

    def _relock_pin(self, uuid: str) -> str | None:
        """Pin the access point NetworkManager chose on the new band, and
        return its address, or None when nothing was pinned.

        Only while the lock is still on: the band setter awaits before this,
        and the lock can have been turned off in between. Makes several
        subprocess calls, so the setters run it in a worker thread.
        """
        if not _load_settings().get("bssid_lock_enabled"):
            return None
        iface = self._get_wifi_interface()
        # The reconnect may have landed on a different profile - a
        # duplicate for the same network, which is the situation this
        # plugin exists to handle. Pinning what we see onto the profile
        # we meant to change would write a foreign address, possibly on
        # the other band, into a profile that now demands this one.
        landed_on = self._get_active_connection_uuid()
        if not iface or landed_on != uuid:
            return None
        link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
        if not link_result["success"]:
            # Not fatal - reconciliation re-points the lock on a
            # later poll - but silence here left the lock enabled
            # in settings and absent from the profile with nothing
            # said about why.
            decky.logger.error(
                f"Band change: could not read the link on {iface} "
                f"to restore the access point lock - "
                f"{_tool_text((link_result.get('stderr', '') or '')[:200])}"
            )
        link_text = link_result.get("stdout", "")
        seen_freq = ""
        for line in link_text.split("\n"):
            st = line.strip()
            if st.startswith("freq:"):
                seen_freq = st.split(":", 1)[1].strip()
        if self._band_conflicts(_load_settings(), seen_freq, None):
            decky.logger.info(
                "Not re-locking after the band change: the radio is "
                "not on the band this network now requires"
            )
            link_text = ""
        for line in link_text.split("\n"):
            if "Connected to" in line:
                parts = line.split()
                if len(parts) >= 3:
                    new_bssid = parts[2]
                    relocked = self._nmcli_modify(
                        uuid, "802-11-wireless.bssid", new_bssid
                    )
                    if relocked["success"]:
                        return new_bssid
                    # Recording a lock that was not applied
                    # would report it as present forever.
                    decky.logger.error(
                        f"Could not re-lock BSSID after band change: "
                        f"{_tool_text(relocked.get('stderr', '')[:120])}"
                    )
                break
        return None

    def _nm_version(self) -> tuple[int, int, int] | None:
        """NetworkManager's version as (major, minor, micro), None if unread.

        Read once and kept, since it only changes with an OS update, which
        restarts the plugin. A failed read is not kept, so the next caller
        tries again. _main warms it, so the status collector can read
        _nm_version_seen without forking. Release candidates print as
        "1.58-rc1", which counts as 1.58.0.
        """
        if hasattr(self, "_nm_version_seen"):
            return self._nm_version_seen
        out = self._run_cmd(["/usr/bin/nmcli", "--version"], timeout=3)
        if not out["success"]:
            return None
        found = re.search(r"version (\d+)\.(\d+)(?:\.(\d+))?", out.get("stdout", ""))
        self._nm_version_seen = (
            (int(found.group(1)), int(found.group(2)), int(found.group(3) or 0))
            if found else None
        )
        return self._nm_version_seen

    def _six_ghz_offered(self, settings: dict, status: dict) -> bool:
        """Whether the panel offers Force 6 GHz: the card and NetworkManager
        can do it. From memoised values only, since the status collector runs
        this. The setter checks the rest (_six_ghz_refusal) with a reason the
        user can act on."""
        version = getattr(self, "_nm_version_seen", None)
        return bool(
            settings.get("supports_6ghz")
            and version and version >= NM_6GHZ_BAND
            and status.get("live", {}).get("wifi_backend") != "iwd"
        )

    def _six_ghz_refusal(self, uuid: str) -> dict | None:
        """Why this profile cannot be held to 6 GHz here, or None if it can.

        Every 6 GHz access point uses WPA3. A profile saved for WPA3 (sae)
        joins one on any NetworkManager that has the band. One saved the
        usual SteamOS way (wpa-psk) only joins it on 1.58.1 or later, and
        only with protected management frames allowed: 1.58.0 lets it
        connect when asked but never on its own, so after sleep or a dropout
        it would stay disconnected. Anything else, open or enterprise, is
        refused rather than guessed at.
        """
        def refuse(message):
            decky.logger.info(f"Refusing band '6GHz' on {uuid}: {message}")
            return {"success": False, "error": "band_unavailable", "message": message}

        settings = _load_settings()
        if not settings.get("supports_6ghz"):
            return refuse("This device's WiFi card doesn't support 6 GHz.")
        version = self._nm_version()
        steamos = settings.get("distro_id") == "steamos"
        if version is None or version < NM_6GHZ_BAND:
            return refuse(
                "6 GHz needs NetworkManager 1.58 or newer"
                + (", expected with SteamOS 3.9." if steamos else ".")
            )
        key_mgmt = self._profile_key_mgmt(uuid)
        if key_mgmt == "sae":
            return None
        if key_mgmt != "wpa-psk":
            return refuse("6 GHz needs a network secured with a WPA3 password.")
        if version < NM_6GHZ_WPA_PSK:
            # The profile keeps the security it was saved with: setting the
            # router to WPA3 only later does not change it. NetworkManager
            # 1.58.0 saves a network that offers only WPA3 as sae, so joining
            # it again after the router change is the way on there.
            return refuse(
                "This network was saved while it allowed WPA2, and on this "
                "NetworkManager version 6 GHz needs it saved for WPA3. If your "
                "router is set to WPA3 only, forget the network and join it "
                "again. NetworkManager 1.58.1 or newer does not need this."
            )
        pmf = self._read_profile_field(uuid, "802-11-wireless-security.pmf")
        if pmf.split(" ")[0] in ("1", "disable"):
            return refuse(
                "This network is saved with protected management frames "
                "turned off, and 6 GHz requires them. Change it in Desktop "
                "Mode's network settings to use 6 GHz."
            )
        return None

    # How long the reassertion trusts an answer from _six_ghz_refusal. It
    # costs up to three nmcli calls on the event loop, and a profile's
    # security changes rarely.
    _SIX_GHZ_CHECK_SECONDS = 600

    def _six_ghz_refusal_cached(self, uuid: str) -> dict | None:
        """_six_ghz_refusal for the status pass, asked once per profile a while."""
        now = time.monotonic()
        seen = getattr(self, "_six_ghz_checked", {})
        if uuid in seen and now - seen[uuid][0] < self._SIX_GHZ_CHECK_SECONDS:
            return seen[uuid][1]
        refused = self._six_ghz_refusal(uuid)
        self._six_ghz_checked = {**seen, uuid: (now, refused)}
        return refused

    def _profile_band(self, uuid: str) -> str | None:
        """The band this profile holds now ("" for none), or None if unreadable."""
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "802-11-wireless.band",
             "con", "show", "uuid", uuid],
            timeout=5,
        )
        _, sep, value = result.get("stdout", "").partition(":")
        value = value.strip()
        if not result["success"] or not sep or (value and value not in BAND_NAMES):
            return None
        return value

    async def _confirm_six_ghz(self, uuid: str, before: dict,
                               had_lock: bool, previous_band: str) -> dict | None:
        """Check the reconnect landed on 6 GHz, and put things back if not.

        None when it did. Otherwise the band, and the settings that record
        it, go back to what they were before, the device reconnects, and the
        lock (if it was on) re-pins where it lands. A 6 GHz access point that
        showed in the scan can still refuse us, a 64-character hex key cannot
        be used for WPA3, and this is checked here rather than left for the
        stranded-pin release a minute later.
        """
        iface = await asyncio.to_thread(self._get_wifi_interface)
        seen = await self._await_association(iface) if iface else {"link": "", "error": "no interface"}
        frequency = self._parse_link(seen.get("link", "")).get("frequency", "")
        if (self._freq_band(frequency) == "6GHz"
                and await asyncio.to_thread(self._get_active_connection_uuid) == uuid):
            return None
        if self._card_not_responding({"stderr": seen.get("error", "")}):
            # Not the band's fault: the card is not answering at all. The
            # band is left as set rather than blamed and put back.
            return self._not_responding_result("Band '6GHz'", seen["error"])
        decky.logger.error(
            f"Band '6GHz' on {uuid} did not connect on 6 GHz "
            f"(frequency {frequency!r}{_tool_text(', ' + seen['error'] if seen.get('error') else '')}); "
            f"putting the band back"
        )
        undone = self._nmcli_modify(uuid, "802-11-wireless.band", previous_band)
        if not undone["success"]:
            decky.logger.error(
                f"Could not put the band back on {uuid}: {_tool_text(undone.get('stderr', '')[:120])}"
            )
        settings = _load_settings()
        for key in ("band_preference_enabled", "band_preference",
                    "band_preference_uuids", "band_preference_ssid", "pins_owned"):
            settings[key] = copy.deepcopy(before.get(key, DEFAULT_SETTINGS[key]))
        _save_settings_with_timestamp(settings)
        if not await self._reconnect(uuid):
            return dict(self._RADIO_OFF_RESULT)
        if had_lock:
            await asyncio.sleep(3)
            pinned = await asyncio.to_thread(self._relock_pin, uuid)
            if pinned:
                self._record_relock(pinned)
        return {
            "success": False,
            "error": "band_unavailable",
            "message": "Couldn't connect on 6 GHz, so the band was put back to how it was.",
            "reconnected": True,
        }

    async def set_band_preference(self, enabled: bool, band: str = "a") -> dict:
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        enabled = enabled is True
        # This setter deliberately clears the BSSID, cycles the radio and
        # re-locks once NM associates on the new band. It awaits in the
        # middle, so a status poll can land between those steps. Hold the
        # window so reconciliation does not write the old BSSID back and
        # leave the profile demanding a band and an AP that contradict.
        #
        # The counter is the whole gate and `finally` is what releases
        # it, including on cancellation. An earlier version put a deadline
        # behind it as a failsafe; a slow NetworkManager could outlive
        # that deadline and it failed open in exactly the circumstance
        # where reassociation is slowest and the race most likely.
        if self._profile_change_refused():
            return self._busy_result()
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        try:

            if band not in BAND_NAMES:
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Choose 5 GHz, 2.4 GHz or 6 GHz.",
                }
            if enabled and await asyncio.to_thread(self._backend_is_iwd):
                return {"success": False, "error": "needs_wpa_supplicant",
                        "message": "The band preference " + self._NEEDS_WPA_SUPPLICANT}

            uuid, err = self._resolve_uuid(
                "Connect to WiFi first to set band preference." if enabled else None
            )
            if err:
                return err

            # Establish which network this belongs to BEFORE writing anything.
            # Recording the preference as on with no network attached leaves it
            # enforced nowhere while the toggle reads on, and lets Force Reapply
            # write the band into an unrelated network. Doing it here means a
            # failure needs no rollback.
            scope_ssid = ""
            if enabled:
                scope_ssid = self._get_profile_ssid(uuid) or ""
                if not scope_ssid:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't identify this network. Try again in a moment.",
                    }
                # A preference set on another network keeps that scope when
                # the band changes, so the band picked here would be written
                # back into that network's profiles, which never saw it.
                held = _load_settings()
                scoped_to = held.get("band_preference_ssid", "")
                if (held.get("band_preference_enabled") and scoped_to
                        and scoped_to != scope_ssid):
                    return {
                        "success": False,
                        "error": "band_scoped_elsewhere",
                        "message": (
                            f"The band preference is set for {_display_ssid(scoped_to)}. "
                            "Turn it off first to set one here."
                        ),
                    }
                if band == "6GHz":
                    refused = await asyncio.to_thread(self._six_ghz_refusal, uuid)
                    if refused:
                        return refused
                # A scan can take 15 s; the guard above is what makes
                # awaiting it safe.
                refused = await asyncio.to_thread(self._band_unreachable, uuid, band)
                if refused:
                    return refused

            value = band if enabled else ""
            unresolved_bands: list[str] = []
            if not enabled:
                # Clear every profile the preference was written to, not just
                # whichever happens to be active. One left demanding a band
                # its network does not offer stops associating, and nothing
                # else would ever revisit it.
                settings_now = _load_settings()
                for other in settings_now.get("band_preference_uuids", []):
                    if other == uuid:
                        continue
                    done = self._nmcli_modify(
                        other, "802-11-wireless.band", "", timeout=2
                    )
                    if done["success"]:
                        continue
                    detail = done.get("stderr", "")
                    decky.logger.info(
                        f"Could not clear band from profile {other}: {_tool_text(detail[:120])}"
                    )
                    # Still set. Keep the record, or nothing knows about it.
                    if "unknown connection" not in detail.lower():
                        unresolved_bands.append(other)
            # What a failed change puts back: the profile's own band, read
            # before the write, rather than one derived from the settings,
            # which can disagree with it (another copy of the network, or a
            # band set outside the plugin).
            settings = _load_settings()
            previous_band = self._profile_band(uuid)
            if previous_band is None:
                previous_band = (settings.get("band_preference", "")
                                 if settings.get("band_preference_enabled") else "")
            result = self._nmcli_modify(uuid, "802-11-wireless.band", value)
            if not result["success"]:
                self._log_detail("set_band_preference", result["stderr"])
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't change the band preference. Try again in a moment.",
                    "detail": result["stderr"],
                }

            # Temporarily clear BSSID lock so NM can find an AP on the
            # requested band. Re-lock to the new BSSID after reconnect.
            settings = _load_settings()
            before = copy.deepcopy(settings)
            had_bssid_lock = settings.get("bssid_lock_enabled", False)
            if enabled and had_bssid_lock:
                unlocked = self._nmcli_modify(uuid, "802-11-wireless.bssid", "")
                if not unlocked["success"]:
                    # The band is already written. Leaving an address from the
                    # other band beside it gives a profile no access point
                    # satisfies, and the reconnect below would activate it.
                    # Put the band back and report, rather than strand it.
                    rollback = self._nmcli_modify(
                        uuid, "802-11-wireless.band", previous_band
                    )
                    if not rollback["success"]:
                        decky.logger.error(
                            "Could not undo the band write after the lock "
                            "release failed; this profile may not associate: "
                            f"{_tool_text(rollback.get('stderr', '')[:120])}"
                        )
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't release the access point lock for the band change. Try again in a moment.",
                        "detail": unlocked["stderr"],
                    }

            settings["band_preference_enabled"] = enabled
            settings["band_preference"] = band
            if not enabled:
                settings["band_preference_uuids"] = unresolved_bands
                settings["band_preference_ssid"] = ""
            if enabled:
                settings["pins_owned"] = _with_pin(settings, "802-11-wireless.band")
                known = list(settings.get("band_preference_uuids", []))
                if uuid not in known:
                    known.append(uuid)
                settings["band_preference_uuids"] = self._capped(
                    known, "802-11-wireless.band"
                )
                # Only claim the network on a fresh enable. reapply_all calls
                # this setter too, so rewriting it every time would move a
                # preference set at home onto whatever network Force Reapply
                # happened to be pressed on.
                if not settings.get("band_preference_ssid"):
                    settings["band_preference_ssid"] = scope_ssid
            _save_settings_with_timestamp(settings)

            if not await self._reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)

            if enabled and band == "6GHz":
                failed = await self._confirm_six_ghz(uuid, before, had_bssid_lock,
                                                     previous_band)
                if failed:
                    return failed

            # Re-lock BSSID to whatever AP NM picked on the new band
            if enabled and had_bssid_lock:
                # Give NM time to associate on the new band before reading the
                # BSSID back. Must not be time.sleep here: this is an async
                # method, and a blocking sleep stalls the whole event loop.
                await asyncio.sleep(3)
                pinned = await asyncio.to_thread(self._relock_pin, uuid)
                if pinned:
                    self._record_relock(pinned)

            return {"success": True, "band": value, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_band_preference error: {e}")
            return self._unexpected_response(e)
        finally:
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    def _dns_profile_change(self, enabled: bool, provider: str,
                            custom_servers: str) -> tuple:
        """set_dns's writes to the profile in use, in a worker thread.
        (uuid, servers, None) once written, or (None, "", the error)."""
        servers = ""
        uuid, err = self._resolve_uuid(
            "Connect to WiFi first to set DNS." if enabled else None
        )
        if err:
            return None, "", err

        if enabled:
            if provider == "custom":
                if not custom_servers or not custom_servers.strip():
                    return None, "", {
                        "success": False,
                        "error": "invalid_dns",
                        "message": "Enter at least one DNS server address.",
                    }
                # Older versions stored the field as typed, and nmcli
                # also accepts commas, so a list saved that way is valid.
                servers = " ".join(custom_servers.replace(",", " ").split())
                parts = servers.split()
                if len(parts) > 6 or not all(
                    DNS_IPV4_RE.match(part) for part in parts
                ):
                    return None, "", {
                        "success": False,
                        "error": "invalid_dns",
                        "message": (
                            "Custom DNS takes up to six IPv4 addresses with spaces "
                            "between them, for example 1.1.1.1 9.9.9.9."
                        ),
                    }
            elif provider in DNS_PROVIDERS:
                servers = DNS_PROVIDERS[provider]
            else:
                return None, "", {
                    "success": False,
                    "error": "invalid_dns",
                    "message": "Choose a DNS provider from the list.",
                }

            previous = self._read_profile_field(uuid, "ipv4.dns")
            previous_v6 = self._read_profile_field(uuid, "ipv6.ignore-auto-dns")
            result = self._nmcli_modify(uuid, "ipv4.dns", servers)
            if not result["success"]:
                self._log_detail("set_dns", result["stderr"])
                return None, "", {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't set this network's DNS servers. Try again in a moment.",
                    "detail": result["stderr"],
                }

            result2 = self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "yes")
            if not result2["success"]:
                # Half applied: our servers merged in beside the router's.
                # Put the profile back the way it was rather than leave a
                # change the settings do not record.
                self._nmcli_modify(uuid, "ipv4.dns", previous.replace(",", " "))
                self._log_detail("set_dns", result2["stderr"])
                return None, "", {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't switch this network to these DNS servers. Try again in a moment.",
                    "detail": result2["stderr"],
                }
            # And the router's IPv6 DNS: on a dual-stack network it was
            # used alongside these servers, so the override was only half
            # one. Undone the same way if it does not take.
            result3 = self._nmcli_modify(uuid, "ipv6.ignore-auto-dns", "yes")
            if not result3["success"]:
                self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "no")
                self._nmcli_modify(uuid, "ipv4.dns", previous.replace(",", " "))
                if previous_v6 in ("yes", "no"):
                    self._nmcli_modify(uuid, "ipv6.ignore-auto-dns", previous_v6)
                self._log_detail("set_dns", result3["stderr"])
                return None, "", {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't switch this network to these DNS servers. Try again in a moment.",
                    "detail": result3["stderr"],
                }
        else:
            # The provider and its servers are kept, so turning DNS back
            # on applies the same ones, rather than Cloudflare (and two
            # reconnects) before a Pi-hole user can pick Custom again.
            # Custom with nothing stored was the dead end that used to
            # reset them here; keeping the servers avoids it, and
            # _clean_settings still repairs one saved by an older version.
            # Order matters and both results matter. Clearing the servers
            # first and then failing to re-enable the automatic ones
            # leaves a profile that ignores DHCP DNS and has none of its
            # own, so it reconnects with no resolvers at all - and with
            # DNS recorded as off, the drift check would not report it.
            restored = self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "no")
            if not restored["success"]:
                self._log_detail("set_dns", restored["stderr"])
                return None, "", {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't give this network its router's DNS back. Try again in a moment.",
                    "detail": restored["stderr"],
                }
            # The router's IPv6 DNS back too. Its failure leaves IPv4's
            # automatic DNS working, so it is reported but nothing is
            # put back.
            restored_v6 = self._nmcli_modify(uuid, "ipv6.ignore-auto-dns", "no")
            if not restored_v6["success"]:
                self._log_detail("set_dns", restored_v6["stderr"])
                return None, "", {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't give this network its router's DNS back. Try again in a moment.",
                    "detail": restored_v6["stderr"],
                }
            cleared = self._nmcli_modify(uuid, "ipv4.dns", "")
            if not cleared["success"]:
                self._log_detail("set_dns", cleared["stderr"])
                return None, "", {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't clear the custom DNS servers. Try again in a moment.",
                    "detail": cleared["stderr"],
                }
        return uuid, servers, None

    async def set_dns(
        self, enabled: bool, provider: str = "cloudflare", custom_servers: str = ""
    ) -> dict:
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        enabled = enabled is True
        # A profile change like the lock and the band: it writes the profile
        # and reconnects, and two at once interleave their radio cycles.
        if self._profile_change_refused():
            return self._busy_result()
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        try:
            # The profile writes in a worker thread: up to eight nmcli calls,
            # each one's whole timeout against a slow NetworkManager, held
            # the event loop (and every panel call) for all of them.
            uuid, servers, err = await asyncio.to_thread(
                self._dns_profile_change, enabled, provider, custom_servers)
            if err:
                return err

            # Every other profile we wrote to, not just the one in use. One
            # we cannot reach stays listed for the next attempt. Up to
            # sixteen profiles, three nmcli calls each, so in a worker
            # thread, and before the settings are read for the save below.
            others, kept = [], set()
            if not enabled:
                others = [u for u in _load_settings().get("dns_uuids", []) if u != uuid]
                kept = set(await asyncio.to_thread(
                    lambda: [u for u in others if not self._clear_dns(u)]))
            settings = _load_settings()
            known = [u for u in settings.get("dns_uuids", []) if u != uuid]
            if enabled:
                known.append(uuid)
            else:
                known = [u for u in known if u not in others or u in kept]
            settings["dns_uuids"] = self._capped(known, "ipv4.dns")
            settings["dns_enabled"] = enabled
            if enabled:
                settings["dns_provider"] = provider
                settings["dns_servers"] = servers
            _save_settings_with_timestamp(settings)

            if not await self._reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)
            return {"success": True, "dns_set": enabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_dns error: {e}")
            return self._unexpected_response(e)
        finally:
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    async def set_ipv6(self, disabled: bool) -> dict:
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        disabled = disabled is True
        if self._profile_change_refused():
            return self._busy_result()
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        try:

            uuid, err = self._resolve_uuid(
                "Connect to WiFi first to disable IPv6." if disabled else None
            )
            if err:
                return err

            method = "disabled" if disabled else "auto"
            result = self._nmcli_modify(uuid, "ipv6.method", method)
            if not result["success"]:
                self._log_detail("set_ipv6", result["stderr"])
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't change IPv6 for this network. Try again in a moment.",
                    "detail": result["stderr"],
                }

            settings = _load_settings()
            settings["ipv6_disabled"] = disabled
            known = list(settings.get("ipv6_uuids", []))
            if disabled:
                # Remember where we put it. Without this the only profile we
                # could ever undo is whichever one happens to be active when
                # the toggle goes off, and NetworkManager keeps more than one
                # profile per network.
                if uuid not in known:
                    known.append(uuid)
                settings["ipv6_uuids"] = self._capped(known, "ipv6.method")
            else:
                settings["ipv6_uuids"] = [u for u in known if u != uuid]
            _save_settings_with_timestamp(settings)

            if not await self._reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)
            return {"success": True, "ipv6_disabled": disabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_ipv6 error: {e}")
            return self._unexpected_response(e)
        finally:
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    def _capture_sysctl_baseline(self, settings: dict):
        """Record what the buffer sysctls are before we change them.

        Only values that differ from ours are taken, so a value we set
        ourselves (the dispatcher reapplies tuning on connect, before the
        plugin loads) is never mistaken for the system's own. A baseline read
        on another kernel is discarded: a reboot into a newer kernel brings
        new defaults, and the old ones would then be the wrong thing to put
        back.
        """
        release = _kernel_release()
        baseline = settings.get("sysctl_baseline") or {}
        values = dict(baseline.get("values", {})) if baseline.get("kernel") == release else {}
        # Tuning already in place (every game launch reapplies it) means there
        # is nothing of the system's left to read. One call instead of eight.
        live = (self._run_cmd(["/usr/bin/sysctl", "-n", "net.core.rmem_max"]).get("stdout") or "").strip()
        if live == TUNED_RMEM_MAX:
            return
        for key, tuned in SYSCTL_PARAMS.items():
            if key in values:
                continue
            current = (self._run_cmd(["/usr/bin/sysctl", "-n", key]).get("stdout") or "").strip()
            if re.fullmatch(r"[0-9]{1,12}", current, re.ASCII) and current != tuned:
                values[key] = current
        settings["sysctl_baseline"] = _clean_sysctl_baseline(
            {"kernel": release, "values": values}
        )

    def _sysctl_restore_values(self, settings: dict) -> dict:
        """What to put back: the recorded baseline where we have one for this
        kernel, the kernel's own default otherwise."""
        release = _kernel_release()
        values = _stock_sysctl_defaults(release)
        baseline = settings.get("sysctl_baseline") or {}
        if baseline.get("kernel") != release:
            return values
        for key, recorded in baseline.get("values", {}).items():
            # Within a sane band of the defaults only. The file this comes
            # from is the desktop user's, and a backlog of 1 or a buffer of
            # 0 written back as root would break networking until a reboot.
            low = max(1, int(values[key]) // 16)
            high = max(int(values[key]), int(SYSCTL_PARAMS[key])) * 16
            if low <= int(recorded) <= high:
                values[key] = recorded
        return values

    def _restore_buffer_sysctls(self, settings: dict) -> int:
        """Put the buffer sysctls back. Returns how many writes failed."""
        failed = 0
        for key, value in self._sysctl_restore_values(settings).items():
            result = self._run_cmd(["/usr/bin/sysctl", "-w", f"{key}={value}"])
            if not result["success"]:
                failed += 1
                decky.logger.error(f"sysctl {key}={value} failed: {_tool_text(result['stderr'])}")
        return failed

    def _buffer_tuning_in_place(self, settings: dict) -> bool:
        """Whether reset or uninstall has anything of ours to undo. Writing the
        restore values unconditionally replaced whatever the system had even
        for people who never turned tuning on."""
        if settings.get("buffer_tuning_enabled"):
            return True
        live = self._run_cmd(["/usr/bin/sysctl", "-n", "net.core.rmem_max"])
        return (live.get("stdout") or "").strip() == TUNED_RMEM_MAX

    def _revert_queue_tuning(self, settings: dict) -> bool:
        """Undo buffer tuning and CAKE for reset and uninstall, and only what
        we actually changed: the system's own values are left alone for
        someone who never turned either on. Returns False when something of
        ours is still in place (a sysctl, the queue length or CAKE)."""
        all_off = True
        tuned = self._buffer_tuning_in_place(settings)
        if tuned and self._restore_buffer_sysctls(settings):
            all_off = False
        iface = self._get_wifi_interface()
        if iface:
            # True also when tc did not answer (None) with the setting on.
            cake = settings.get("cake_enabled") or self._get_cake_status(iface) is True
            if tuned or cake:
                result = self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", "1000"])
                if not result["success"]:
                    decky.logger.error(f"Could not put the queue length back: {_tool_text(result['stderr'][:120])}")
                    all_off = False
            if cake:
                result = self._run_cmd(["/usr/bin/tc", "qdisc", "del", "dev", iface, "root"])
                # A setting that said CAKE with none in place leaves nothing
                # to delete; only CAKE still there counts, or a tc that did
                # not answer (stuck, so nothing could look).
                # A second tc that did not answer either is no proof it is gone.
                if not result["success"] and (self._card_not_responding(result)
                                              or self._get_cake_status(iface) is not False):
                    decky.logger.error(f"Could not take CAKE off: {_tool_text(result['stderr'][:120])}")
                    all_off = False
        return all_off

    def _queue_tuning_guard(self) -> asyncio.Lock:
        """Held while buffer tuning or CAKE change, and while Reset or the end
        of a stream takes them off. The setters' commands run in a worker
        thread, and two of those at once (or one beside Reset's undo)
        interleaved their writes to the same sysctls and queue."""
        if getattr(self, "_queue_tuning_lock", None) is None:
            self._queue_tuning_lock = asyncio.Lock()
        return self._queue_tuning_lock

    async def set_buffer_tuning(self, enabled: bool, *, reapply: bool = False) -> dict:
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is. reapply (keyword only, never
        # from the panel): see set_power_save.
        enabled = enabled is True
        try:
            # Uninstall under way: nothing is put on after it took it off.
            if enabled and getattr(self, "_tearing_down", False):
                return self._busy_result()
            if enabled and not reapply and self._paused_by_streaming():
                return self._record_paused("buffer_tuning_enabled")
            # Up to eight sysctl calls and an ip, 5 s each, so off the event
            # loop, one change at a time.
            async with self._queue_tuning_guard():
                return await asyncio.to_thread(self._buffer_tuning_work, enabled, reapply)
        except Exception as e:
            decky.logger.error(f"set_buffer_tuning error: {e}")
            return self._unexpected_response(e)

    def _buffer_tuning_work(self, enabled: bool, reapply: bool) -> dict:
        """set_buffer_tuning's commands and save, in a worker thread."""
        # A re-application decides from the settings as they are now, read
        # in the same call that applies: a Reset or the user's off since the
        # caller read them wins.
        if reapply and not (enabled and self._reapply_wanted("buffer_tuning_enabled")):
            return {"success": True, "skipped": True}
        apply_lock = None
        try:
            settings = _load_settings()
            baseline = None
            if enabled:
                self._capture_sysctl_baseline(settings)
                baseline = settings.get("sysctl_baseline")
                failed_keys = []
                for key, value in SYSCTL_PARAMS.items():
                    result = self._run_cmd(
                        ["/usr/bin/sysctl", "-w", f"{key}={value}"]
                    )
                    if not result["success"]:
                        failed_keys.append(key)
                        decky.logger.error(f"sysctl {key}={value} failed: {_tool_text(result['stderr'])}")
                if len(failed_keys) == len(SYSCTL_PARAMS):
                    # Reporting success here recorded the setting as on, which
                    # then showed as drift on every poll with no control able
                    # to clear it: Optimize Safe just calls back into here and
                    # fails again.
                    return {
                        "success": False,
                        "error": "unexpected",
                        "message": ("Couldn't apply network buffer settings. Try again. "
                                    "If it keeps failing, restart the device."),
                    }
            else:
                # Refusing to disable would leave the setting recorded as on
                # with a toggle that will not move, which is worse than
                # recording the user's intent and logging that the restore
                # did not take. Saved before the restore, for the reason
                # set_power_save gives: a reconnect meanwhile reapplied it.
                # With the apply lock held from the save to the restore, as
                # set_power_save does: a dispatcher run that read "on" would
                # otherwise put the tuning back after it came off.
                apply_lock = _take_apply_lock()
                settings = _load_settings()
                settings["buffer_tuning_enabled"] = False
                _save_settings(settings)
                if self._restore_buffer_sysctls(settings) == len(SYSCTL_PARAMS):
                    decky.logger.error(
                        "Could not restore any kernel buffer defaults; recording "
                        "the setting as off anyway so the toggle is not stuck"
                    )
                failed_keys = []

            # TX queue length (CAKE needs 256; defer to it if active)
            iface = self._get_wifi_interface()
            if iface:
                # CAKE paused by auto mode is not on the interface to defer to.
                now = _load_settings()
                if now.get("cake_enabled") and self._volatile_gate_open(now):
                    txq = "256"
                else:
                    txq = "2000" if enabled else "1000"
                self._run_cmd(
                    ["/usr/bin/ip", "link", "set", iface, "txqueuelen", txq]
                )

            # Read again for the save: the commands above took a while, and
            # a copy from before them would undo whatever was saved meanwhile.
            # A re-application saves only the baseline it read, and leaves
            # the setting and "Last changed" as they are.
            settings = _load_settings()
            if baseline is not None:
                settings["sysctl_baseline"] = baseline
            if reapply:
                if baseline is not None:
                    _save_settings(settings)
            else:
                settings["buffer_tuning_enabled"] = enabled
                _save_settings_with_timestamp(settings)
            result = {"success": True, "buffer_tuning": enabled}
            if failed_keys:
                # Some took and some did not. The status poll only checks one
                # of them, so without saying so here a partial apply looks
                # exactly like a full one. Their names are in the log above,
                # one line each; to the reader they mean nothing.
                result["message"] = (
                    "Most buffer settings applied; a few weren't accepted by the system."
                )
            return result
        finally:
            _drop_apply_lock(apply_lock)

    def _get_cake_status(self, iface: str) -> bool | None:
        """Whether a CAKE qdisc is on the interface: True or False from tc's
        answer, None when tc did not answer (stuck behind the driver, timed
        out or refused), which says nothing either way."""
        result = self._run_cmd(["/usr/bin/tc", "qdisc", "show", "dev", iface])
        if not result.get("success"):
            return None
        return "cake" in result.get("stdout", "")

    async def set_cake(self, enabled: bool, *, reapply: bool = False) -> dict:
        """Enable or disable CAKE QoS (unlimited mode: FQ + AQM + ack-filter, no bandwidth shaper)."""
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is. reapply (keyword only, never
        # from the panel): see set_power_save.
        enabled = enabled is True
        try:
            # Uninstall under way: nothing is put on after it took it off.
            if enabled and getattr(self, "_tearing_down", False):
                return self._busy_result()
            if enabled and not reapply and self._paused_by_streaming():
                return self._record_paused("cake_enabled")
            # modprobe, tc and ip, so off the event loop, one change at a time.
            async with self._queue_tuning_guard():
                return await asyncio.to_thread(self._cake_work, enabled, reapply)
        except Exception as e:
            decky.logger.error(f"set_cake error: {e}")
            return self._unexpected_response(e)

    def _cake_work(self, enabled: bool, reapply: bool) -> dict:
        """set_cake's commands and save, in a worker thread."""
        if reapply and not (enabled and self._reapply_wanted("cake_enabled")):
            return {"success": True, "skipped": True}
        apply_lock = None
        try:
            iface = self._get_wifi_interface()
            if not iface:
                if enabled:
                    return {"success": False, "error": "no_wifi", "message": "Not connected to WiFi."}
                settings = _load_settings()
                settings["cake_enabled"] = False
                _save_settings_with_timestamp(settings)
                return {"success": True, "cake": False}

            if enabled:
                modprobe = "/usr/bin/modprobe" if os.path.isfile("/usr/bin/modprobe") else "/usr/sbin/modprobe"
                self._run_cmd([modprobe, "sch_cake"], timeout=5)
                result = self._run_cmd([
                    "/usr/bin/tc", "qdisc", "replace", "dev", iface, "root",
                    "cake", "unlimited", "diffserv4", "nat", "ack-filter",
                ])
                if not result["success"] and self._card_not_responding(result):
                    return self._not_responding_result("CAKE", result.get("stderr", ""))
                if not result["success"]:
                    self._log_detail("set_cake", result.get("stderr", ""))
                    return {
                        "success": False,
                        "error": "unexpected",
                        "message": "Couldn't turn on CAKE. If it keeps failing, this system may not include it.",
                        "detail": result.get("stderr", ""),
                    }
                # Lower txqueuelen to complement CAKE's queue management
                self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", "256"])
                decky.logger.info(f"CAKE enabled (unlimited) on {iface}")
            else:
                # Saved off before it comes off, for the reason set_power_save
                # gives, and put back on below if it would not come off. The
                # apply lock is held from the save to the end, as for buffer
                # tuning, so a dispatcher run cannot put CAKE back between.
                apply_lock = _take_apply_lock()
                settings = _load_settings()
                was_on = bool(settings.get("cake_enabled"))
                if was_on:
                    settings["cake_enabled"] = False
                    _save_settings(settings)
                removal = self._run_cmd(
                    ["/usr/bin/tc", "qdisc", "del", "dev", iface, "root"]
                )
                # tc stuck behind the driver: nothing could look, so CAKE is
                # not recorded as removed.
                if not removal["success"] and self._card_not_responding(removal):
                    if was_on:
                        settings = _load_settings()
                        settings["cake_enabled"] = True
                        _save_settings(settings)
                    return self._not_responding_result("CAKE", removal.get("stderr", ""))
                if not removal["success"]:
                    # Not fatal - tc reports an error when there is no qdisc
                    # to remove - but worth a line when it is something else.
                    decky.logger.info(
                        f"tc qdisc del on {iface}: {_tool_text(removal.get('stderr', '')[:120])}"
                    )
                    # Still there, or a tc that did not answer this time.
                    if self._get_cake_status(iface) is not False:
                        if was_on:
                            settings = _load_settings()
                            settings["cake_enabled"] = True
                            _save_settings(settings)
                        return {
                            "success": False,
                            "error": "unexpected",
                            "message": ("Couldn't remove CAKE. Try again in a moment. "
                                        "If it keeps failing, restart the device."),
                            "detail": removal.get("stderr", ""),
                        }
                # Restore txqueuelen based on whether buffer tuning is active
                settings = _load_settings()
                tuned = settings.get("buffer_tuning_enabled") and self._volatile_gate_open(settings)
                txq = "2000" if tuned else "1000"
                self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", txq])
                decky.logger.info(f"CAKE disabled on {iface}")

            # A re-application leaves the setting and "Last changed" as they
            # are: it only puts back what the setting already says.
            if not reapply:
                settings = _load_settings()
                settings["cake_enabled"] = enabled
                _save_settings_with_timestamp(settings)
            return {"success": True, "cake": enabled}
        finally:
            _drop_apply_lock(apply_lock)

    async def optimize_safe(self) -> dict:
        """Apply universally-safe optimizations: power save, BSSID lock, auto-fix, buffer tuning."""
        try:

            results = {}
            applied = 0
            total = 4

            # Order matters: BSSID lock reconnects WiFi which resets power_save.
            # Apply auto-fix and buffer tuning first (no reconnect), then BSSID
            # lock (reconnects - dispatcher reapplies settings), then power_save
            # last to ensure it sticks.
            r = await self.set_auto_fix(True)
            results["auto_fix"] = r
            if r.get("success"):
                applied += 1

            r = await self.set_buffer_tuning(True)
            results["buffer_tuning"] = r
            if r.get("success"):
                applied += 1

            # Under iwd the lock would only stop NetworkManager reconnecting,
            # so Optimize Safe leaves it out rather than report it failed,
            # and says so: left out silently, the panel read "All applied".
            if await asyncio.to_thread(self._backend_is_iwd):
                total -= 1
                results["bssid_lock"] = {
                    "success": True,
                    "message": ("Left out: the access point lock needs the "
                                "wpa_supplicant backend (Advanced)."),
                }
            elif await asyncio.to_thread(self._lock_holding):
                # Already on and holding on this network. Turning it on again
                # rewrote the pin and cycled the radio for nothing, and its
                # fresh scan could move the lock to another access point.
                results["bssid_lock"] = {"success": True}
                applied += 1
            elif await asyncio.to_thread(self._lock_held_elsewhere):
                # Turning it on again here would move the lock off the
                # network it was set on, which nobody asked Optimize to do.
                total -= 1
                results["bssid_lock"] = {
                    "success": True,
                    "message": "Left as it is: the access point lock is on for another network.",
                }
            else:
                r = await self.set_bssid_lock(True)
                results["bssid_lock"] = r
                if r.get("success"):
                    applied += 1

            r = await self.set_power_save(True)
            results["power_save"] = r
            if r.get("success"):
                applied += 1

            settings = _load_settings()
            settings["last_applied"] = int(time.time())
            _save_settings(settings)

            return {
                "success": True,
                "total": total,
                "applied": applied,
                "results": results,
                # Only the lock reconnects, and only when it was turned on.
                "reconnected": any(bool(r.get("reconnected")) for r in results.values()),
            }
        except Exception as e:
            decky.logger.error(f"optimize_safe error: {e}")
            return self._unexpected_response(e)

    def _lock_holding(self) -> bool:
        """Whether the access point lock is on, for this network, written to
        the profile in use, and holding: the device is on the access point
        it pins. Several subprocesses, so called in a worker thread."""
        settings = _load_settings()
        value = settings.get("bssid_lock_value", "").upper()
        if not settings.get("bssid_lock_enabled") or not value:
            return False
        uuid = self._get_active_connection_uuid()
        iface = self._get_wifi_interface()
        if not uuid or not iface or not IFACE_RE.match(iface):
            return False
        if not self._lock_is_for_this_network(settings, uuid, self._get_profile_ssid(uuid)):
            return False
        pinned = self._read_profile_field(uuid, "802-11-wireless.bssid").replace("\\", "")
        if pinned.upper() != value:
            return False
        link = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
        if not link["success"]:
            return False
        return self._parse_link(link.get("stdout", "")).get("connected_bssid", "").upper() == value

    def _record_reapplied(self, last_changed: int, stamps=(), skipped: bool = False):
        """Put back "Last changed" after a re-application, and stamp it as one.

        Re-applying reuses the setters. Called with reapply=True they leave
        "Last changed" alone; a setter that stamped anyway reports the stamp
        it wrote (stamps), and only that is put back to last_changed, and
        only when no step was skipped. Anything else there was saved by
        something else meanwhile (a Reset, the user's own toggle), and
        writing the copy from before over it undid that. Re-applying is what
        the dispatcher does after a reconnect, and the panel already shows
        that separately as "Auto-applied", from the file the dispatcher
        writes. Use that.
        """
        # Contract C12: whoever is first to write last_enforced in a boot
        # claims the "driver parameters at load" line before it. The callers
        # claim it first in a worker thread (it reads sysfs and runs logger),
        # so here it only finds the marker already made.
        self._claim_at_load()
        settings = _load_settings()
        current = settings.get("last_applied", 0)
        if not skipped and current != last_changed and current in stamps:
            settings["last_applied"] = last_changed
            # Best effort: only the panel's "Last changed" depends on it, and
            # a save that raised ended startup and the streaming pass that
            # called this, with nothing after it run.
            try:
                _save_settings(settings)
            except OSError as e:
                self._log_throttled("settings_save", f"Could not save settings: {e}")
        try:
            os.makedirs(os.path.dirname(ENFORCED_FILE), mode=0o755, exist_ok=True)
        except OSError:
            pass
        # Renamed over, never removed first: the dispatcher reads a missing
        # file as a boot nothing of ours has touched yet (_replace_no_follow).
        _replace_no_follow(ENFORCED_FILE, str(int(time.time())))

    async def _reapply_volatile_steps(self, settings: dict, power_save_first: bool = True,
                                      results: dict | None = None) -> tuple:
        """Run the volatile setters a re-application wants, each with
        reapply=True: each decides again from the settings as they are when
        it gets its turn, so settings is only which ones to try. Power save
        first, or last (startup: buffer tuning sets the queue length, CAKE
        overrides it, power save sticks after any reconnects). (applied,
        total, stamps, skipped). A step skipped counts in neither total nor
        applied."""
        applied, total, stamps, skipped = 0, 0, set(), False
        power_save = ("power_save_disabled", "power_save",
                      lambda: self.set_power_save(True, reapply=True))
        queue = (("buffer_tuning_enabled", "buffer_tuning",
                  lambda: self.set_buffer_tuning(True, reapply=True)),
                 ("cake_enabled", "cake", lambda: self.set_cake(True, reapply=True)))
        steps = (power_save, *queue) if power_save_first else (*queue, power_save)
        for key, name, run in steps:
            if not settings.get(key):
                continue
            r = await run()
            if r.get("skipped"):
                skipped = True
                continue
            total += 1
            if results is not None:
                results[name] = r
            if r.get("success"):
                applied += 1
            if "stamp" in r:
                stamps.add(r["stamp"])
        return applied, total, stamps, skipped

    async def reapply_volatile(self) -> dict:
        """Reapply volatile (non-reconnecting) settings. Safe to call mid-stream."""
        try:
            # A Reset, a lock or band change or a backend switch under way:
            # it decides what is in force, and re-applying from settings read
            # before it finished put back what it took off.
            if self._profile_change_in_flight() or self._backend_switch_running():
                return {"success": True, "applied": 0, "total": 0, "skipped": True}
            settings = _load_settings()
            if not self._volatile_gate_open(settings):
                # Auto mode with no stream: the fixes stay off until one starts.
                return {"success": True, "applied": 0, "total": 0, "paused": True}
            applied, total, stamps, skipped = await self._reapply_volatile_steps(settings)

            if total > 0:
                decky.logger.info(f"reapply_volatile: {applied}/{total} applied")
                await asyncio.to_thread(self._claim_at_load)
                self._record_reapplied(settings.get("last_applied", 0), stamps, skipped)

            return {"success": True, "applied": applied, "total": total}
        except Exception as e:
            decky.logger.error(f"reapply_volatile error: {e}")
            return self._unexpected_response(e)

    async def reapply_all(self) -> dict:
        """Force reapply all enabled optimizations."""
        try:

            settings = _load_settings()
            results = {}
            applied = 0
            total = 0
            did_reconnect = False

            # Non-reconnecting first
            if settings.get("auto_fix_on_wake"):
                total += 1
                r = await self.set_auto_fix(True)
                results["auto_fix"] = r
                if r.get("success"):
                    applied += 1

            # Buffer tuning and CAKE as re-applications (reapply=True): each
            # reads the settings again when its turn comes, so a Reset or a
            # toggle off since the read above is not undone. A step that
            # finds its setting off counts in neither total nor applied.
            for key, name, run in (
                    ("buffer_tuning_enabled", "buffer_tuning",
                     lambda: self.set_buffer_tuning(True, reapply=True)),
                    ("cake_enabled", "cake", lambda: self.set_cake(True, reapply=True))):
                if not settings.get(key):
                    continue
                r = await run()
                if r.get("skipped"):
                    continue
                total += 1
                results[name] = r
                if r.get("success"):
                    applied += 1

            # Reconnecting (each does hard_reconnect). What they depend on is
            # read off the event loop first: each is a subprocess.
            here_uuid, here_ssid, iwd = await asyncio.to_thread(self._reapply_context)
            if iwd:
                # Neither pin can be used under iwd; see _pins_for_backend.
                settings = {**settings, "bssid_lock_enabled": False,
                            "band_preference_enabled": False}
            if settings.get("bssid_lock_enabled") and not await asyncio.to_thread(
                self._lock_is_for_this_network, settings, here_uuid, here_ssid
            ):
                # The same reasoning as the band below: reapplying the lock
                # here would pin the first access point of a network nobody
                # asked to lock, and block roaming on it.
                decky.logger.info("Skipping access point lock: it belongs to another network")
            elif settings.get("bssid_lock_enabled"):
                total += 1
                r = await self.set_bssid_lock(True)
                results["bssid_lock"] = r
                if r.get("success"):
                    applied += 1
                did_reconnect = True

            if settings.get("band_preference_enabled"):
                # Only on the network the preference belongs to. Reapplying it
                # anywhere else writes a band into that network's profile and
                # reconnects into it, which strands a network that has no
                # access point on that band. The setter refuses a change
                # made elsewhere too; this skips quietly, as the path is
                # programmatic.
                scoped_to = settings.get("band_preference_ssid", "")
                # Read again: the lock above reconnects, and can land on
                # another copy of the network.
                here = (await asyncio.to_thread(self._reapply_context, False))[1]
                # An unknown scope is not permission. Everything else treats
                # it as "do not enforce"; treating it as "go ahead" here would
                # write the band into whatever network is active and reconnect
                # into it, which is the stranding this scoping exists to stop.
                if not scoped_to or here != scoped_to:
                    decky.logger.info(
                        "Skipping band preference: it belongs to another network"
                    )
                else:
                    total += 1
                    r = await self.set_band_preference(
                        True, settings.get("band_preference", "a")
                    )
                    results["band_preference"] = r
                    if r.get("success"):
                        applied += 1
                    did_reconnect = True

            # DNS and IPv6 belong to the networks they were set on, the same
            # as the band: reapplying them here would spread them onto
            # whichever network happens to be active. An empty list is a
            # setting from before tracking existed; applying it once records
            # where it lives.
            active_uuid = (await asyncio.to_thread(self._reapply_context, False))[0]

            def elsewhere(list_key: str) -> bool:
                tracked = settings.get(list_key, [])
                return bool(tracked) and active_uuid not in tracked

            if settings.get("dns_enabled") and elsewhere("dns_uuids"):
                decky.logger.info("Skipping DNS: it was set on another network")
            elif settings.get("dns_enabled"):
                total += 1
                r = await self.set_dns(
                    True,
                    settings.get("dns_provider", "cloudflare"),
                    settings.get("dns_servers", ""),
                )
                results["dns"] = r
                if r.get("success"):
                    applied += 1
                did_reconnect = True

            if settings.get("ipv6_disabled") and elsewhere("ipv6_uuids"):
                decky.logger.info("Skipping IPv6: it was turned off on another network")
            elif settings.get("ipv6_disabled"):
                total += 1
                r = await self.set_ipv6(True)
                results["ipv6"] = r
                if r.get("success"):
                    applied += 1
                did_reconnect = True

            # Power save last (sticks after any reconnects, dispatcher also
            # reapplies), as a re-application like buffer tuning and CAKE.
            if settings.get("power_save_disabled"):
                r = await self.set_power_save(True, reapply=True)
                if not r.get("skipped"):
                    total += 1
                    results["power_save"] = r
                    if r.get("success"):
                        applied += 1

            if total == 0:
                return {
                    "success": True,
                    "total": 0,
                    "applied": 0,
                    "results": {},
                    "message": "No optimizations are turned on.",
                }

            result = {
                "success": True,
                "total": total,
                "applied": applied,
                "results": results,
            }
            if did_reconnect:
                result["reconnected"] = True
            return result
        except Exception as e:
            decky.logger.error(f"reapply_all error: {e}")
            return self._unexpected_response(e)

    def _reapply_context(self, backend: bool = True) -> tuple:
        """(active profile, its network, whether iwd is in use) for
        reapply_all, read in a worker thread. backend=False skips the last,
        answering False."""
        uuid = self._get_active_connection_uuid() or ""
        return uuid, self._get_profile_ssid(uuid), backend and self._backend_is_iwd()

    async def repair_drifted(self) -> dict:
        """Put back only the settings that are on and have stopped holding.

        What the drift banner's "Fix now" runs. It used to run Optimize Safe,
        which turned on the access point lock, buffer tuning and auto-fix for
        someone who had deliberately left them off, and did nothing for drift
        in CAKE, IPv6 or the band, while reporting everything applied.
        """
        try:
            status = await self.get_status()
            drift = dict(status.get("drift") or {})
            settings = _load_settings()
            # Non-reconnecting first and power save last, as in reapply_all,
            # so a reconnect cannot undo what was just put back.
            # Power save, buffer tuning and CAKE as re-applications
            # (reapply=True): each reads the settings again when its turn
            # comes, so a Reset or a toggle off meanwhile is not undone. One
            # that finds its setting off counts in neither total nor applied.
            steps = (
                ("buffer_tuning", "buffer_tuning_enabled",
                 lambda: self.set_buffer_tuning(True, reapply=True)),
                ("cake", "cake_enabled", lambda: self.set_cake(True, reapply=True)),
                ("bssid_lock", "bssid_lock_enabled",
                 lambda: self.set_bssid_lock(True)),
                ("band_preference", "band_preference_enabled",
                 lambda: self.set_band_preference(
                     True, settings.get("band_preference", "a"))),
                ("ipv6", "ipv6_disabled", lambda: self.set_ipv6(True)),
                # The servers saved with the setting, so a custom list is put
                # back as it was rather than replaced by a provider's.
                ("dns", "dns_enabled", lambda: self.set_dns(
                    True, settings.get("dns_provider", "cloudflare"),
                    settings.get("dns_servers", ""))),
                ("power_save", "power_save_disabled",
                 lambda: self.set_power_save(True, reapply=True)),
            )
            results = {}
            applied = 0
            reconnected = False
            for key, enabled_key, run in steps:
                if not drift.get(key) or not settings.get(enabled_key):
                    continue
                r = await run()
                if r.get("skipped"):
                    continue
                results[key] = r
                if r.get("success"):
                    applied += 1
                reconnected = reconnected or bool(r.get("reconnected"))
            result = {
                "success": True,
                "total": len(results),
                "applied": applied,
                "results": results,
            }
            if not results:
                result["message"] = "Nothing needed fixing."
            if reconnected:
                result["reconnected"] = True
            return result
        except Exception as e:
            decky.logger.error(f"repair_drifted error: {e}")
            return self._unexpected_response(e)

    # What Reset answers once uninstall has started under it.
    _TEARING_DOWN_RESULT = {"success": False, "error": "busy",
                            "message": "The plugin is being uninstalled."}

    # How long Reset spends taking pins off profiles. Ones it does not reach
    # stay tracked, and the leftover-pin cleanup takes them off later.
    _RESET_PIN_BUDGET = 30

    async def reset_settings(self) -> dict:
        """Undo everything and go back to the defaults, with everything off."""
        # A profile change of its own: it rewrites the profiles the lock,
        # band, DNS and IPv6 setters write, so none of them may run alongside.
        # An update reloads the plugin when it finishes, which would cut a
        # reset off part way; a backend switch restarts NetworkManager under it.
        if self._profile_change_refused():
            return self._busy_result()
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        try:
            # And no streaming pass may put the fixes back while they come
            # off, or take them off half way through.
            async with self._stream_guard():
                self._bump_stream_epoch()
                # And no set_power_save already under way (the startup
                # apply, or the reapply after a game launch) may save power
                # save back on and rewrite its files after Reset took them
                # off. Taken after the stream guard, as the auto mode toggle
                # nests them.
                async with self._power_save_guard():
                    return await self._reset_locked()
        except Exception as e:
            decky.logger.error(f"reset_settings error: {e}")
            return self._unexpected_response(e)
        finally:
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    async def _reset_locked(self) -> dict:
        # A settings file that could not be read refuses every save, Reset's
        # included, and a restart does not change that. Set aside as
        # unreadable (renamed, never read) so Reset can go on from defaults.
        if _unread_settings_in_place() and not _set_aside_unread_settings():
            return {
                "success": False,
                "error": "settings_unreadable",
                "message": ("The settings file can't be read or replaced. Remove "
                            "~/homebrew/settings/WiFi Optimizer/settings.json, then "
                            "try Reset again."),
            }
        # The new settings are saved before anything is undone. The
        # dispatcher reapplies whatever the settings say on every reconnect,
        # and the old ones said on until the very end.
        #
        # Power save is saved off, which is not the default a fresh install
        # gets. Reset has just turned it back on, so a default of off-is-on
        # read as drifted at once, and the dispatcher put it back off at the
        # next reconnect with nobody having asked.
        #
        # The pins are kept, and what this install owns: the leftover-pin
        # cleanup works from both, and the profiles still carry them until
        # they are taken off below.
        info = await self.get_device_info()
        # Uninstalled meanwhile: a save would put back the settings file.
        if getattr(self, "_tearing_down", False):
            return dict(self._TEARING_DOWN_RESULT)
        # Read after that await, not before: a status pass in the meantime
        # can record a profile (an IPv6 adoption), and settings read earlier
        # would neither keep it nor take its pin off.
        before = _load_settings()
        fresh = copy.deepcopy(DEFAULT_SETTINGS)
        # Current, so the next load does not run the migrations again.
        fresh["settings_version"] = self._SETTINGS_VERSION
        fresh["model"] = info.get("model", "unknown")
        fresh["device_family"] = info.get("device_family", "unknown")
        fresh["device_label"] = info.get("device_label", "Unknown Device")
        # The radio can be missing (WiFi down after a bad wake, the state
        # Reset is often pressed in). Startup keeps the driver it knew rather
        # than record "unknown", and so does Reset: "unknown" leaves the
        # driver's fixes on below, stops the dispatcher applying them, and
        # has the panel call the hardware unknown.
        if info.get("driver", "unknown") != "unknown" or before.get("driver") in (
                None, "", "unknown"):
            fresh["driver"] = info.get("driver", "unknown")
            fresh["chip_label"] = info.get("chip_label", "unknown")
            fresh["supports_6ghz"] = info.get("supports_6ghz", False)
        else:
            fresh["driver"] = before.get("driver", "unknown")
            fresh["chip_label"] = before.get("chip_label", "unknown")
            fresh["supports_6ghz"] = before.get("supports_6ghz", False)
        distro = self._detect_distro()
        fresh["distro_id"] = distro["id"]
        fresh["distro_name"] = distro["name"]
        # A settings file set aside as unreadable is kept through a reset,
        # since it may be the only copy of what the user had. Whether they
        # have already dismissed the notice about it is kept too: dropping
        # that would bring back a notice they have seen, about a file that
        # has not changed.
        fresh["corrupt_acknowledged"] = before.get("corrupt_acknowledged", [])
        # Update preferences are not WiFi settings. Reset used to put a beta
        # tester back on Stable without a word, and bring back a notice they
        # had turned off, announcing a version already announced.
        for key in ("update_channel", "update_notify", "update_notified_version"):
            fresh[key] = copy.deepcopy(before.get(key, DEFAULT_SETTINGS[key]))
        fresh["power_save_disabled"] = False
        fresh["pins_owned"] = list(before.get("pins_owned", []))
        for key in ("bssid_lock_uuids", "band_preference_uuids", "ipv6_uuids", "dns_uuids"):
            fresh[key] = list(before.get(key, []))
        # Allow WPA2 here goes on the list the poll retries from the start.
        # Kept on the live list until the final save below, a Reset cut off
        # in between (a reload) left the profile on WPA2 with nothing ever
        # putting WPA3 only back: the live list is retried by nothing.
        fresh["wpa2_uuids"] = []
        fresh["wpa2_restore_uuids"] = list(dict.fromkeys(
            list(before.get("wpa2_uuids", [])) + list(before.get("wpa2_restore_uuids", []))))
        # Each step is tried whatever the one before it did, as uninstall's
        # are: one that raised used to end Reset there, with the defaults
        # already saved and everything after it (the backend setting, the
        # dispatcher, the record of pins left) never reached.
        not_undone = []

        async def undo(what: str, work, *args, default=None):
            # A helper that raised, or one that returned False: it swallows
            # what it could not undo (logged), and Reset said "done" over it.
            try:
                result = await asyncio.to_thread(work, *args)
            except Exception as e:
                decky.logger.error(f"Reset could not undo {what}: {e}")
                not_undone.append(what)
                return default
            if result is False:
                decky.logger.error(f"Reset could not undo all of {what}")
                not_undone.append(what)
            return result

        # Held from this save until power save is off: a dispatcher run that
        # read the old settings would otherwise put the fixes back after.
        apply_lock = await asyncio.to_thread(_take_apply_lock)
        try:
            _save_settings(fresh)
            # Power save first, as uninstall orders it: the files that hold
            # it off past a reboot are what last longest, and a Reset cut off
            # during the pins, which can take half a minute, left them in
            # force under a toggle already saved off.
            await undo("power save", self._release_power_save, before)
        finally:
            _drop_apply_lock(apply_lock)
        # Nothing known taken off if that step failed outright: every list
        # stays tracked, for the leftover cleanup or another Reset, Allow
        # WPA2 on the list the poll retries (as saved above).
        tracked = {key: list(before.get(key, [])) for key in (
            "bssid_lock_uuids", "band_preference_uuids", "ipv6_uuids", "dns_uuids")}
        tracked["wpa2_uuids"] = []
        tracked["wpa2_restore_uuids"] = list(fresh["wpa2_restore_uuids"])
        left = await undo(
            "the profile pins", self._clear_profile_pins,
            time.monotonic() + self._RESET_PIN_BUDGET, before, default=tracked,
        )
        await undo("the region", self._release_regdomain, before)
        # After a buffer tuning or CAKE change already running, undone too.
        async with self._queue_tuning_guard():
            await undo("buffer tuning and CAKE", self._revert_queue_tuning, before)
        try:
            _remove_no_follow(LEGACY_ENFORCED_FILE)
        except FileNotFoundError:
            pass
        for path in (ENFORCED_FILE, STREAM_FLAG_FILE, STREAM_HOLD_FILE):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        # Diagnostics too: they describe the setup being reset. In the
        # user's directory, so a directory planted at the name is logged
        # rather than allowed to fail the whole reset.
        try:
            _remove_no_follow(_diagnostics_path())
        except FileNotFoundError:
            pass
        except OSError as e:
            decky.logger.error(f"reset_settings could not remove diagnostics: {e}")
        # The confirm text promises the backend goes back; a file kept
        # because the fallback could not be enabled is a Reset not finished.
        await undo("the backend setting", self._release_generic_backend_conf)
        # A pin released before the reset is not news after it.
        self._pin_released_at = 0
        self._released_at = {}
        self._repoll_after_release = False
        self._offline_since = None
        self._stream_match = None

        # Uninstalled meanwhile (it waits for Reset only so long): putting
        # the dispatcher and settings file back would leave them behind.
        if getattr(self, "_tearing_down", False):
            return dict(self._TEARING_DOWN_RESULT)

        # Auto-fix is on in the defaults, so the script has to be there for
        # the toggle to tell the truth: with auto-fix off before, there was
        # none. With every fix off it applies nothing.
        installed = self._install_dispatcher()
        if not installed:
            self._remove_dispatcher()
        settings = _load_settings()
        settings.update(left)
        settings["auto_fix_on_wake"] = installed
        # Only the kinds still on some profile stay ours (_owned_kinds_left).
        settings["pins_owned"] = _owned_kinds_left(settings.get("pins_owned", []), left)
        # What Valve's file says now that nothing of ours is in it is the
        # user's own setting, and is recorded as such. The "?" the defaults
        # carry let a later uninstall take the user's 2 for ours.
        settings["valve_powersave_before"] = self._valve_powersave_record()
        _save_settings(settings)
        if any(left.values()):
            decky.logger.info(
                f"Reset left {sum(len(v) for v in left.values())} profile pin(s) "
                "it could not take off; they stay tracked and are retried"
            )

        decky.logger.info("Settings reset to defaults")
        if not_undone:
            # The settings are the defaults now, but something they described
            # is still in place; the log says what.
            decky.logger.error(f"Reset left in place: {', '.join(not_undone)}")
            return {
                "success": False,
                "error": "partial",
                "message": ("Settings are reset, but some changes couldn't be undone. "
                            "Try Reset again. If it keeps happening, restart the device."),
            }
        if left and any(left.values()):
            # Pins on profiles that refused the write or ran out of time.
            # The leftover cleanup retries them while the plugin runs, so
            # pressing Reset again is not the answer.
            return {"success": True, "pins_left": True,
                    "message": ("Settings are reset. Some networks couldn't be changed "
                                "yet; the plugin retries while it runs.")}
        return {"success": True, "message": "Settings reset to defaults."}

    # ---- Streaming auto mode ----
    #
    # Off by default, and off means nothing changes: the fixes apply all the
    # time. On, power save, buffer tuning and CAKE are applied while a
    # streaming app runs (or while the user holds them on) and taken off
    # after. The lock, band, DNS and IPv6 stay global: changing them means a
    # reconnect, which would drop the very stream this is for. Idea and
    # first implementation by bassobr (issue #5).

    def _volatile_gate_open(self, settings: dict) -> bool:
        """Whether power save, buffer tuning and CAKE should be in place now."""
        return not settings.get("streaming_mode_enabled") or os.path.isfile(STREAM_FLAG_FILE)

    def _paused_by_streaming(self) -> bool:
        return not self._volatile_gate_open(_load_settings())

    def _record_paused(self, key: str) -> dict:
        """Turn a fix on while auto mode has it paused: kept, applied later."""
        settings = _load_settings()
        settings[key] = True
        _save_settings_with_timestamp(settings)
        return {"success": True, "paused": True,
                "message": "On. It applies while a streaming app is running."}

    def _stream_patterns(self, settings: dict) -> list[tuple[str, str]]:
        """(label, pattern) for every app watched, the user's own last."""
        watched = settings.get("streaming_apps", {})
        out = [(label, pattern) for app_id, label, patterns in STREAMING_APPS
               if watched.get(app_id, True) for pattern in patterns]
        kept, _refused = _parse_stream_patterns(settings.get("streaming_custom_patterns", ""))
        out.extend((pattern, pattern) for pattern in kept)
        return out

    def _game_or_stream_running(self, settings: dict) -> bool:
        """Whether Steam is running a game, or a game stream looks to be on.

        Steam starts every game through its SteamLaunch wrapper, whose
        command line carries the game's AppId; Steam itself never does.
        """
        return (self._find_stream([("game", "steamlaunch appid=")]) is not None
                or self._stream_running(settings))

    def _update_blocker(self, settings: dict) -> str | None:
        """Why an update must wait, for the panel, or None.

        Keep fixes on counts as a stream, since the user says one is on. It
        is named when it is the only reason, because "close your game or
        stream" said nothing to someone running neither.
        """
        if not self._game_or_stream_running(settings):
            return None
        held = os.path.isfile(STREAM_HOLD_FILE)
        playing = (self._find_stream([("game", "steamlaunch appid=")]) is not None
                   or self._find_stream(self._stream_patterns(settings)) is not None)
        if held and not playing:
            return "Keep fixes on is holding the streaming fixes on. Turn it off first, then update."
        if held:
            # Both, named together: one at a time, the next press after
            # closing the game was refused again for the other.
            return "Close your game or stream and turn off Keep fixes on first, then update."
        return "Close your game or stream first, then update."

    def _find_stream(self, patterns: list[tuple[str, str]],
                     wait: float = PROC_SCAN_WAIT_SECONDS) -> dict | None:
        """The first running process whose command line has a pattern in it.

        Processes of the plugin's own are never one: this backend, what it
        started (nmcli, journalctl and curl carry addresses and ids a name
        of the user's can match), the update script with everything it
        started, and the dispatcher. wait is how long to wait for a search
        already under way (see PROC_SCAN_WAIT_SECONDS).
        """
        if not patterns:
            return None
        # The status read's call (_stream_running_now): no wait, and an
        # answer too old to go by is said to be unknown.
        no_wait = getattr(_PROC_SCAN_LOCAL, "no_wait", False)
        snapshot = self._proc_snapshot(0 if no_wait else wait)
        if no_wait and (snapshot is None
                        or time.monotonic() - snapshot[0] > PROC_SCAN_FRESH_SECONDS):
            _PROC_SCAN_LOCAL.unknown = True
            return None
        if snapshot is None:
            return None
        for pid, raw in snapshot[1]:
            cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace").lower()
            if not any(pattern in cmdline for _label, pattern in patterns):
                continue
            if self._our_own_process(pid, raw):
                continue
            label = next(label for label, pattern in patterns if pattern in cmdline)
            try:
                with open(os.path.join(PROC_DIR, pid, "comm"), "rb") as f:
                    name = f.read(64).decode("utf-8", "replace").strip()
            except OSError:
                name = ""
            return {"app": label, "process": name or pid}
        return None

    def _proc_scan_state(self) -> dict:
        state = getattr(self, "_proc_scan", None)
        if state is None:
            state = self._proc_scan = {"lock": threading.Lock(), "pending": None, "last": None,
                                       "started": 0.0, "stuck_said": None}
        return state

    def _proc_snapshot(self, wait: float = PROC_SCAN_WAIT_SECONDS):
        """(monotonic time, [(pid, raw cmdline)]) from a search started now
        or already under way, or the last one finished when it has not
        answered within wait seconds. None when no search has finished yet.

        The search runs in a thread of its own, one at a time, so ten
        callers at once make one search, and one stuck in the kernel keeps
        one thread rather than every caller's.
        """
        state = self._proc_scan_state()
        with state["lock"]:
            pending = state["pending"]
            if pending is None:
                pending = state["pending"] = threading.Event()
                state["started"] = time.monotonic()
                threading.Thread(target=self._run_proc_scan, args=(pending,),
                                 name="wifi-optimizer-process-search", daemon=True).start()
        if wait > 0:
            pending.wait(wait)
        # A search stuck in the kernel (a process's cmdline read blocked in
        # D state) never finishes, and every answer after it is the last one
        # it had. Said once per stuck search, rather than nothing at all.
        if not pending.is_set():
            age = time.monotonic() - state["started"]
            if age > self._PROC_SCAN_STUCK_SECONDS and state["stuck_said"] is not pending:
                state["stuck_said"] = pending
                decky.logger.error(
                    f"process search has not finished for {age:.0f} s; auto mode is "
                    "going by the last answer")
        return state["last"]

    # How long a process search may run before it is said to be stuck.
    _PROC_SCAN_STUCK_SECONDS = 30

    def _run_proc_scan(self, done: threading.Event):
        state = self._proc_scan_state()
        entries = None
        try:
            entries = self._read_processes()
        except Exception as e:
            self._log_throttled("process_search", f"process search error: {e}")
        finally:
            with state["lock"]:
                if entries is not None:
                    state["last"] = (time.monotonic(), entries)
                if state["pending"] is done:
                    state["pending"] = None
            done.set()

    def _read_processes(self) -> list[tuple[str, bytes]]:
        """Every process's command line but this backend's own (a kernel
        thread has none and is left out)."""
        own = str(os.getpid())
        try:
            pids = [d for d in os.listdir(PROC_DIR) if d.isdigit() and d != own]
        except OSError:
            return []
        out = []
        for pid in pids:
            try:
                with open(os.path.join(PROC_DIR, pid, "cmdline"), "rb") as f:
                    raw = f.read(4096)
            except OSError:
                continue
            if raw:
                out.append((pid, raw))
        return out

    def _our_own_process(self, pid: str, raw: bytes) -> bool:
        """Whether pid is the update script, the dispatcher, or started by
        either or by this backend (followed up a few parents at most); or is
        never a stream: a Decky plugin's backend or Decky's PluginLoader
        (_never_a_stream), as the update script's playing() has it."""
        if self._never_a_stream(raw):
            return True
        own = str(os.getpid())
        for _ in range(8):
            argv = raw.split(b"\0")[:2]
            for arg in argv:
                text = arg.decode("utf-8", "replace")
                if text == DISPATCHER_PATH or _UPDATE_SCRIPT_ARG_RE.match(text):
                    return True
            try:
                with open(os.path.join(PROC_DIR, pid, "status"), "rb") as f:
                    status = f.read(4096).decode("utf-8", "replace")
            except OSError:
                return False
            found = re.search(r"^PPid:\s*(\d+)", status, re.M)
            if not found:
                return False
            pid = found.group(1)
            if pid == own:
                return True
            if pid in ("0", "1"):
                return False
            try:
                with open(os.path.join(PROC_DIR, pid, "cmdline"), "rb") as f:
                    raw = f.read(4096)
            except OSError:
                return False
        return False

    # Decky titles every plugin backend "<Name> (<plugin dir>/main.py)".
    # The same shape as playing()'s glob in the update script.
    _PLUGIN_BACKEND_TITLE = re.compile(rb" \(/.*/homebrew/plugins/.*/main\.py\)", re.S)

    @classmethod
    def _never_a_stream(cls, raw: bytes) -> bool:
        """Whether a process, by its own argv[0], is a Decky plugin's backend
        (a name like "moondeck" matched MoonDeck's, which always runs, and held
        the fixes on for good) or Decky's PluginLoader."""
        arg0 = raw.split(b"\0")[0]
        if cls._PLUGIN_BACKEND_TITLE.search(arg0):
            return True
        return arg0.rsplit(b"/", 1)[-1] in (b"PluginLoader", b"plugin_loader")

    def _set_stream_flag(self, on: bool, path: str | None = None):
        path = path or STREAM_FLAG_FILE
        if on:
            try:
                os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
            except OSError:
                pass
            # Renamed over a flag already there, so the dispatcher never
            # finds it missing mid-stream (_replace_no_follow).
            _replace_no_follow(path, f"{int(time.time())}\n")
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def _revert_volatile_fixes(self, settings: dict):
        """Take power save, buffer tuning and CAKE off, keeping them chosen.

        Every step is tried whatever the one before it did, as
        _release_power_save does: a driver parameter that would not take
        (a read-only file, an I/O error) used to leave buffer tuning and
        CAKE on after the stream, with the fixes already flagged off.
        """
        def radio_power_save_on():
            # Every radio set_power_save turned it off on (contract C2).
            radios = [r for r in dict.fromkeys(self._power_save_radios()) if r]
            # A 2 in Valve's file is Steam's own Wi-Fi power management
            # switched off; power save then stays off because the user said.
            if radios and self._valve_powersave() != "2":
                for iface in radios:
                    self._run_cmd(["/usr/bin/iw", "dev", iface, "set", "power_save", "on"])

        steps = []
        if settings.get("power_save_disabled"):
            steps += [("radio power save", radio_power_save_on),
                      ("driver fixes", lambda: self._apply_driver_fixes(False)),
                      ("PCIe power states", lambda: self._apply_pcie_aspm_fix(False))]
        steps.append(("buffer tuning and CAKE", lambda: self._revert_queue_tuning(settings)))
        for what, undo in steps:
            try:
                undo()
            except Exception as e:
                decky.logger.error(f"Streaming auto mode, taking off {what}: {e}")

    async def _stream_pass(self, settle: bool = False):
        """Look for a stream, and apply or take off the fixes if that changed.

        settle skips the wait before taking them off, for when the panel
        knows something just exited or the user changed what is watched.

        The search runs before the guard is taken. It reads every process's
        command line, and a read of one stuck in the kernel never returns:
        held across it, the guard kept Reset and the auto mode toggle
        waiting for good.
        """
        settings = _load_settings()
        if not settings.get("streaming_mode_enabled"):
            return
        seen = getattr(self, "_stream_epoch", 0)
        found = await asyncio.to_thread(self._find_stream, self._stream_patterns(settings))
        async with self._stream_guard():
            # A settled pass, Reset or the toggle that ran while this one
            # searched has changed what it searched for, or taken the fixes
            # off on its own newer answer. Searched again, now the guard is
            # held: going on with the old answer put the fixes back on right
            # after they came off. The search is bounded now
            # (PROC_SCAN_WAIT_SECONDS), so it cannot hold the guard for good.
            if getattr(self, "_stream_epoch", 0) != seen:
                found = await asyncio.to_thread(
                    self._find_stream, self._stream_patterns(_load_settings()))
            await self._stream_pass_locked(settle, found)
            if settle:
                self._bump_stream_epoch()

    def _bump_stream_epoch(self):
        """Say that what a search made before the guard found is out of date
        (see _stream_pass). Only called with the stream guard held."""
        self._stream_epoch = getattr(self, "_stream_epoch", 0) + 1

    def _stream_guard(self) -> asyncio.Lock:
        """Held for every change to whether the fixes are on, so a pass and a
        toggle never apply and take off the fixes at the same time."""
        if getattr(self, "_stream_lock", None) is None:
            self._stream_lock = asyncio.Lock()
        return self._stream_lock

    async def _stream_pass_locked(self, settle: bool = False, found: dict | None = None):
        """_stream_pass's work, with the guard held. found is the stream the
        search (made before the guard was taken) found running, or None."""
        # Looked at again now the guard is held. Everything that turns auto
        # mode off takes the guard, but the search ran before this pass had
        # it, and a pass that went on regardless would put the fixes back
        # under settings that no longer have auto mode at all.
        settings = _load_settings()
        if not settings.get("streaming_mode_enabled"):
            return
        held = os.path.isfile(STREAM_HOLD_FILE)
        was_open = os.path.isfile(STREAM_FLAG_FILE)
        if found or held:
            self._stream_match = found
            self._stream_misses = 0
            want = True
        else:
            self._stream_misses = getattr(self, "_stream_misses", 0) + 1
            want = (was_open and not settle
                    and self._stream_misses < STREAM_MISSES_TO_CLOSE)
        if not want:
            # Kept through the countdown above, so the panel goes on naming
            # the stream until the fixes actually come off.
            self._stream_match = None
        if want == was_open:
            return
        if want:
            self._set_stream_flag(True)
            why = f"{found['app']} ({found['process']})" if found else "held on"
            decky.logger.info(f"Streaming auto mode: {why}, applying the fixes")
            self._gate_opening = True
            try:
                await self.reapply_volatile()
            finally:
                self._gate_opening = False
        else:
            # The flag comes off and the fixes after it with the apply lock
            # held, so a dispatcher run that saw the flag still there cannot
            # put them back once they are off. After any buffer tuning or
            # CAKE change already running (the guard is taken before the
            # lock, as every holder of both does), so what it applied comes
            # off too.
            async with self._queue_tuning_guard():
                async with _apply_lock_async():
                    self._set_stream_flag(False)
                    decky.logger.info("Streaming auto mode: no stream running, fixes off")
                    await asyncio.to_thread(self._revert_volatile_fixes, settings)

    async def _streaming_watch(self):
        """Look every few seconds for as long as auto mode is on."""
        while True:
            await asyncio.sleep(STREAM_POLL_SECONDS)
            try:
                if not _load_settings().get("streaming_mode_enabled"):
                    # The defaults standing in for a file that could not be
                    # read say off too. Ending the watch on one of those left
                    # the fixes as they were until the next reload.
                    if _SETTINGS_STATE["read_failed"]:
                        continue
                    return
                await self._stream_pass()
            except Exception as e:
                self._log_throttled("stream_watch", f"streaming watch error: {e}")

    def _start_streaming_watch(self):
        task = getattr(self, "_stream_task", None)
        if task is None or task.done():
            self._stream_task = asyncio.create_task(self._streaming_watch())

    async def _drop_persistent_power_save(self):
        """Remove what keeps power save off beyond a session.

        NetworkManager's file holds it off on every connect and the modprobe
        options at every module load, both whether or not anything streams.

        The files and NetworkManager's reload are worked in a thread, and
        what is recorded is saved here on the event loop. Saved from the
        thread, it raced every setter on the loop: whichever wrote last
        undid the other's change.
        """
        found = await asyncio.to_thread(self._remove_persistent_power_save)
        settings = _load_settings()
        settings["valve_powersave_before"] = found
        # Best effort: the record is worked out again from the file the next
        # time. A save that raised here ended startup's streaming pickup and
        # the auto mode toggle before either started the watch.
        try:
            _save_settings(settings)
        except OSError as e:
            self._log_throttled("settings_save", f"Could not save the power save record: {e}")

    def _remove_persistent_power_save(self) -> str:
        """_drop_persistent_power_save's file work. Returns what to record as
        Valve's power save value from before."""
        # Ours and the modprobe options removed, Valve's line put back (only
        # while our file was there to be removed: without it a 2 there is the
        # user's Steam setting), and NetworkManager told to reread its config
        # once, after Valve's line. Reloaded before it, it read our 2 still
        # there and kept power save off until the next reload.
        self._release_power_save_files(_load_settings())
        # With our file gone nothing of ours can reach Valve's any more, so
        # what it says now is the user's own (Steam's Wi-Fi power management
        # setting). Recorded, so a later Reset or uninstall leaves it alone
        # rather than taking an unrecorded 2 for ours.
        return self._valve_powersave_record()

    def _reload_nm_conf(self):
        """Have NetworkManager read its config files again.

        It reads them at startup and keeps them, so a file of ours removed
        mid-boot would otherwise go on holding power save off at every
        reconnect until the next reboot. Reloading the config leaves
        connections alone.
        """
        result = self._run_cmd(["/usr/bin/nmcli", "general", "reload", "conf"], timeout=10)
        if not result["success"]:
            decky.logger.info(f"nmcli general reload conf: {_tool_text(result['stderr'][:120])}")

    async def _startup_streaming(self, settings: dict):
        """Pick auto mode up where it was, before the startup apply runs.

        A flag left in /run says a stream was running before this load (the
        plugin was reloaded or updated mid-stream). Looking first keeps the
        fixes on through that instead of taking them off and putting them
        back a few seconds later.
        """
        if not settings.get("streaming_mode_enabled"):
            for path in (STREAM_FLAG_FILE, STREAM_HOLD_FILE):
                self._set_stream_flag(False, path)
            return
        await self._drop_persistent_power_save()
        await self._stream_pass(settle=True)
        self._start_streaming_watch()

    def _streaming_live(self, settings: dict) -> dict:
        """What the panel shows about auto mode."""
        if not settings.get("streaming_mode_enabled"):
            return {"streaming_mode": False}
        match = getattr(self, "_stream_match", None) or {}
        return {
            "streaming_mode": True,
            "streaming_active": os.path.isfile(STREAM_FLAG_FILE),
            "streaming_held": os.path.isfile(STREAM_HOLD_FILE),
            "streaming_app": match.get("app", ""),
            "streaming_process": match.get("process", ""),
        }

    async def set_streaming_mode(self, enabled: bool) -> dict:
        """Turn streaming auto mode on or off."""
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        enabled = enabled is True
        saved = False
        try:
            # And not while the backend switches: it rewrites Valve's
            # NetworkManager file, which this reads and writes too.
            if self._profile_change_refused():
                return self._busy_result()
            # Searched before the guard is taken, for the reason _stream_pass
            # gives: a search that never returns must not hold it.
            found = None
            seen = getattr(self, "_stream_epoch", 0)
            if enabled:
                found = await asyncio.to_thread(
                    self._find_stream, self._stream_patterns(_load_settings()))
            # Taken first, so a pass already applying or taking off the fixes
            # (in a worker thread, which cancelling would not stop) finishes
            # before this changes anything. The watch then ends on its own.
            async with self._stream_guard():
                if enabled and getattr(self, "_stream_epoch", 0) != seen:
                    found = await asyncio.to_thread(
                        self._find_stream, self._stream_patterns(_load_settings()))
                self._bump_stream_epoch()
                settings = _load_settings()
                settings["streaming_mode_enabled"] = bool(enabled)
                _save_settings_with_timestamp(settings)
                saved = True
                if enabled:
                    # Under the power save guard, so a set_power_save already
                    # under way cannot write the modprobe options and
                    # NetworkManager's file back after they are dropped. Not
                    # held over the pass below, which may call set_power_save.
                    async with self._power_save_guard():
                        await self._drop_persistent_power_save()
                    # Until now the fixes applied all the time, so they are
                    # on. A settled pass takes them off unless a stream runs.
                    self._set_stream_flag(True)
                    self._stream_misses = 0
                    await self._stream_pass_locked(True, found)
                else:
                    for path in (STREAM_FLAG_FILE, STREAM_HOLD_FILE):
                        self._set_stream_flag(False, path)
                    self._stream_match = None
                    # Back to all the time, NetworkManager's file included.
                    await self.reapply_volatile()
            if enabled:
                self._start_streaming_watch()
            decky.logger.info(f"Streaming auto mode {'on' if enabled else 'off'}")
            return {"success": True, "streaming_mode": bool(enabled)}
        except Exception as e:
            decky.logger.error(f"set_streaming_mode error: {e}")
            # Saved on, so the watch runs whatever failed after the save:
            # without it auto mode reads on and never follows a stream.
            if enabled and saved:
                self._start_streaming_watch()
            return self._unexpected_response(e)

    async def set_streaming_hold(self, held: bool) -> dict:
        """Keep the fixes on until reboot, for a stream that is not detected."""
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        held = held is True
        try:
            if not _load_settings().get("streaming_mode_enabled"):
                return {"success": False, "message": "Streaming auto mode is off."}
            self._set_stream_flag(bool(held), STREAM_HOLD_FILE)
            await self._stream_pass(settle=True)
            return {"success": True, "held": bool(held)}
        except Exception as e:
            decky.logger.error(f"set_streaming_hold error: {e}")
            return self._unexpected_response(e)

    async def set_streaming_app(self, app_id: str, watched: bool) -> dict:
        """Watch or ignore one of the known streaming apps."""
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        watched = watched is True
        try:
            if app_id not in STREAMING_APP_IDS:
                return {"success": False, "message": "Unknown app."}
            settings = _load_settings()
            apps = dict(settings.get("streaming_apps", {}))
            apps[app_id] = bool(watched)
            settings["streaming_apps"] = apps
            _save_settings(settings)
            await self._stream_pass(settle=True)
            return {"success": True}
        except Exception as e:
            decky.logger.error(f"set_streaming_app error: {e}")
            return self._unexpected_response(e)

    async def set_streaming_patterns(self, text: str) -> dict:
        """Save the user's own process patterns, refusing ones that would
        match something always running."""
        try:
            kept, refused = _stream_pattern_verdicts(str(text)[:512])
            settings = _load_settings()
            settings["streaming_custom_patterns"] = " ".join(kept)
            _save_settings(settings)
            await self._stream_pass(settle=True)
            result = {"success": True, "patterns": " ".join(kept)}
            if refused:
                result["message"] = self._stream_refusal_message(refused)
            return result
        except Exception as e:
            decky.logger.error(f"set_streaming_patterns error: {e}")
            return self._unexpected_response(e)

    @staticmethod
    def _stream_refusal_message(refused: list[tuple[str, str]]) -> str:
        """What the panel says about the names it left out, with the reason
        each one broke rather than every rule at once."""
        reasons = {
            "ascii": "use plain letters and digits",
            "short": f"too short, use at least {STREAM_PATTERN_MIN} characters",
            "long": f"too long, use at most {STREAM_PATTERN_MAX} characters",
            "always": "matches something that is always running, like Steam",
            "limit": f"only {STREAM_PATTERNS_MAX} names are kept",
        }
        parts = []
        for why, text in reasons.items():
            names = [t if len(t) <= 24 else t[:21] + "..." for t, w in refused if w == why]
            if names:
                parts.append(f"{', '.join(names)} ({text})")
        return "Left out: " + "; ".join(parts) + "."

    def _streaming_app_list(self) -> list[dict]:
        """The known apps, for the panel's list."""
        watched = _load_settings().get("streaming_apps", {})
        return [{"id": app_id, "label": label, "watched": watched.get(app_id, True)}
                for app_id, label, _patterns in STREAMING_APPS]

    async def list_streaming_apps(self) -> dict:
        try:
            return {"success": True, "apps": self._streaming_app_list()}
        except Exception as e:
            decky.logger.error(f"list_streaming_apps error: {e}")
            return {"success": False, "apps": []}

    async def poke_detection(self) -> dict:
        """Look now. The panel calls this when Steam starts or stops a game.

        Counted as an ordinary pass: the panel pokes again a few seconds
        later, so a stream app that is restarting is seen again before its
        fixes would come off.
        """
        try:
            await self._stream_pass()
            return {"success": True}
        except Exception as e:
            decky.logger.error(f"poke_detection error: {e}")
            return {"success": False}

    # ---- Updates ----

    async def set_update_channel(self, channel: str) -> dict:
        """Set the update channel to 'stable' or 'beta'."""
        try:
            if channel not in ("stable", "beta"):
                return {"success": False, "message": "Choose Stable or Beta."}
            settings = _load_settings()
            settings["update_channel"] = channel
            _save_settings(settings)
            decky.logger.info(f"Update channel set to {channel}")
            return {"success": True, "channel": channel}
        except Exception as e:
            decky.logger.error(f"set_update_channel error: {e}")
            return self._unexpected_response(e)

    async def set_update_notify(self, enabled: bool) -> dict:
        """Turn the available-update toast on or off."""
        # From the panel. Anything but a real True counts as False, so a
        # stray value is never saved as is.
        enabled = enabled is True
        try:
            settings = _load_settings()
            settings["update_notify"] = bool(enabled)
            _save_settings(settings)
            decky.logger.info(f"Update notices {'on' if enabled else 'off'}")
            return {"success": True, "enabled": bool(enabled)}
        except Exception as e:
            decky.logger.error(f"set_update_notify error: {e}")
            return self._unexpected_response(e)

    async def update_notice(self) -> dict:
        """Whether the background check should announce an update now.

        Called by the plugin whether or not the panel is open. notify is set
        for a newer release on the user's channel that has not been announced
        yet; a "go back to" offer on the beta channel is not announced. busy
        says a stream looks to be running, so the toast waits. retry says the
        check itself failed and is worth trying again sooner. Nothing here
        installs anything.
        """
        try:
            settings = _load_settings()
            # While an update installs, the one on offer is likely the one
            # being installed.
            if not settings.get("update_notify", True) or self._update_running():
                return {"notify": False}
            info = await self.check_for_update()
            if not info.get("success"):
                return {"notify": False, "retry": True}
            latest = info.get("latest_version", "")
            notify = bool(
                info.get("update_available") and not info.get("is_downgrade")
                and latest and latest != settings.get("update_notified_version", "")
            )
            busy = notify and await asyncio.to_thread(self._stream_running, settings)
            return {"notify": notify, "version": latest, "busy": bool(busy)}
        except Exception as e:
            decky.logger.error(f"update_notice error: {e}")
            return {"notify": False, "retry": True}

    async def mark_update_notified(self, version: str) -> dict:
        """Record that the update to version has been announced, so it is not
        announced again after a reload or a reboot."""
        try:
            if not isinstance(version, str) or len(version) > 64 or not VERSION_RE.match(version):
                return {"success": False, "message": "Not a version."}
            settings = _load_settings()
            settings["update_notified_version"] = version
            _save_settings(settings)
            decky.logger.info(f"Announced the update to v{version}")
            return {"success": True}
        except Exception as e:
            decky.logger.error(f"mark_update_notified error: {e}")
            return self._unexpected_response(e)

    @staticmethod
    def _version_parts(version: str) -> tuple[tuple[int, ...], int]:
        """(numbers, release rank) for comparison. A prerelease sorts first.

        "0.12.2-beta" is EARLIER than "0.12.2" and later than "0.12.1", which
        a plain string compare gets wrong in both directions.
        """
        head, _, tail = version.partition("-")
        numbers = []
        for piece in head.split("."):
            try:
                numbers.append(int(piece))
            except ValueError:
                numbers.append(0)
        return tuple(numbers), 0 if tail else 1

    def _is_older(self, candidate: str, than: str) -> bool:
        return self._version_parts(candidate) < self._version_parts(than)

    async def _github_api(self, url: str) -> dict:
        """GET a GitHub API url. The _run_cmd result, with an answer too
        large to be a real one turned into a failure."""
        result = await asyncio.to_thread(
            self._run_cmd,
            [
                "/usr/bin/curl", "-sL", "--proto", "=https", "--connect-timeout", "3", "--max-time", "10",
                "--max-filesize", str(RELEASES_LIST_MAX),
                "-H", "Accept: application/vnd.github.v3+json", url,
            ],
            15,
        )
        if len(result.get("stdout") or "") > RELEASES_LIST_MAX:
            return {**result, "success": False, "stderr": "response too large"}
        return result

    @classmethod
    def _newest_release(cls, releases) -> str:
        """The highest version among published releases, or "".

        Highest by version, not newest by date: a stable release newer than
        every beta is the one a beta user should be offered, and the order
        releases were made in says nothing about which is ahead. Drafts and
        any tag that is not "v" and a version are skipped, never repaired.
        Ties between prereleases of one number go to the later name, so the
        answer does not depend on the order GitHub lists them in.
        """
        if not isinstance(releases, list):
            return ""
        best = None
        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            tag = release.get("tag_name")
            if not isinstance(tag, str) or not tag.startswith("v"):
                continue
            version = tag[1:]
            if len(version) > 64 or not VERSION_RE.match(version):
                continue
            key = (cls._version_parts(version), version)
            if best is None or key > best:
                best = key
        return best[1] if best else ""

    async def check_for_update(self) -> dict:
        """Check GitHub's releases for a newer version on the chosen channel.

        Stable asks for the latest release, which GitHub never answers with
        a prerelease. Beta looks at recent releases of both kinds and takes
        the highest version.
        """
        try:
            current = decky.DECKY_PLUGIN_VERSION
            settings = _load_settings()
            channel = settings.get("update_channel", "stable")
            decky.logger.info(f"Update check: current={current}, channel={channel}")

            if channel == "beta":
                result = await self._github_api(f"{RELEASES_API}?per_page=30")
            else:
                result = await self._github_api(f"{RELEASES_API}/latest")

            if not result["success"] or not result["stdout"]:
                decky.logger.error(
                    f"Update check: curl failed - rc={result.get('returncode')}, "
                    f"stderr={_tool_text(result.get('stderr', '')[:200])}"
                )
                return {
                    "success": False,
                    "current_version": current,
                    "update_available": False,
                    "channel": channel,
                    "message": "Couldn't reach GitHub to check for updates. Check the connection and try again.",
                }

            data = json.loads(result["stdout"])

            if channel == "beta":
                latest = self._newest_release(data)
            else:
                tag = data.get("tag_name", "") if isinstance(data, dict) else ""
                latest = tag[1:] if isinstance(tag, str) and tag.startswith("v") else tag if isinstance(tag, str) else ""

            if latest and (len(latest) > 64 or not VERSION_RE.match(latest)):
                # Refuse rather than sanitise. This value reaches a download
                # URL and a root shell script, and a version that does not
                # look like a version means something is wrong upstream.
                decky.logger.error(f"Update check: refusing malformed version {latest!r}")
                return {
                    "success": False,
                    "current_version": current,
                    "update_available": False,
                    "channel": channel,
                    "message": "Received an unexpected version from GitHub.",
                }

            if not latest:
                # GitHub explains a refusal (a rate limit) in "message". It is
                # never shown, being English for developers, and only its kind
                # is logged: a rate limit names the network's public address,
                # and people post this log in public threads.
                raw = data.get("message") if isinstance(data, dict) else None
                raw = raw if isinstance(raw, str) else ""
                if "rate limit" in raw.lower():
                    kind = "rate limit"
                else:
                    kind = "other: " + (_mask_addresses(raw[:200]) or "none given")
                decky.logger.error(f"Update check: no version - {kind}")
                if "rate limit" in raw.lower():
                    msg = ("GitHub is limiting update checks from this network "
                           "for now. Try again in an hour.")
                else:
                    msg = "GitHub didn't list a release to update to. Try again later."
                return {
                    "success": False,
                    "current_version": current,
                    "update_available": False,
                    "channel": channel,
                    "message": msg,
                }

            # Beta: update if versions differ (allows downgrade back to stable)
            # Stable: update only if newer (strip -beta suffix for comparison)
            if channel == "beta":
                update_available = latest != current
            elif "-" in latest:
                # Stable users must never be offered a prerelease. This only
                # happens if a beta tag is published without the prerelease
                # flag, and the version compare below would strip the suffix
                # and treat it as stable.
                decky.logger.error(
                    f"Update check: ignoring prerelease {latest!r} on stable channel"
                )
                return {
                    "success": True,
                    "current_version": current,
                    "latest_version": current,
                    "update_available": False,
                    "channel": channel,
                }
            else:
                current_clean = current.split("-")[0]
                latest_clean = latest.split("-")[0]
                current_tuple = tuple(int(x) for x in current_clean.split("."))
                latest_tuple = tuple(int(x) for x in latest_clean.split("."))
                update_available = latest_tuple > current_tuple or (
                    "-beta" in current and latest_tuple >= current_tuple
                )

            # On the beta channel any difference counts, which deliberately
            # includes going BACKWARDS - a beta whose tag was pulled needs a
            # way home. Saying so is the part that was missing: offered as
            # "update available", one press silently replaces a newer build
            # with an older one and nothing tells the user which way it goes.
            going_back = update_available and self._is_older(latest, current)

            decky.logger.info(
                f"Update check: current={current}, latest={latest}, "
                f"channel={channel}, update={update_available}"
                + (", direction=downgrade" if going_back else "")
            )

            return {
                "success": True,
                "is_downgrade": going_back,
                "current_version": current,
                "latest_version": latest,
                "update_available": update_available,
                "channel": channel,
            }
        except Exception as e:
            decky.logger.error(f"check_for_update error: {e}")
            return {
                "success": False,
                "current_version": decky.DECKY_PLUGIN_VERSION,
                "update_available": False,
                "message": "Couldn't check for updates.",
            }

    def _fetch_release_file(self, url: str, limit: int, deadline: float) -> dict:
        """Download one small release file into memory.

        {"data": bytes or None, "missing": bool}. missing means GitHub
        answered 404: the release has no such file, so it is never retried.
        A network error is tried once more if deadline (time.monotonic())
        leaves room. curl writes to a private file rather than to its output,
        because the signature covers these exact bytes and _run_cmd decodes
        and strips what it captures.
        """
        directory = tempfile.mkdtemp(prefix="wifi-optimizer-release-")
        path = os.path.join(directory, "file")
        try:
            os.chmod(directory, 0o700)
            for attempt in (1, 2):
                left = int(deadline - time.monotonic())
                limit_s = max(10, min(RELEASE_FETCH_TRY_MAX, left))
                result = self._run_cmd(
                    [
                        "/usr/bin/curl", "-sSL", "--fail", "--proto", "=https", "--tlsv1.2",
                        "--connect-timeout", "15", "--max-time", str(limit_s),
                        "--max-filesize", str(limit), "-w", "%{http_code}", "-o", path, url,
                    ],
                    limit_s + 5,
                )
                if result["success"]:
                    break
                http = result.get("stdout", "").strip()
                decky.logger.error(
                    f"Release file {url} not downloaded (try {attempt}): rc={result.get('returncode')}, "
                    f"http={http[:8]!r}, {_tool_text(result.get('stderr', '')[:200])}"
                )
                if http == "404":
                    return {"data": None, "missing": True}
                network = http == "000" or result.get("returncode") in RELEASE_FETCH_NETWORK_RC
                if attempt == 2 or not network or deadline - time.monotonic() < 20:
                    return {"data": None, "missing": False}
                time.sleep(RELEASE_FETCH_RETRY_DELAY)
            with open(path, "rb") as f:
                data = f.read(limit + 1)
            if len(data) > limit:
                decky.logger.error(f"Release file {url} is larger than {limit} bytes")
                return {"data": None, "missing": False}
            return {"data": data, "missing": False}
        except OSError as e:
            decky.logger.error(f"Release file {url} not read: {e}")
            return {"data": None, "missing": False}
        finally:
            for leftover, remove in ((path, os.remove), (directory, os.rmdir)):
                try:
                    remove(leftover)
                except OSError:
                    pass

    async def _signed_release(self, version: str) -> dict:
        """The tarball's URL and signed sha256 for a release, or why not.

        {"ok": True, "url": ..., "sha256": ...} or {"ok": False, "message":
        text for the panel}. The release's SHA256SUMS must carry a signature
        from a pinned key whose trusted comment names this version, and must
        list the tarball. Anything less and nothing is installed.
        """
        def refuse(message, reason):
            decky.logger.error(f"Update to v{version} refused: {reason}")
            return {"ok": False, "message": message}

        if not isinstance(version, str) or len(version) > 64 or not VERSION_RE.match(version):
            return refuse("Received an unexpected version from GitHub.", f"version {version!r}")
        base = f"{RELEASE_DOWNLOAD}/v{version}"
        deadline = time.monotonic() + RELEASE_FETCH_BUDGET
        sums = await asyncio.to_thread(self._fetch_release_file, f"{base}/SHA256SUMS", RELEASE_SUMS_MAX, deadline)
        sig = await asyncio.to_thread(self._fetch_release_file, f"{base}/SHA256SUMS.minisig", RELEASE_SIG_MAX, deadline)
        if sums["missing"] or sig["missing"]:
            return refuse("This release isn't signed, so it wasn't installed.", "no signature files")
        if sums["data"] is None or sig["data"] is None:
            return refuse("Couldn't download the release's signature. Try again later.",
                          "signature files not downloaded")
        try:
            sig_text = sig["data"].decode("utf-8")
        except UnicodeDecodeError:
            sig_text = None
        # Pure Python arithmetic, a fraction of a second, kept off the loop.
        check = await asyncio.to_thread(verify_release_manifest, sums["data"], sig_text, version)
        if not check.get("ok"):
            return refuse("This release's signature didn't check out, so it wasn't installed.",
                          check.get("error", "verification failed"))
        name = f"wifi-optimizer-v{version}.tar.gz"
        sha256 = check["sha256"].get(name)
        if not sha256:
            return refuse("This release's signed checksums don't cover the update, so it wasn't installed.",
                          f"{name} not in SHA256SUMS")
        decky.logger.info(f"Release v{version} signature verified, {name} sha256={sha256}")
        return {"ok": True, "url": f"{base}/{name}", "sha256": sha256}

    async def apply_update(self, version: str = "") -> dict:
        """Install the update the user approved: version is the one the
        panel showed them, from its last check_for_update.

        The script runs on its own; the panel follows it through
        get_update_progress and asks Decky to reload the plugin once the new
        files are in place, with a plugin_loader restart as the fallback.
        """
        claimed = False
        try:
            self._ensure_backend_switch_state()
            if self._backend_switch.get("in_progress"):
                # The update restarts Decky, which would lose the switch's
                # progress and result part way through.
                return {"success": False, "message": "Wait for the WiFi backend switch to finish."}
            if self._update_running():
                return {"success": False, "message": "An update is already being installed."}
            if self._profile_change_in_flight():
                # Decky restarts at the end, which would cut a lock or band
                # change off between its pin write and its reconnect.
                return {"success": False, "message": "Wait for the current WiFi change to finish."}
            # Claimed before the first await, or two requests both pass the
            # check above while the version lookup runs.
            self._update_starting = True
            claimed = True
            # An update normally reloads only this plugin, which a running
            # game never notices. If that reload does not answer, the script
            # falls back to restarting plugin_loader, which restarts Steam
            # and takes the game down with it. So not during a game or a
            # stream; the update notice already waits for the same.
            settings = _load_settings()
            blocked = await asyncio.to_thread(self._update_blocker, settings)
            if blocked:
                return {"success": False, "message": blocked}
            if not isinstance(version, str) or len(version) > 64 or not VERSION_RE.match(version):
                decky.logger.error(f"Update refused: approved version {version!r} malformed")
                return {"success": False, "message": "Check for updates again, then retry."}
            info = await self.check_for_update()
            # A check that could not reach GitHub is not "no update": said
            # as such, the panel's own re-check then contradicted it.
            if not info.get("success", True):
                return {"success": False,
                        "message": info.get("message")
                        or "Couldn't check for updates. Try again in a moment."}
            if not info.get("update_available"):
                return {"success": False, "message": "No update available."}

            channel = info.get("channel", "stable")
            latest = info["latest_version"]
            # GitHub is asked again here, and its answer may not be what the
            # user saw. Installing something else would skip the choice they
            # made, a "Go back to" above all, so it has to be the same one.
            if latest != version:
                decky.logger.error(f"Update refused: approved v{version}, GitHub now offers v{latest}")
                return {"success": False, "message": "The available update changed. Check again and retry."}
            plugin_dir = decky.DECKY_PLUGIN_DIR
            # By the release itself: the beta channel also offers stable
            # releases, and "Updated to beta v0.24.5" misnamed those.
            label = f"beta v{latest}" if "-" in latest else f"v{latest}"

            # Nothing is downloaded to run until the release's signed
            # checksum list has been checked here. The script then refuses a
            # tarball whose sha256 is not the one that list gives.
            signed = await self._signed_release(latest)
            if not signed["ok"]:
                return {"success": False, "message": signed["message"]}
            download_url = signed["url"]
            sha256 = signed["sha256"]
            src_dir = f"WifiOptimizer-{latest}"

            # Values reaching the script are passed as ARGUMENTS, never
            # interpolated, so nothing derived from the network can be read as
            # shell. PATH is pinned rather than inherited, and every binary is
            # resolved through it so this still works on distros that lay out
            # /usr differently.
            script = UPDATE_SCRIPT
            # Not a fixed path under /tmp. That directory is world writable, so
            # any local user could pre-create the file we are about to write,
            # keep ownership of it, and have root execute whatever they put in
            # it. mkdtemp gives a unique directory only root can enter.
            script_dir = tempfile.mkdtemp(prefix="wifi-optimizer-update-")
            os.chmod(script_dir, 0o700)
            script_path = os.path.join(script_dir, "update.sh")
            fd = os.open(
                script_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o700,
            )
            with os.fdopen(fd, "w") as f:
                f.write(script)
            # What an earlier update left behind ("failed", "done") would be
            # read by the panel as the outcome of this one. Nothing is running
            # (checked above), so it is history.
            try:
                os.remove(UPDATE_STATE_FILE)
            except OSError:
                pass
            streams_path = os.path.join(script_dir, "streams")
            owner_path = os.path.join(script_dir, "owner")
            try:
                # The script waits for a stream to end before it restarts
                # Decky, so it is told what counts as one here: the apps
                # the user watches and their own process names, as the
                # plugin's own search does. Built in, it waited on every
                # known app, ticked or not, and never on the user's own.
                # In a file beside the script, not an argument: on its
                # command line the script's own search found the patterns
                # and took itself for a stream, every time.
                streams = _stream_process_pattern(
                    [pattern for _label, pattern in self._stream_patterns(_load_settings())]
                )
                fd = os.open(
                    streams_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "w") as f:
                    f.write(streams + "\n")
                # And this backend's PID: what it starts (curl, nmcli) is
                # never a stream to the script, as it is not to the plugin.
                fd = os.open(
                    owner_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                with os.fdopen(fd, "w") as f:
                    f.write(f"{os.getpid()}\n")
                await asyncio.to_thread(
                    self._launch_updater,
                    ["/bin/bash", script_path, script_dir, plugin_dir,
                     download_url, src_dir, label, latest, sha256],
                )
            except Exception:
                # Nothing started, so nothing will remove the directory, and
                # its presence would read as an update running until Decky
                # restarts - refusing every later update and backend switch.
                for leftover in (streams_path, owner_path, script_path, script_dir):
                    try:
                        (os.rmdir if leftover == script_dir else os.remove)(leftover)
                    except OSError:
                        pass
                raise
            # The script removes its own directory when it exits, so while
            # the directory exists an update is running.
            self._update_dir = script_dir
            self._update_started_at = time.monotonic()

            decky.logger.info(f"Update to {label} initiated (channel={channel})")
            return {"success": True, "message": f"Updating to {label}...", "version": latest}
        except Exception as e:
            decky.logger.error(f"apply_update error: {e}")
            return self._unexpected_response(e)
        finally:
            # Only the request that made the claim may drop it; a refused one
            # passing through here must not release another's.
            if claimed:
                self._update_starting = False

    def _launch_updater(self, argv: list[str]):
        """Start the update script so that nothing happening to Decky stops it.

        It runs in a transient systemd unit of its own. Started directly it
        is part of plugin_loader's control group, and a plugin_loader with
        the default KillMode stops the whole group, script included, however
        it was detached. Where systemd-run is missing or refuses, it starts
        directly as before; the script's lock stops a second copy running if
        systemd-run did start one after all.
        """
        if os.path.exists(SYSTEMD_RUN):
            unit = f"wifi-optimizer-update-{int(time.time())}-{os.getpid()}"
            result = self._run_cmd(
                [SYSTEMD_RUN, "--unit", unit, "--collect", "--quiet",
                 f"--property=RuntimeMaxSec={UPDATE_STATE_MAX_AGE}", "--", *argv],
                timeout=15,
            )
            if result["success"]:
                decky.logger.info(f"Update script started in unit {unit}")
                return
            decky.logger.error(
                f"systemd-run could not start the update ({_tool_text(result['stderr'][:200])}), "
                "starting it directly"
            )
        self._update_proc = subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_child_env(),
        )

    def _update_running(self) -> bool:
        if getattr(self, "_update_starting", False):
            return True
        # Only within the script's time limit. A script killed outright
        # (SIGKILL, once systemd gave up on its TERM) cannot remove its
        # directory, and every update, backend switch, Reset and profile
        # change was refused until Decky restarted.
        if self._update_script_alive():
            return True
        # After a reload this is a new backend that did not start the script,
        # which is still finishing. What the script reports covers that.
        phase, _, age = _read_update_state()
        return phase in UPDATE_ACTIVE_PHASES and age < UPDATE_STATE_MAX_AGE

    def _update_script_alive(self) -> bool:
        """Whether this backend can tell the update script it started is
        still running: its directory is there (it removes it as it exits)
        and its time limit, kept on the monotonic clock as systemd keeps
        the unit's, has not passed. A backend that did not start it (one
        loaded since) cannot tell, and answers False."""
        directory = getattr(self, "_update_dir", None)
        started = getattr(self, "_update_started_at", None)
        return bool(directory and os.path.isdir(directory)
                    and (started is None
                         or time.monotonic() - started < UPDATE_STATE_MAX_AGE))

    async def get_update_progress(self) -> dict:
        """How far a running update has got, for the panel to show and act on."""
        try:
            phase, version, age = _read_update_state()
            # Old by the clock is not enough: the clock runs through sleep,
            # and an update that slept part way through is still running.
            # Called failed then, the panel stopped asking Decky to reload
            # the plugin, and the script fell back to restarting Steam.
            if (phase in UPDATE_ACTIVE_PHASES and age >= UPDATE_STATE_MAX_AGE
                    and not self._update_script_alive()):
                phase = "failed"
            return {"success": True, "phase": phase, "version": version, "age": int(age),
                    "running_version": decky.DECKY_PLUGIN_VERSION}
        except Exception as e:
            decky.logger.error(f"get_update_progress error: {e}")
            return {"success": False, "phase": "", "version": "", "age": 0,
                    "running_version": ""}

    def _acknowledge_update(self):
        """Tell a waiting update script that this version is the one running.

        Without the answer the script restarts plugin_loader, which restarts
        Steam, on the assumption that the reload never happened.
        """
        phase, version, _ = _read_update_state()
        if phase == "swapped" and version == decky.DECKY_PLUGIN_VERSION:
            if _write_update_state("loaded", version):
                decky.logger.info(f"Reloaded as {version} after an update")

    # ---- WiFi backend switch (iwd / wpa_supplicant) ----

    _BACKEND_SWITCH_UNEXPECTED = (
        "The WiFi backend switch stopped on an unexpected error. If WiFi "
        "doesn't come back, restart the device."
    )

    async def _backend_switch_worker(self, target: str):
        """Background task that switches the WiFi backend with phase transitions.

        Invokes the privileged helper directly (BACKEND_HELPER, under
        /usr/bin/steamos-polkit-helpers or holo-polkit-helpers) to bypass
        pkexec, which fails from a rootful systemd context with no polkit
        agent. The helper tries to recreate a missing wlan0 itself, on phy0
        only; we parse its output to report whether that fired, then check
        for an interface whatever the driver, since stopping iwd removes it on
        every radio but rtw88, and recover it ourselves when the helper could
        not.
        """
        try:
            other = "iwd" if target == "wpa_supplicant" else "wpa_supplicant"

            # Phase: switching - write config then restart services.
            self._backend_switch["phase"] = "switching"
            decky.logger.info(
                f"backend switch: calling helper write_config target={target} "
                f"(euid={os.geteuid()}, helper={BACKEND_HELPER})"
            )
            valve_before = await asyncio.to_thread(self._valve_powersave)
            write_result = await asyncio.to_thread(
                self._run_cmd, [BACKEND_HELPER, "write_config", target], 5
            )
            # Valve's helper rewrites the file with the backend line only,
            # dropping the power save line Steam's own setting keeps there.
            # Put it back before NetworkManager restarts and reads it.
            if (valve_before in _VALVE_POWERSAVE_VALUES
                    and await asyncio.to_thread(self._valve_powersave) != valve_before):
                await asyncio.to_thread(self._set_valve_powersave, valve_before)
            decky.logger.info(
                f"backend switch: write_config result rc={write_result.get('returncode')} "
                f"stdout={_tool_text(write_result.get('stdout', '')[:200])} "
                f"stderr={_tool_text(write_result.get('stderr', '')[:200])}"
            )
            if not write_result["success"]:
                detail = (write_result.get("stderr") or write_result.get("stdout") or "")[:200]
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "message": self._friendly_backend_error(detail),
                    "detail": detail,
                }
                decky.logger.error(
                    f"backend switch failed at write_config: rc={write_result.get('returncode')}, "
                    f"detail={_tool_text(detail)}"
                )
                return

            restart_result = await asyncio.to_thread(
                self._run_cmd, [BACKEND_HELPER, "restart_units", other], 45
            )
            rs_stdout = restart_result.get("stdout", "")
            rs_stderr = restart_result.get("stderr", "")
            recovery_performed = "missing wlan0" in rs_stdout

            # Absence is the failure, not the name, and on every driver. The
            # helper says "wlan0 could not be created" by name, but an
            # interface that came back as wlpXsY is working; and the helper's
            # own recovery only knows phy0, so under wpa_supplicant a missing
            # one is recreated on the real phy before anyone is told to
            # reboot. Under iwd, which makes its own, it is waited for.
            iface, recovered = await self._settle_interface(target)
            recovery_performed = recovery_performed or recovered
            needs_reboot = not iface

            # Phase: reconnecting. Poll nmcli at 1-second cadence for up to 15s
            # to confirm WiFi actually comes back. 15s is generous for typical
            # NM reconnect (about 5s on wpa_supplicant, 1-2s on iwd) but not
            # so long that users with dead networks wait forever.
            reconnect_timed_out = False
            if not needs_reboot:
                self._backend_switch["phase"] = "reconnecting"
                elapsed = 0
                reconnected = False
                while elapsed < 15:
                    iface = await asyncio.to_thread(self._get_wifi_interface)
                    uuid = None
                    if iface:
                        uuid = await asyncio.to_thread(self._get_active_connection_uuid)
                    if iface and uuid:
                        reconnected = True
                        break
                    await asyncio.sleep(1)
                    elapsed += 1
                reconnect_timed_out = not reconnected

            # Verify final system state
            final_backend = await asyncio.to_thread(self._get_current_backend)

            if needs_reboot:
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": True,
                    "message": "The WiFi backend was switched, but WiFi didn't come back. Restart the device.",
                }
            elif not restart_result["success"] or final_backend != target:
                detail = rs_stderr[:200] or rs_stdout[:200]
                self._backend_switch["phase"] = "failed"
                self._log_detail("_backend_switch_worker", detail)
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": False,
                    "reconnect_timed_out": reconnect_timed_out,
                    "message": self._friendly_backend_error(detail),
                    "detail": detail,
                }
            else:
                self._backend_switch["phase"] = "done"
                self._backend_switch["result"] = {
                    "success": True,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": False,
                    "reconnect_timed_out": reconnect_timed_out,
                }
            decky.logger.info(
                f"backend switch: target={target}, final={final_backend}, "
                f"recovery={recovery_performed}, needs_reboot={needs_reboot}, "
                f"reconnect_timed_out={reconnect_timed_out}"
            )
        except asyncio.CancelledError:
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                "message": "The WiFi backend switch was cancelled.",
            }
            raise
        except Exception as e:
            decky.logger.error(f"_backend_switch_worker error: {e}")
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                # The exception is in the log above. Its text is not for
                # the panel, which shows this message as it is.
                "message": self._BACKEND_SWITCH_UNEXPECTED,
            }
        finally:
            self._backend_switch["in_progress"] = False

    def _recover_wlan0(self) -> bool:
        """Recreate a wlan0 that the backend switch destroyed.

        Valve's polkit helper tries this itself but calls `iw phy phy0`, with
        the phy index hardcoded. Devices whose wiphy is not phy0 - the Steam
        Deck OLED among them - fail there, and because the helper runs under
        `set -e` it aborts before restarting NetworkManager, leaving no
        interface at all. Retry with the phy this machine actually has.

        The wiphy outlives the netdev, so /sys/class/ieee80211 still lists it
        even though wlan0 is gone.
        """
        try:
            phys = os.listdir(SYSFS_IEEE80211)
        except Exception:
            return False
        if not phys:
            return False

        def already_has_netdev(phy: str) -> bool:
            # Adding a second station interface to a phy that already has one
            # is a genuine mess, and the caller reaches here whenever the
            # interface is merely named something other than wlan0.
            try:
                return bool(os.listdir(os.path.join(SYSFS_IEEE80211, phy, "device", "net")))
            except Exception:
                return False

        def driver_of(phy: str) -> str:
            # Read the module name, not the driver directory. The stored
            # driver is normalized to the DRIVER_PROFILES key (rtw88_pci and
            # rtw88_8822ce both become rtw88) while the driver directory is
            # the raw name, so comparing the two never matched on rtw88 - the
            # Steam Deck LCD - and the preference silently degraded to
            # alphabetical order.
            for probe in ("device/driver/module", "device/driver"):
                path = os.path.join(SYSFS_IEEE80211, phy, probe)
                # realpath does not raise on a missing path, it just returns
                # the normalised name - which would hand back the literal
                # "module" and then match nothing.
                if not os.path.exists(path):
                    continue
                try:
                    name = os.path.basename(os.path.realpath(path))
                except Exception:
                    continue
                if name and name != os.path.basename(probe):
                    return name
            return ""

        phys = [phy for phy in phys if not already_has_netdev(phy)]
        if not phys:
            return False

        # Prefer the radio this device is actually built around. With a USB
        # adapter plugged in there is more than one phy, and picking the wrong
        # one brings WiFi back on the wrong hardware while the internal radio
        # stays dead - reported as a successful recovery.
        wanted = _load_settings().get("driver", "")

        def mismatch(phy: str) -> bool:
            name = driver_of(phy)
            if not name or not wanted:
                return True
            # Either direction: the stored value may be the normalized family
            # name while the module is a variant of it, or the reverse.
            return not (name.startswith(wanted) or wanted.startswith(name))

        def on_pci(phy: str) -> bool:
            # An rtw88 USB adapter has a module that matches as well as the
            # built-in card's does; the bus tells them apart.
            path = os.path.join(SYSFS_IEEE80211, phy, "device", "subsystem")
            return os.path.exists(path) and os.path.basename(os.path.realpath(path)) == "pci"

        phys.sort(key=lambda phy: (mismatch(phy), not on_pci(phy), phy))

        # The first wlanN nobody has: another radio may already be wlan0.
        try:
            taken = set(os.listdir(SYSFS_NET))
        except OSError:
            taken = set()
        name = next(f"wlan{i}" for i in range(len(taken) + 1) if f"wlan{i}" not in taken)

        for phy in phys:
            result = self._run_cmd(
                ["/usr/bin/iw", "phy", phy, "interface", "add", name,
                 "type", "station"],
                timeout=10,
            )
            if result["success"]:
                decky.logger.info(f"Recreated {name} on {phy}")
                self._run_cmd(
                    ["/usr/bin/systemctl", "restart", "NetworkManager"],
                    timeout=30,
                )
                return True
            decky.logger.error(
                f"{name} recovery on {phy} failed: {_tool_text(result.get('stderr', '')[:120])}"
            )
        return False

    async def _steamos_manager_backend_switch_worker(self, target: str):
        """Backend switch through steamos-manager (SteamOS 3.8+).

        This is the path the OS settings UI uses. It is preferred over the
        polkit helper for two concrete reasons: the helper recreates a missing
        wlan0 with a hardcoded `iw phy phy0`, which fails outright on devices
        whose wiphy is not phy0 and leaves NetworkManager stopped with no
        interface; and the helper rewrites the Valve config file with only the
        backend stanza, discarding the Wi-Fi power management setting the OS
        stores alongside it.
        """
        try:
            self._backend_switch["phase"] = "switching"
            decky.logger.info(f"steamos-manager backend switch -> {target}")

            cmd = self._session_bus_cmd([STEAMOSCTL, "set-wifi-backend", target])
            if not cmd:
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "message": ("Couldn't reach the SteamOS settings service. Try "
                                "again in a moment. If it keeps failing, restart "
                                "the device."),
                }
                return

            valve_before = await asyncio.to_thread(self._valve_powersave)
            result = await asyncio.to_thread(self._run_cmd, cmd, 45)
            # steamos-manager rewrites Valve's file from the merged config,
            # ours included, so our power save value lands in it. Put Valve's
            # own back, whatever the outcome of the switch.
            if (await asyncio.to_thread(self._valve_powersave) != valve_before
                    and os.path.exists(NM_CONF_PATH)
                    and valve_before in _VALVE_POWERSAVE_VALUES + (None,)):
                await asyncio.to_thread(self._set_valve_powersave, valve_before)
            if not result["success"]:
                detail = (result.get("stderr") or result.get("stdout") or "")[:200]
                decky.logger.error(f"steamos-manager switch failed: {_tool_text(detail)}")
                # A failure can come after the old backend was already
                # stopped - steamos-manager 26.2.1 failed exactly there on any
                # wiphy other than phy0 - so leave the device with an
                # interface if one can be had, whichever backend won.
                in_force = (
                    await asyncio.to_thread(self._steamosctl_backend)
                    or await asyncio.to_thread(self._get_current_backend)
                )
                iface, recovered = await self._settle_interface(in_force)
                self._backend_switch["phase"] = "failed"
                self._log_detail("_steamos_manager_backend_switch_worker", detail)
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "backend": in_force,
                    "recovery_performed": recovered,
                    "needs_reboot": not iface,
                    "message": self._friendly_backend_error(detail),
                    "detail": detail,
                }
                return

            # Recover the interface BEFORE waiting for a reconnection. iwd
            # destroys the netdev when it stops on ath11k, and waiting first
            # means spending the whole timeout against an interface that does
            # not exist yet, then reporting "WiFi didn't reconnect" about a
            # switch that recovered and works.
            self._backend_switch["phase"] = "reconnecting"
            iface, recovery_performed = await self._settle_interface(target)
            needs_reboot = not iface

            # Only now is it meaningful to wait for an association.
            reconnect_timed_out = True
            if not needs_reboot:
                for _ in range(15):
                    iface = await asyncio.to_thread(self._get_wifi_interface)
                    if iface:
                        uuid = await asyncio.to_thread(
                            self._get_active_connection_uuid
                        )
                        if uuid:
                            reconnect_timed_out = False
                            break
                    await asyncio.sleep(1)

            # Ask steamos-manager rather than reading the config file it
            # writes. This is the one place the answer decides whether the
            # user is told the switch worked, so it should come from the
            # service that performed it. The per-poll reader keeps its own
            # path, which is cheap when a conf file declares the backend.
            final_backend = await asyncio.to_thread(self._steamosctl_backend)
            if final_backend is None:
                final_backend = await asyncio.to_thread(self._get_current_backend)

            if needs_reboot:
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": True,
                    "reconnect_timed_out": reconnect_timed_out,
                    "message": "The WiFi backend was switched, but WiFi didn't come back. Restart the device.",
                }
            elif final_backend == target:
                self._backend_switch["phase"] = "done"
                self._backend_switch["result"] = {
                    "success": True,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": False,
                    "reconnect_timed_out": reconnect_timed_out,
                }
            else:
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": False,
                    "reconnect_timed_out": reconnect_timed_out,
                    "message": (
                        f"Expected {target} but the system reports "
                        f"{final_backend or 'no backend'}. Restart the device, "
                        f"then try the switch again."
                    ),
                }

            decky.logger.info(
                f"steamos-manager backend switch: target={target}, "
                f"final={final_backend}, iface={iface}, "
                f"recovery={recovery_performed}, needs_reboot={needs_reboot}, "
                f"reconnect_timed_out={reconnect_timed_out}"
            )
        except asyncio.CancelledError:
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                "message": "The WiFi backend switch was cancelled.",
            }
            raise
        except Exception as e:
            decky.logger.error(f"_steamos_manager_backend_switch_worker error: {e}")
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                # The exception is in the log above. Its text is not for
                # the panel, which shows this message as it is.
                "message": self._BACKEND_SWITCH_UNEXPECTED,
            }
        finally:
            self._backend_switch["in_progress"] = False

    async def _settle_interface(self, backend: str | None) -> tuple:
        """(interface, whether we recreated it) once a backend restart settles.

        Recreating wlan0 is the wpa_supplicant case only. iwd creates its own
        netdev and names it after the phy (wlan31 on a Deck OLED), so adding
        one under it puts a second station interface on the phy iwd is about
        to claim; under iwd the right move is to give it longer.

        With the managed card known, only its netdev counts as back: a USB
        adapter's interface is not the internal card recovered.
        """
        # Let the restart settle before deciding the interface is missing.
        # Checking immediately can catch the moment it is down and add a
        # second station netdev to a phy that was about to get its own.
        await asyncio.sleep(2)
        iface = await asyncio.to_thread(self._interface_back)
        if iface:
            return iface, False
        if backend == "iwd":
            for _ in range(8):
                await asyncio.sleep(1)
                iface = await asyncio.to_thread(self._interface_back)
                if iface:
                    break
            return iface, False
        if not await asyncio.to_thread(self._recover_wlan0):
            return None, False
        await asyncio.sleep(2)
        iface = await asyncio.to_thread(self._interface_back)
        return iface, bool(iface)

    # Everything NetworkManager builds its configuration from. The status
    # poll asks for the backend every few seconds, and NetworkManager is only
    # asked again when one of these changes.
    _NM_CONF_SOURCES = (
        "/etc/NetworkManager/NetworkManager.conf",
        "/etc/NetworkManager/conf.d",
        "/run/NetworkManager/conf.d",
        "/usr/lib/NetworkManager/conf.d",
        "/var/lib/NetworkManager/NetworkManager-intern.conf",
    )

    def _nm_conf_stamp(self) -> tuple:
        stamp = []
        for path in self._NM_CONF_SOURCES:
            try:
                entries = sorted(os.listdir(path)) if os.path.isdir(path) else [""]
            except OSError:
                entries = []
            for name in entries:
                full = os.path.join(path, name) if name else path
                try:
                    st = os.stat(full)
                    stamp.append((full, st.st_mtime_ns, st.st_size))
                except OSError:
                    stamp.append((full, None, None))
        return tuple(stamp)

    def _merged_nm_backend_cached(self) -> str | None:
        stamp = self._nm_conf_stamp()
        cached = getattr(self, "_merged_backend_seen", None)
        if cached and cached[0] == stamp:
            return cached[1]
        found = self._merged_nm_backend()
        self._merged_backend_seen = (stamp, found)
        return found

    def _merged_nm_backend(self, config_dir: str | None = None) -> str | None:
        """The backend NetworkManager's merged configuration names, from NM
        itself, so the order conf.d files are read in is NM's own.

        config_dir stands in for /etc/NetworkManager/conf.d, to ask what the
        merge would be with a different set of files there.
        """
        for binary in ("/usr/bin/NetworkManager", "/usr/sbin/NetworkManager"):
            if not os.path.isfile(binary):
                continue
            args = [binary, "--print-config"]
            if config_dir:
                args.append(f"--config-dir={config_dir}")
            result = self._run_cmd(args, timeout=5)
            if not result["success"]:
                return None
            for line in result.get("stdout", "").split("\n"):
                key, sep, val = line.strip().partition("=")
                if sep and key.strip() == "wifi.backend":
                    val = val.strip()
                    return val if val in ("iwd", "wpa_supplicant") else None
            return None
        return None

    def _effective_generic_backend(self) -> str | None:
        """The backend actually in force on a generic system: the one NM is
        configured for, and only if its service is running."""
        configured = self._merged_nm_backend() or self._get_current_backend()
        if not configured:
            return None
        active = self._run_cmd(["/usr/bin/systemctl", "is-active", configured], timeout=3)
        return configured if active.get("stdout", "").strip() == "active" else None

    _IWD_UNITS = (
        "/usr/lib/systemd/system/iwd.service",
        "/lib/systemd/system/iwd.service",
        "/etc/systemd/system/iwd.service",
    )

    def _iwd_installed(self) -> bool:
        """Whether an iwd unit exists in any of the places systemd keeps one."""
        return any(os.path.exists(unit) for unit in self._IWD_UNITS)

    def _heal_orphaned_iwd_conf(self) -> bool:
        """Undo our switch to iwd on a system that no longer has iwd.

        Bazzite dropped iwd from its images in 2026 and migrates its own
        settings back, but our conf file outlives that: NetworkManager keeps
        being told to use a backend that is not installed, and the device has
        no WiFi at all, before anyone can reach a panel to switch back.
        """
        try:
            with open(GENERIC_BACKEND_CONF) as f:
                text = f.read(4096)
        except OSError:
            return False
        if not re.search(r"^\s*wifi\.backend\s*=\s*iwd\s*$", text, re.M):
            return False
        if self._iwd_installed():
            return False
        try:
            os.remove(GENERIC_BACKEND_CONF)
        except OSError as e:
            decky.logger.error(f"Could not remove the stale iwd backend setting: {e}")
            return False
        decky.logger.info(
            "iwd is no longer installed but our backend setting still named it; "
            "removed it and restarted NetworkManager on wpa_supplicant"
        )
        # Our earlier switch to iwd disabled wpa_supplicant, which also removes
        # the D-Bus alias NetworkManager starts it through.
        for cmd in (["/usr/bin/systemctl", "enable", "wpa_supplicant"],
                    ["/usr/bin/systemctl", "restart", "NetworkManager"]):
            self._run_cmd(cmd, timeout=15)
        return True

    # Valve's default for wifi.powersave when its file names none (3, enable).
    _VALVE_POWERSAVE_DEFAULT = "3"
    _POWERSAVE_LINE_RE = re.compile(r"^\s*wifi\.powersave\s*=\s*(\S*)\s*$")

    def _valve_powersave(self) -> str | None:
        """wifi.powersave in Valve's NetworkManager file, None if it names none.

        steamos-manager writes this file from NetworkManager's MERGED config
        whenever it switches the backend, and our own file is part of that
        merge. So a backend switch run from here copied our power save value
        into Valve's file, where it outlived our Reset and uninstall. Steam's
        own Wi-Fi power management setting writes the same line.
        """
        try:
            with open(WIFI_BACKEND_CONF) as f:
                lines = f.read(8192).splitlines()
        except OSError:
            return None
        section = ""
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                section = stripped
                continue
            found = self._POWERSAVE_LINE_RE.match(line)
            if section == "[connection]" and found:
                return found.group(1)
        return None

    def _set_valve_powersave(self, value: str | None) -> bool:
        """Put Valve's wifi.powersave back to value, removing it for None.

        Only an existing file is touched, and only that one line: the backend
        line beside it is Valve's to own.
        """
        try:
            with open(WIFI_BACKEND_CONF) as f:
                lines = f.read(8192).splitlines()
        except OSError:
            return False
        out, section, done = [], "", False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                if section == "[connection]" and not done and value is not None:
                    out.append(f"wifi.powersave={value}")
                    done = True
                section = stripped
            elif section == "[connection]" and self._POWERSAVE_LINE_RE.match(line):
                if value is not None and not done:
                    out.append(f"wifi.powersave={value}")
                done = True
                continue
            out.append(line)
        if value is not None and not done:
            if section != "[connection]":
                out.append("[connection]")
            out.append(f"wifi.powersave={value}")
        # A name of its own in the same directory, so two writers (a backend
        # switch and a toggle) cannot rename each other's half-written file.
        tmp = os.path.join(os.path.dirname(WIFI_BACKEND_CONF),
                           f".{os.path.basename(WIFI_BACKEND_CONF)}.{os.urandom(6).hex()}.tmp")
        try:
            with open(tmp, "x") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, WIFI_BACKEND_CONF)
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            decky.logger.error(f"Could not restore Valve's power save setting: {e}")
            return False
        decky.logger.info(f"Valve's wifi.powersave put back to {value or 'unset'}")
        return True

    def _release_valve_powersave(self, settings: dict):
        """Undo our power save value in Valve's file when we stop managing it.

        Put back what was there when the plugin took power save over. With
        no record of that (an install from before it was recorded), a 2 there
        is taken as ours and returned to Valve's default: a backend switch
        from this plugin is how nearly every such file got its 2, and Steam's
        own setting shows the result either way, so it can be changed back.
        """
        if self._valve_powersave() != "2":
            return
        before = settings.get("valve_powersave_before", "?")
        if before == "2":
            return
        if before not in _VALVE_POWERSAVE_VALUES + ("",):
            before = self._VALVE_POWERSAVE_DEFAULT
        self._set_valve_powersave(before or None)

    _BACKEND_CONF_TEXT = {
        "iwd": "[device]\nwifi.backend=iwd\nwifi.iwd.autoconnect=yes\n",
        "wpa_supplicant": "[device]\nwifi.backend=wpa_supplicant\n",
    }

    def _put_back_backend_conf(self, before: str | None):
        """Return our backend conf file to what it held before a switch."""
        try:
            if before is None:
                os.remove(GENERIC_BACKEND_CONF)
            else:
                _write_system_conf(GENERIC_BACKEND_CONF, before)
        except OSError as e:
            decky.logger.error(f"restoring backend conf: {e}")

    def _restore_generic_backend(self, backend: str) -> tuple:
        """Point NM back at a backend and start it, after a failed switch.

        The one we tried is stopped and disabled first: left enabled, both
        start at the next boot and fight over the interface.

        Returns (whether there is a WiFi interface afterwards, whether we
        recreated it). Stopping iwd destroys the interface on every radio but
        rtw88 (it owns the netdev on drivers outside its default-interface
        list), and wpa_supplicant never makes one, so putting wpa_supplicant
        back left no WiFi at all until a reboot.
        """
        tried = "iwd" if backend == "wpa_supplicant" else "wpa_supplicant"
        self._run_cmd(["/usr/bin/systemctl", "disable", "--now", tried], timeout=15)
        try:
            _write_system_conf(GENERIC_BACKEND_CONF, self._BACKEND_CONF_TEXT[backend])
        except OSError as e:
            decky.logger.error(f"restoring backend conf: {e}")
        for cmd in (["/usr/bin/systemctl", "enable", backend],
                    ["/usr/bin/systemctl", "start", backend],
                    ["/usr/bin/systemctl", "restart", "NetworkManager"]):
            self._run_cmd(cmd, timeout=15)
        return self._interface_after_restart(backend)

    # How long _interface_after_restart gives a restart to settle, then (iwd
    # only) how many more seconds it waits for iwd's own interface. The same
    # as _settle_interface, for callers already off the event loop.
    _SETTLE_SECONDS = 2
    _IWD_INTERFACE_WAIT = 8

    def _interface_after_restart(self, backend: str | None) -> tuple:
        """(whether there is a WiFi interface, whether we recreated it) once a
        backend restart settles. Blocking: for worker threads only. Under
        wpa_supplicant a missing interface is recreated; under iwd it is
        waited for, since iwd makes its own (see _settle_interface). Only
        the managed card's netdev counts when that card is known."""
        if self._interface_back():
            return True, False
        time.sleep(self._SETTLE_SECONDS)
        if self._interface_back():
            return True, False
        if backend == "iwd":
            for _ in range(self._IWD_INTERFACE_WAIT):
                time.sleep(1)
                if self._interface_back():
                    return True, False
            return False, False
        if not self._recover_wlan0():
            return False, False
        time.sleep(self._SETTLE_SECONDS)
        present = bool(self._interface_back())
        return present, present

    async def _generic_backend_switch_worker(self, target: str):
        """Backend switch for non-SteamOS systems (Bazzite, CachyOS, etc.).
        Writes NM config directly and manages systemd services.

        A marker in /run says a switch is under way until it ends one way or
        the other. A plugin reload or a kill part way leaves it behind, and
        the next load finishes or undoes the switch (see
        _reconcile_backend_switch) rather than leave NetworkManager pointed
        at a backend that is not running.
        """
        other = "iwd" if target == "wpa_supplicant" else "wpa_supplicant"
        cancelled = False
        try:
            self._backend_switch["phase"] = "switching"
            decky.logger.info(f"generic backend switch: {other} -> {target}")

            self._set_backend_marker(f"{other} {target}")
            os.makedirs(os.path.dirname(GENERIC_BACKEND_CONF), exist_ok=True)
            try:
                with open(GENERIC_BACKEND_CONF) as f:
                    before = f.read(4096)
            except OSError:
                before = None
            _write_system_conf(GENERIC_BACKEND_CONF, self._BACKEND_CONF_TEXT[target])

            # Ask NetworkManager which backend it will now use BEFORE stopping
            # anything. Another conf.d file can outrank ours, and stopping the
            # backend it still names leaves no WiFi at all.
            merged = await asyncio.to_thread(self._merged_nm_backend)
            if merged is not None and merged != target:
                self._put_back_backend_conf(before)
                # Name the file when it is one we know (Bazzite's iwd.conf),
                # so there is something to act on.
                where = await asyncio.to_thread(self._file_naming_backend, merged)
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": merged,
                    "target": target,
                    "message": (
                        f"Another NetworkManager setting on this system "
                        + (f"({where}) " if where else "")
                        + f"keeps {merged} in force, so nothing was changed. "
                        + ("To switch, remove or change that setting first."
                           if where else
                           "To switch, find the file in /etc/NetworkManager/conf.d "
                           "that sets wifi.backend and change it first.")
                    ),
                }
                return

            # The new one is enabled before the old one is touched, and that
            # is checked. Stopping and disabling first left a stretch with
            # neither enabled (and no D-Bus alias for wpa_supplicant), which
            # a failed enable, or a reload landing there, made a boot with
            # no WiFi.
            enabled = await asyncio.to_thread(
                self._run_cmd, ["/usr/bin/systemctl", "enable", target], 10
            )
            if not enabled["success"]:
                self._put_back_backend_conf(before)
                self._backend_switch["phase"] = "failed"
                self._log_detail("_generic_backend_switch_worker", enabled.get("stderr", "")[:200])
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "message": f"{target} could not be enabled, so nothing was changed.",
                    "detail": enabled.get("stderr", "")[:200],
                }
                return
            for cmd in [
                ["/usr/bin/systemctl", "stop", other],
                ["/usr/bin/systemctl", "disable", other],
            ]:
                await asyncio.to_thread(self._run_cmd, cmd, 10)
            started = await asyncio.to_thread(
                self._run_cmd, ["/usr/bin/systemctl", "start", target], 10
            )
            if not started["success"]:
                # Going on would leave NetworkManager pointed at a backend
                # that is not running and the other one stopped: no WiFi.
                # Put the previous one back first.
                present, recovered = await asyncio.to_thread(
                    self._restore_generic_backend, other)
                detail = started.get("stderr", "")[:200]
                self._backend_switch["phase"] = "failed"
                self._log_detail("_generic_backend_switch_worker", detail)
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "recovery_performed": recovered,
                    "needs_reboot": not present,
                    "message": (f"{target} would not start, so {other} was kept. "
                                + self._after_restore_text(present)),
                    "detail": detail,
                }
                return

            restart = await asyncio.to_thread(
                self._run_cmd,
                ["/usr/bin/systemctl", "restart", "NetworkManager"],
                15,
            )
            if not restart["success"]:
                # The old backend is stopped and disabled by now, and
                # NetworkManager may not be running at all. Put the previous
                # one back, as when the new one would not start.
                present, recovered = await asyncio.to_thread(
                    self._restore_generic_backend, other)
                detail = restart.get("stderr", "")[:200]
                self._backend_switch["phase"] = "failed"
                self._log_detail("_generic_backend_switch_worker", detail)
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "recovery_performed": recovered,
                    "needs_reboot": not present,
                    "message": self._friendly_backend_error(detail),
                    "detail": detail,
                }
                return

            # Phase: reconnecting. The interface first: stopping iwd destroys
            # it on every radio but rtw88 (iwd owns the netdev on drivers
            # outside its default-interface list: ath11k, mt7921e, iwlwifi,
            # rtw89), and wpa_supplicant never makes one. Waiting for a
            # reconnection without it timed out and then reported success,
            # on a device with no WiFi until a reboot.
            self._backend_switch["phase"] = "reconnecting"
            iface, recovery_performed = await self._settle_interface(target)
            reconnect_timed_out = True
            for _ in range(15 if iface else 0):
                await asyncio.sleep(1)
                if await asyncio.to_thread(self._get_wifi_interface):
                    uuid = await asyncio.to_thread(self._get_active_connection_uuid)
                    if uuid:
                        reconnect_timed_out = False
                        break

            await self._judge_generic_switch(target, other, bool(iface),
                                             recovery_performed, reconnect_timed_out)
        except asyncio.CancelledError:
            # Ended part way, so the marker stays for the next load.
            cancelled = True
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                "message": "The WiFi backend switch was cancelled.",
            }
            raise
        except Exception as e:
            decky.logger.error(f"_generic_backend_switch_worker error: {e}")
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                # The exception is in the log above. Its text is not for
                # the panel, which shows this message as it is.
                "message": self._BACKEND_SWITCH_UNEXPECTED,
            }
        finally:
            if not cancelled:
                self._set_backend_marker(None)
            self._backend_switch["in_progress"] = False

    async def _judge_generic_switch(self, target: str, other: str, iface: bool,
                                    recovery_performed: bool, reconnect_timed_out: bool):
        """The end of a generic switch: whether it took, and the result.

        Judged from what is running, not from the file just written: reading
        that back always agreed with itself. Another file in conf.d can still
        win (Bazzite's iwd.conf sorts after ours), and the service can fail
        after starting. Allow a few seconds for a service that is still
        coming up. iface: whether the WiFi interface came back.
        """
        final_backend = None
        for _ in range(5):
            final_backend = await asyncio.to_thread(self._effective_generic_backend)
            if final_backend == target:
                break
            await asyncio.sleep(1)

        if final_backend == target and iface:
            self._backend_switch["phase"] = "done"
            self._backend_switch["result"] = {
                "success": True,
                "backend": final_backend,
                "target": target,
                "recovery_performed": recovery_performed,
                "needs_reboot": False,
                "reconnect_timed_out": reconnect_timed_out,
            }
        elif final_backend == target:
            # Switched, but no interface could be had: the radio comes
            # back with the next boot.
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "backend": final_backend,
                "target": target,
                "recovery_performed": recovery_performed,
                "needs_reboot": True,
                "reconnect_timed_out": reconnect_timed_out,
                "message": "The WiFi backend was switched, but WiFi didn't come back. Restart the device.",
            }
        else:
            # Put the previous backend back rather than leave a half
            # switch: the new one is not in force and the old one is
            # stopped and disabled.
            present, recovered = await asyncio.to_thread(
                self._restore_generic_backend, other)
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "backend": final_backend,
                "target": target,
                "recovery_performed": recovery_performed or recovered,
                "needs_reboot": not present,
                "reconnect_timed_out": reconnect_timed_out,
                "message": (f"{target} did not take over, so {other} was put back. "
                            + self._after_restore_text(present)),
            }

        decky.logger.info(
            f"generic backend switch: target={target}, final={final_backend}, "
            f"iface={iface}, recovery={recovery_performed}, "
            f"reconnect_timed_out={reconnect_timed_out}"
        )

    @staticmethod
    def _after_restore_text(present: bool) -> str:
        """What to do once the previous backend was put back."""
        if present:
            return "Restart the device, then try the switch again."
        return "WiFi didn't come back. Restart the device."

    @staticmethod
    def _marker_dir_fd(create: bool = False) -> int:
        """The marker's directory, opened without links and only when it is
        root's alone (made 0700 if it is not), as _report_dir_fd does for the
        same directory. The caller closes it."""
        fd = _open_dir_no_follow(os.path.dirname(BACKEND_SWITCH_MARKER), create=create)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.geteuid():
                raise PermissionError("the backend switch marker's directory belongs to someone else")
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o700)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _boot_id() -> str:
        """This boot's id, or "" when it cannot be read."""
        try:
            with open(BOOT_ID_FILE, "rb") as f:
                text = f.read(64).decode("ascii", "replace").strip()
        except OSError:
            return ""
        return text if re.fullmatch(r"[0-9a-f-]{8,40}", text) else ""

    def _set_backend_marker(self, text: str | None):
        """Write, or with None remove, the marker of a switch under way.
        Written with this boot's id after the two backends."""
        name = os.path.basename(BACKEND_SWITCH_MARKER)
        if text is None:
            try:
                dfd = self._marker_dir_fd()
            except FileNotFoundError:
                return
            except OSError as e:
                decky.logger.error(f"Could not remove the backend switch marker: {e}")
                return
            try:
                for leftover in (name + ".tmp", name):
                    try:
                        os.unlink(leftover, dir_fd=dfd)
                    except FileNotFoundError:
                        pass
            except OSError as e:
                decky.logger.error(f"Could not remove the backend switch marker: {e}")
            finally:
                os.close(dfd)
            return
        boot = self._boot_id()
        data = (f"{text} {boot}" if boot else text) + "\n"
        try:
            dfd = self._marker_dir_fd(create=True)
        except OSError as e:
            decky.logger.error(f"Could not write the backend switch marker: {e}")
            return
        try:
            try:
                os.unlink(name + ".tmp", dir_fd=dfd)
            except FileNotFoundError:
                pass
            fd = os.open(name + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=dfd)
            with os.fdopen(fd, "w") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name + ".tmp", name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError as e:
            decky.logger.error(f"Could not write the backend switch marker: {e}")
        finally:
            os.close(dfd)

    def _reconcile_backend_switch(self):
        """Finish or undo a backend switch this plugin did not see through.

        The worker's marker is still there only if the plugin was reloaded,
        killed, or the device restarted or lost power part way through one.
        From this boot: if the new backend is in force the old one is left
        disabled, so the two do not both start at the next boot; otherwise
        the old one is put back, as a switch that failed would have done.
        From an earlier boot (another boot id): the configuration this boot
        came up with decides. It names the new backend: the switch is
        finished (new one enabled and running, old one disabled and
        stopped). It does not: the old one is put back.
        """
        try:
            dfd = self._marker_dir_fd()
        except OSError:
            return
        try:
            fd = os.open(os.path.basename(BACKEND_SWITCH_MARKER),
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError:
            return
        finally:
            os.close(dfd)
        try:
            words = (os.read(fd, 128).decode("ascii", "replace").split()
                     if _is_regular(os.fstat(fd).st_mode) else [])
        except OSError:
            words = []
        finally:
            os.close(fd)
        self._set_backend_marker(None)
        backends = ("iwd", "wpa_supplicant")
        if len(words) not in (2, 3) or words[0] not in backends or words[1] not in backends:
            return
        other, target = words[:2]
        boot = self._boot_id()
        if len(words) == 3 and boot and words[2] != boot:
            self._settle_switch_from_earlier_boot(other, target)
            return
        if self._effective_generic_backend() == target:
            self._run_cmd(["/usr/bin/systemctl", "disable", other], timeout=10)
            decky.logger.info(
                f"A backend switch to {target} was cut off part way; it had taken, "
                f"so {other} is left disabled"
            )
        else:
            present, _recovered = self._restore_generic_backend(other)
            decky.logger.info(
                f"A backend switch to {target} was cut off part way and had not "
                f"taken; put {other} back"
                + ("" if present else ", but no WiFi interface came back; a reboot brings it back")
            )

    def _settle_switch_from_earlier_boot(self, other: str, target: str):
        """_reconcile_backend_switch for a switch a restart cut off: by the
        backend this boot's NetworkManager configuration names."""
        if self._merged_nm_backend() == target:
            def systemctl(*args) -> str:
                return (self._run_cmd(["/usr/bin/systemctl", *args], timeout=10)
                        .get("stdout", "") or "").strip()
            if (systemctl("is-enabled", target) == "enabled"
                    and systemctl("is-enabled", other) != "enabled"
                    and systemctl("is-active", target) == "active"):
                decky.logger.info(
                    f"A backend switch to {target} was cut off by a restart after it "
                    "had finished; nothing to settle")
                return
            for args in (("enable", target), ("start", target),
                         ("stop", other), ("disable", other),
                         ("restart", "NetworkManager")):
                self._run_cmd(["/usr/bin/systemctl", *args], timeout=15)
            present, _recovered = self._interface_after_restart(target)
            decky.logger.info(
                f"A backend switch to {target} was cut off by a restart; the configuration "
                f"names {target}, so it was finished and {other} disabled"
                + ("" if present else ", but no WiFi interface came back; a reboot brings it back")
            )
            return
        present, _recovered = self._restore_generic_backend(other)
        decky.logger.info(
            f"A backend switch to {target} was cut off by a restart before it took; "
            f"put {other} back"
            + ("" if present else ", but no WiFi interface came back; a reboot brings it back")
        )

    async def start_backend_switch(self, backend: str) -> dict:
        """Kick off a backend switch. Returns immediately; poll get_backend_switch_status for progress."""
        reserved = False
        launched = False
        previous = None
        try:
            self._ensure_backend_switch_state()
            if backend not in ("iwd", "wpa_supplicant"):
                return {
                    "accepted": False,
                    "reason": "invalid_backend",
                    "message": "Choose iwd or wpa_supplicant.",
                }
            if self._backend_switch.get("in_progress"):
                return {
                    "accepted": False,
                    "reason": "in_progress",
                    "message": "A WiFi backend switch is already running.",
                }
            if self._update_running():
                return {
                    "accepted": False,
                    "reason": "update_in_progress",
                    "message": "An update is being installed. Try again once it finishes.",
                }
            # A lock, band, DNS or IPv6 change, or a Reset, is part way
            # through its writes and reconnect. Restarting NetworkManager
            # under it, or clearing the pins it is writing, leaves the
            # profile in neither state.
            if self._profile_change_in_flight():
                return {
                    "accepted": False,
                    "reason": "busy",
                    "message": self._BUSY_RESULT["message"],
                }
            # Reserve before the first await. Checking and then awaiting let
            # two requests both pass the check and both start a worker.
            previous = dict(self._backend_switch)
            self._backend_switch.update({
                "in_progress": True,
                "phase": "switching",
                "target": backend,
                "started_at": int(time.time()),
                "result": None,
            })
            reserved = True
            if not await asyncio.to_thread(self._has_backend_tool):
                return {
                    "accepted": False,
                    "reason": "tool_missing",
                    "message": "This system has no way to switch the WiFi backend.",
                }
            current = await asyncio.to_thread(self._get_current_backend)
            method = await asyncio.to_thread(self._get_backend_method)
            if current == backend and method not in ("steamos_manager", "steamos"):
                # Configured is not the same as working. If the named backend
                # is not actually running, switching to it again is the repair.
                effective = await asyncio.to_thread(self._effective_generic_backend)
                if effective != backend:
                    current = effective
            if current == backend:
                return {
                    "accepted": False,
                    "reason": "already_set",
                    "message": f"WiFi already uses {backend}.",
                    "backend": current,
                }

            if backend == "iwd":
                # A pinned profile cannot be brought up by NetworkManager under
                # iwd, and the switch ends with exactly such a reconnect.
                await asyncio.to_thread(self._clear_lock_and_band_pins)

            # Route to the appropriate worker based on backend method
            if method == "steamos_manager":
                worker = self._steamos_manager_backend_switch_worker(backend)
            elif method == "steamos":
                worker = self._backend_switch_worker(backend)
            else:
                worker = self._generic_backend_switch_worker(backend)
            self._backend_switch_task = asyncio.create_task(worker)
            launched = True
            decky.logger.info(f"backend switch started: {current} -> {backend}")
            return {
                "accepted": True,
                "target": backend,
                "from": current,
            }
        except Exception as e:
            decky.logger.error(f"start_backend_switch error: {e}")
            return {
                "accepted": False,
                "reason": "unexpected",
                "message": "Couldn't start the WiFi backend switch. Try again in a moment.",
            }
        finally:
            # Anything that returned without starting a worker gives the
            # reservation back, or every later switch reads "in progress".
            # Without the previous switch's result: a panel that saw the
            # reservation took it for a switch under way, and then showed
            # that old result as this one's.
            if reserved and not launched and previous is not None:
                self._backend_switch.clear()
                self._backend_switch.update(previous)
                self._backend_switch["result"] = None

    async def get_backend_switch_status(self) -> dict:
        """Return current phase and, when terminal, the final result."""
        try:
            self._ensure_backend_switch_state()
            return {
                "success": True,
                "in_progress": self._backend_switch["in_progress"],
                "phase": self._backend_switch["phase"],
                "target": self._backend_switch["target"],
                "started_at": self._backend_switch["started_at"],
                "result": self._backend_switch["result"],
            }
        except Exception as e:
            decky.logger.error(f"get_backend_switch_status error: {e}")
            # Return a complete shape so the frontend's poll handler hits the
            # terminal branch cleanly and surfaces the error to the user rather
            # than silently stopping with no feedback.
            return {
                "success": False,
                "in_progress": False,
                "phase": "failed",
                "target": None,
                "started_at": 0,
                "result": {
                    "success": False,
                    "target": "",
                    "message": "Couldn't read the backend switch status. Try again in a moment.",
                },
                "message": "Couldn't read the backend switch status. Try again in a moment.",
            }

    # ---- Send report to the developer ----
    #
    # The panel shows the user exactly what would be sent (preview_report),
    # and only what they saw is sent (send_report). A report that cannot go
    # yet is kept, one at a time, and sent from the background watch once
    # WiFi is back. Nothing here runs while REPORT_URL is empty.

    _REPORT_MESSAGES = {
        "disabled": "Sending reports isn't available in this version. Use Copy diagnostics instead.",
        "collect_failed": "Couldn't put the report together. Try again in a moment.",
        "too_large": "The report is too large to send. Use Copy diagnostics and post it on GitHub instead.",
        "stale": "That report is out of date. Select Send report to the developer again.",
        "queued": "No connection. It will be sent when WiFi is back.",
        "queued_unreachable": "Couldn't reach the report service. Your report is saved and will be sent later.",
        "queued_busy": "The report service is busy. Your report is saved and will be sent later.",
        "queued_limited": "The report service is busy. Your report is saved and will be sent later.",
        "queued_unexpected": "Couldn't send the report. It is saved and will be tried again later.",
        "not_kept": "Couldn't send the report or save it. Try again when WiFi is back.",
        "refused": "The report wasn't accepted, so it was not sent. Use Copy diagnostics and post it on GitHub instead.",
        "unexpected": "Couldn't send the report. Try again later, or use Copy diagnostics and post it on GitHub.",
    }
    # How a send that is kept for later is worded, by how it ended.
    _REPORT_QUEUED_MESSAGE = {
        "offline": "queued", "unreachable": "queued_unreachable", "unavailable": "queued_busy",
        "rate_limited": "queued_limited", "unexpected": "queued_unexpected",
    }

    def _report_guard(self) -> asyncio.Lock:
        """Held while a report is sent, so the panel and the background
        retry never send the same waiting report twice."""
        if getattr(self, "_report_lock", None) is None:
            self._report_lock = asyncio.Lock()
        return self._report_lock

    def _report_kind_for_code(self, code: str) -> str:
        """How an HTTP answer other than a 200 with ok ends a send."""
        if code == "413":
            return "too_large"
        if code == "429":
            return "rate_limited"
        if code == "408" or code.startswith("5"):
            return "unavailable"
        if code == "404":
            # Not the report service at that address (a URL this version
            # has wrong, or a route moved): nothing about the report, so it
            # is kept and tried later rather than dropped as refused.
            return "unexpected"
        if code.startswith("4"):
            return "refused"
        return "unexpected"

    def _post_report(self, path: str) -> dict:
        """POST the file at path to REPORT_URL. {"kind": ..., "ref": ...}.

        kind is "sent" (ref when the answer carries one the plugin knows,
        otherwise ""), "refused" or "too_large" (never tried again), or
        "offline", "unreachable", "unavailable", "rate_limited" or
        "unexpected" (kept and tried later). The body goes to curl as a file,
        never on its command line, with an Idempotency-Key that is the
        sha256 of the body (the id the preview gave it), so a report the
        receiving end took but whose answer was lost is not taken twice.
        curl as for the update check: system curl through _run_cmd, https
        only, bounded in time and in what it reads back. -q first so no
        curlrc can add a proxy or headers, and no redirect is followed, so
        the report never goes anywhere but REPORT_URL.
        """
        url = _report_url()
        if not url:
            return {"kind": "refused", "ref": ""}
        try:
            with open(path, "rb") as f:
                key = hashlib.sha256(f.read(REPORT_MAX_BYTES + 4096)).hexdigest()
        except OSError as e:
            decky.logger.error(f"Report: could not read the report to send: {e}")
            return {"kind": "unexpected", "ref": ""}
        result = self._run_cmd(
            [
                "/usr/bin/curl", "-q", "-sS", "--proto", "=https", "--tlsv1.2",
                "--connect-timeout", "5", "--max-time", "20", "--max-filesize", "4096",
                "-H", "Content-Type: application/json", "-H", f"Idempotency-Key: {key}",
                "--data-binary", "@" + path, "-w", "\n%{http_code}", url,
            ],
            25,
        )
        answer, _sep, code = (result.get("stdout") or "").rpartition("\n")
        code = code.strip()
        if not result.get("success"):
            rc = result.get("returncode")
            self._log_throttled(
                "report_send", f"Report: not sent, rc={rc}: "
                f"{_tool_text((result.get('stderr') or '')[:160])}")
            timed_out = rc == -1 and "timed out" in (result.get("stderr") or "")
            if rc in REPORT_NETWORK_RC or timed_out:
                # No answer at all. Offline only when NetworkManager agrees:
                # with WiFi up it is the report service that cannot be
                # reached, and "No connection" would not be true.
                state, _radio = self._nm_general_state()
                connected = (state or "").startswith("connected")
                return {"kind": "unreachable" if connected else "offline", "ref": ""}
            if rc in REPORT_TLS_RC:
                return {"kind": "unexpected", "ref": ""}
            if rc == 63 and code.isdigit() and code != "000":
                # An answer larger than --max-filesize: its status still says
                # what it was (an error page from a proxy, say).
                return {"kind": self._report_kind_for_code(code), "ref": ""}
            return {"kind": "unexpected", "ref": ""}
        try:
            data = json.loads(answer) if answer.strip() else None
        except ValueError:
            data = None
        if code == "200" and isinstance(data, dict) and data.get("ok") is True:
            ref = data.get("ref")
            if not (isinstance(ref, str) and REPORT_REF_RE.match(ref)):
                # Taken all the same: sending it again would only make copies.
                decky.logger.info("Report: accepted with a reference this version does not know")
                ref = ""
            return {"kind": "sent", "ref": ref}
        decky.logger.error(f"Report: not accepted, http={code[:8]}")
        return {"kind": self._report_kind_for_code(code), "ref": ""}

    def _post_report_body(self, body: str) -> dict:
        """_post_report for a body that could not be kept in REPORT_DIR,
        from a file in a private temporary directory of its own."""
        directory = tempfile.mkdtemp(prefix="wifi-optimizer-report-")
        path = os.path.join(directory, "report.json")
        try:
            os.chmod(directory, 0o700)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(body)
            return self._post_report(path)
        except OSError as e:
            decky.logger.error(f"Report: could not stage the report: {e}")
            return {"kind": "unexpected", "ref": ""}
        finally:
            for leftover, remove in ((path, os.remove), (directory, os.rmdir)):
                try:
                    remove(leftover)
                except OSError:
                    pass

    def _write_report(self, name: str, data: str) -> bool:
        """_write_report_file, which never makes REPORT_DIR again once
        uninstall has begun. A send already under way then finishes in its
        worker thread (cancelling its task only ends the wait for it), and
        would otherwise put the folder back after the teardown removed it,
        with nothing left to remove it again."""
        return _write_report_file(name, data, create=not getattr(self, "_tearing_down", False))

    def _drop_pending_report(self, why: str):
        """Remove the waiting report unsent, and note it for the panel. With
        REPORT_URL empty the panel shows no Send report part at all, so
        nothing would ever show the note or remove it: none is written."""
        decky.logger.info(f"Report: dropped a waiting report {why}")
        _remove_report_file(REPORT_PENDING_NAME)
        if _report_url():
            self._write_report(REPORT_DROPPED_NAME, why)

    def _pending_report_body(self) -> str | None:
        """The waiting report's body if it is one to send: a waiting file
        that is not one of ours, or two weeks old, is dropped here. Runs
        whether or not REPORT_URL is set, so emptying it does not keep a
        report on disk for good. No command is run."""
        body, mtime = _read_report_file(REPORT_PENDING_NAME, REPORT_MAX_BYTES + 1024)
        if body is None:
            return None
        if not _pending_body_valid(body):
            self._drop_pending_report("that is not one of ours")
            return None
        if time.time() - mtime > REPORT_PENDING_MAX_AGE:
            self._drop_pending_report("older than two weeks")
            return None
        return body

    def _send_pending_locked(self) -> str:
        """Try the waiting report once. Returns how it ended, "" if none.

        Sent: removed, and its reference kept for the panel. Refused:
        removed, never retried, and noted for the panel. Anything else:
        left for the next try.
        """
        if self._pending_report_body() is None:
            return ""
        outcome = self._post_report(os.path.join(REPORT_DIR, REPORT_PENDING_NAME))
        kind = outcome["kind"]
        if kind == "sent":
            _remove_report_file(REPORT_PENDING_NAME)
            if outcome["ref"]:
                self._write_report(REPORT_SENT_NAME, outcome["ref"])
            # The wake it was about now has a report that went.
            noted, _mtime = _read_report_file(REPORT_WAKE_SENT_NAME, 64)
            words = (noted or "").split()
            if len(words) == 1 and words[0].isdigit():
                self._note_wake_report(int(words[0]), True)
            decky.logger.info(f"Report: the waiting report was sent {outcome['ref']}".rstrip())
        elif kind in ("refused", "too_large"):
            self._drop_pending_report(f"the receiving end refused ({kind})")
        return kind

    async def _send_pending_report_if_due(self):
        """Send a waiting report once WiFi is back. From the background watch.

        Costs a stat at most every two minutes, and one nmcli call when a
        report is waiting and REPORT_URL is set. With REPORT_URL empty a
        waiting report is never sent, but one too old is still dropped.
        """
        try:
            now = time.monotonic()
            if now < getattr(self, "_report_retry_at", 0.0):
                return
            self._report_retry_at = now + REPORT_RETRY_AFTER["offline"]
            if not os.path.exists(os.path.join(REPORT_DIR, REPORT_PENDING_NAME)):
                return
            if not _report_url():
                await asyncio.to_thread(self._pending_report_body)
                return
            state, _radio = await asyncio.to_thread(self._nm_general_state)
            if not (state or "").startswith("connected"):
                return
            async with self._report_guard():
                kind = await asyncio.to_thread(self._send_pending_locked)
            if kind in REPORT_RETRY_AFTER:
                self._report_retry_at = time.monotonic() + REPORT_RETRY_AFTER[kind]
        except Exception as e:
            self._log_throttled("report_retry", f"Report: waiting report not tried: {e}")

    async def preview_report(self) -> dict:
        """Exactly what Send report would send, for the panel to show.

        {"success", "id", "payload"}: payload is the request body as data;
        id names it for send_report, which sends only a report it matches.
        """
        if not _report_url():
            return {"success": False, "enabled": False, "error": "disabled",
                    "message": self._REPORT_MESSAGES["disabled"]}
        try:
            info = await self.get_diagnostic_info()
            version = getattr(decky, "DECKY_PLUGIN_VERSION", "")
            if (not isinstance(info, dict) or info.get("success") is False
                    or not isinstance(version, str) or not VERSION_RE.match(version)):
                return {"success": False, "enabled": True, "error": "collect_failed",
                        "message": self._REPORT_MESSAGES["collect_failed"]}
            if not _report_looks_real(info):
                # The receiving end would refuse it, and a refused report is
                # dropped for good: refused here instead, and nothing sent.
                decky.logger.error("Report: the diagnostics lack a part every report needs")
                return {"success": False, "enabled": True, "error": "collect_failed",
                        "message": self._REPORT_MESSAGES["collect_failed"]}
            woke_badly = bool(await self._wake_failure_stamp())
            made = _report_payload(info, version, woke_badly)
            if made is None:
                return {"success": False, "enabled": True, "error": "too_large",
                        "message": self._REPORT_MESSAGES["too_large"]}
            payload, body = made
            report_id = hashlib.sha256(body.encode("utf-8")).hexdigest()
            self._report_preview = (report_id, body)
            return {"success": True, "enabled": True, "id": report_id, "payload": payload}
        except Exception as e:
            decky.logger.error(f"preview_report error: {e}")
            return {"success": False, "enabled": True, "error": "collect_failed",
                    "message": self._REPORT_MESSAGES["collect_failed"]}

    def _note_wake_report(self, stamp: int, sent: bool):
        """Record in REPORT_WAKE_SENT_NAME that a report about the wake at
        stamp is waiting to go, or (sent) has gone. A record that one has
        gone is never turned back into a waiting one."""
        current, _mtime = _read_report_file(REPORT_WAKE_SENT_NAME, 64)
        if (current or "").split() == [str(int(stamp)), "sent"]:
            return
        self._write_report(REPORT_WAKE_SENT_NAME, f"{int(stamp)} sent" if sent else str(int(stamp)))

    async def _retire_wake_notice(self, sent: bool = False):
        """Note that a report about the current failed wake has been sent or
        is waiting to go, so the notice does not ask again. Its own record,
        not Dismiss's: withdrawing a waiting report brings the notice back
        (cancel_pending_report)."""
        stamp = await self._wake_failure_stamp()
        if stamp:
            await asyncio.to_thread(self._note_wake_report, stamp, sent)

    async def send_report(self, report_id: str = "") -> dict:
        """Send the report preview_report last showed, the user having said so.

        status: "sent" (with ref, "" when the answer carried none this
        version knows), "queued" (kept, sent later) or "failed". The report
        is kept before it is sent, replacing any other waiting one, so a
        send cut short still goes later. Sent or queued, the wake notice
        retires.
        """
        if not _report_url():
            return {"success": False, "status": "failed", "error": "disabled",
                    "message": self._REPORT_MESSAGES["disabled"]}
        preview = getattr(self, "_report_preview", None)
        if not preview or not isinstance(report_id, str) or report_id != preview[0]:
            return {"success": False, "status": "failed", "error": "stale",
                    "message": self._REPORT_MESSAGES["stale"]}
        body = preview[1]
        try:
            async with self._report_guard():
                kept = await asyncio.to_thread(self._write_report, REPORT_PENDING_NAME, body)
                if kept:
                    outcome = await asyncio.to_thread(
                        self._post_report, os.path.join(REPORT_DIR, REPORT_PENDING_NAME))
                else:
                    outcome = await asyncio.to_thread(self._post_report_body, body)
                kind = outcome["kind"]
                if kind in ("sent", "refused", "too_large") and kept:
                    await asyncio.to_thread(_remove_report_file, REPORT_PENDING_NAME)
        except Exception as e:
            decky.logger.error(f"send_report error: {e}")
            return {"success": False, "status": "failed", "error": "unexpected",
                    "message": self._REPORT_MESSAGES["unexpected"]}
        if kind == "sent":
            self._report_preview = None
            await self._retire_wake_notice_safely(sent=True)
            ref = outcome["ref"]
            decky.logger.info(f"Report: sent {ref}".rstrip())
            return {"success": True, "status": "sent", "ref": ref,
                    "message": (f"Report sent. Reference {ref}. Quote it if you post about this."
                                if ref else "Report sent.")}
        if kind in ("refused", "too_large"):
            return {"success": False, "status": "failed", "error": kind,
                    "message": self._REPORT_MESSAGES[kind]}
        if not kept:
            return {"success": False, "status": "failed", "error": "not_kept",
                    "message": self._REPORT_MESSAGES["not_kept"]}
        self._report_retry_at = time.monotonic() + REPORT_RETRY_AFTER.get(kind, 3600)
        self._report_preview = None
        await self._retire_wake_notice_safely()
        message = self._REPORT_QUEUED_MESSAGE.get(kind, "queued_unexpected")
        return {"success": True, "status": "queued", "error": kind,
                "message": self._REPORT_MESSAGES[message]}

    async def _retire_wake_notice_safely(self, sent: bool = False):
        """_retire_wake_notice, never failing the send it follows."""
        try:
            await self._retire_wake_notice(sent)
        except Exception as e:
            decky.logger.error(f"Report: could not retire the wake notice: {e}")

    # The previous boot's journal, as the wake notice and Copy diagnostics
    # read it: kernel and NetworkManager lines, Unix times, no host name.
    _WAKE_READ = ["/usr/bin/journalctl", "-b", "-1", "--no-hostname", "-o", "short-unix",
                  "--no-pager", "-n", WAKE_JOURNAL_MAX]
    _WAKE_SOURCES = ["_TRANSPORT=kernel", "+", "_SYSTEMD_UNIT=NetworkManager.service"]
    # journalctl's words for a boot it has no record of: an answer, not a
    # read that failed.
    _NO_PREVIOUS_BOOT_RE = re.compile(r"(?i)specified boot|no journal boot entry|no such boot")
    # How long after a read that failed the wake notice reads again.
    _WAKE_RETRY_SECONDS = 60

    def _wake_events_read(self) -> dict:
        """The previous boot's wake events (sleeps, wakes, WiFi up, the
        radio), from one --grep read: _run_cmd's result. Kept once a read
        succeeds, since the previous boot cannot change while this one runs;
        the wake notice and Copy diagnostics share it, so both judge the same
        wake. Copy diagnostics runs it itself: it works with REPORT_URL
        empty."""
        kept = getattr(self, "_wake_events_kept", None)
        if kept is not None:
            return kept
        result = self._run_cmd(
            self._WAKE_READ + ["--grep", _WAKE_EVENT_GREP] + self._WAKE_SOURCES, timeout=8)
        if result.get("success"):
            self._wake_events_kept = result
        return result

    def _read_wake_failure(self) -> int | None:
        """_wake_failure_at for the previous boot, from a few bounded reads,
        or None when a read it needed failed or timed out (so the answer is
        not known, and must not be kept as "no failed wake").

        The events first (sleeps, wakes, WiFi up and the radio), which are
        few, then the WiFi stack's lines around the wake they point to, then
        when the boot ended. Reading the boot's last lines instead lost the
        wake on exactly the boots that matter: a driver that kept failing
        after it filled them. A journalctl without --grep gets the old single
        read. No previous boot in the journal is an answer: 0.
        """
        events = self._wake_events_read()
        if not events.get("success"):
            stderr = events.get("stderr") or ""
            if self._NO_PREVIOUS_BOOT_RE.search(stderr):
                return 0
            if events.get("returncode") == -1:
                # Timed out, or could not run: the full read would fare no
                # better, and an 8 s wait more helps nobody.
                return None
            log = self._run_cmd(self._WAKE_READ + self._WAKE_SOURCES, timeout=8)
            if not log.get("success"):
                return 0 if self._NO_PREVIOUS_BOOT_RE.search(log.get("stderr") or "") else None
            return _wake_failure_at(log.get("stdout", ""))
        text = events.get("stdout", "")
        parsed = _wake_events(text)
        found = _wake_candidate(parsed)
        if not found:
            return 0
        start, wake = int(parsed[found[0]][0]), int(parsed[found[1]][0])
        driver = self._run_cmd(
            self._WAKE_READ + ["-k", "--since", f"@{start - 5}",
                               "--until", f"@{wake + WAKE_DRIVER_WINDOW}",
                               "--grep", _WAKE_DRIVER_GREP],
            timeout=8,
        )
        if not driver.get("success"):
            return None
        text += "\n" + driver.get("stdout", "")
        last = self._run_cmd(
            ["/usr/bin/journalctl", "-b", "-1", "--no-hostname", "-o", "short-unix",
             "--no-pager", "-n", "1"],
            timeout=8,
        )
        if not last.get("success"):
            return None
        ended = [float(hit.group(1)) for hit in map(re.compile(r"^([0-9]{9,11}(?:\.[0-9]+)?) ").match,
                                                    (last.get("stdout") or "").splitlines()) if hit]
        return _wake_failure_at(text, max(ended, default=0))

    async def _wake_failure_stamp(self) -> int:
        """The previous boot's failed wake, worked out once per plugin load:
        the previous boot cannot change while this one runs. Only an answer
        is kept. A read that failed or timed out answers 0 for now and is
        tried again after _WAKE_RETRY_SECONDS, so a busy journal at the first
        poll does not hide a failed wake for the whole load."""
        if getattr(self, "_wake_lock", None) is None:
            self._wake_lock = asyncio.Lock()
        async with self._wake_lock:
            if getattr(self, "_wake_stamp", None) is not None:
                return self._wake_stamp
            now = time.monotonic()
            if now < getattr(self, "_wake_retry_at", 0.0):
                return 0
            try:
                stamp = await asyncio.to_thread(self._read_wake_failure)
            except Exception as e:
                decky.logger.error(f"wake check error: {e}")
                stamp = None
            if stamp is None:
                self._wake_retry_at = time.monotonic() + self._WAKE_RETRY_SECONDS
                return 0
            self._wake_stamp = stamp
            if stamp:
                decky.logger.info("Report: the previous boot's last wake left WiFi down")
            return stamp

    async def get_report_state(self) -> dict:
        """What the panel needs for Send report and the wake notice.

        enabled: REPORT_URL is set; with it off everything else is empty and
        no log is read (a waiting report too old is still dropped). pending:
        a report is waiting to be sent. sent_ref: the reference of a waiting
        report that has since gone, until the panel dismisses it. dropped: a
        waiting report was dropped unsent, until the panel dismisses it.
        wake_failure and wake_failure_at: the previous boot's failed wake,
        and when; nothing else from the log reaches the panel. wake_unknown
        (only when true): the journal read failed or timed out and waits for
        its retry, so "no failed wake" is not known yet and the panel keeps
        asking (contract C17).
        """
        state = {"success": True, "enabled": False, "pending": False, "sent_ref": "",
                 "wake_failure": False, "wake_failure_at": 0, "dropped": False}
        try:
            await asyncio.to_thread(self._pending_report_body)
        except Exception as e:
            decky.logger.error(f"get_report_state error: {e}")
        if not _report_url():
            return state
        state["enabled"] = True
        try:
            state["pending"] = os.path.exists(os.path.join(REPORT_DIR, REPORT_PENDING_NAME))
            state["dropped"] = os.path.exists(os.path.join(REPORT_DIR, REPORT_DROPPED_NAME))
            ref, _mtime = await asyncio.to_thread(_read_report_file, REPORT_SENT_NAME, 64)
            ref = (ref or "").strip()
            state["sent_ref"] = ref if REPORT_REF_RE.match(ref) else ""
            stamp = await self._wake_failure_stamp()
            if getattr(self, "_wake_stamp", None) is None:
                state["wake_unknown"] = True
            if stamp:
                seen, _mtime = await asyncio.to_thread(
                    _read_report_file, REPORT_WAKE_SEEN_NAME, 32)
                sent, _mtime = await asyncio.to_thread(
                    _read_report_file, REPORT_WAKE_SENT_NAME, 64)
                # Hidden once dismissed, or once a report about it was sent
                # or is waiting to go.
                if (seen or "").strip() != str(stamp) and (sent or "").split()[:1] != [str(stamp)]:
                    state["wake_failure"] = True
                    state["wake_failure_at"] = int(stamp)
        except Exception as e:
            decky.logger.error(f"get_report_state error: {e}")
        return state

    async def dismiss_report_notice(self, kind: str = "") -> dict:
        """The panel has shown a notice: "wake", "sent" or "dropped". Stop
        reporting it."""
        try:
            if kind == "wake":
                stamp = getattr(self, "_wake_stamp", None) or 0
                if stamp and not await asyncio.to_thread(
                        self._write_report, REPORT_WAKE_SEEN_NAME, str(int(stamp))):
                    return {"success": False, "error": "write_failed"}
                return {"success": True}
            if kind == "sent":
                return {"success": await asyncio.to_thread(_remove_report_file, REPORT_SENT_NAME)}
            if kind == "dropped":
                return {"success": await asyncio.to_thread(_remove_report_file, REPORT_DROPPED_NAME)}
            return {"success": False, "error": "unknown"}
        except Exception as e:
            decky.logger.error(f"dismiss_report_notice error: {e}")
            return {"success": False, "error": "unexpected"}

    async def cancel_pending_report(self) -> dict:
        """Withdraw the waiting report: it is removed and never sent. Waits
        for a send of it already under way to end first. The wake notice it
        retired comes back, unless a report about that wake has already gone
        or the notice was dismissed (wake-notice-seen is Dismiss's alone)."""
        try:
            async with self._report_guard():
                waiting = os.path.exists(os.path.join(REPORT_DIR, REPORT_PENDING_NAME))
                gone = await asyncio.to_thread(_remove_report_file, REPORT_PENDING_NAME)
                if waiting and gone:
                    noted, _mtime = await asyncio.to_thread(
                        _read_report_file, REPORT_WAKE_SENT_NAME, 64)
                    if "sent" not in (noted or "").split():
                        await asyncio.to_thread(_remove_report_file, REPORT_WAKE_SENT_NAME)
            if gone:
                decky.logger.info("Report: the waiting report was withdrawn")
            return {"success": gone}
        except Exception as e:
            decky.logger.error(f"cancel_pending_report error: {e}")
            return {"success": False}
