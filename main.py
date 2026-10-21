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

DISPATCHER_PATH = "/etc/NetworkManager/dispatcher.d/99-wifi-optimizer"
SYSFS_NET = "/sys/class/net"
NM_CONF_PATH = "/etc/NetworkManager/conf.d/99-wifi-optimizer.conf"
MODPROBE_CONF_PATH = "/etc/modprobe.d/99-wifi-optimizer.conf"
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
        "chip_label": "WiFi 6E (QCA206X)",
        "supports_6ghz": True,
        "sysfs_power_fixes": [],
        "modprobe_options": [],
    },
    "rtw89": {
        "chip_label": "WiFi 6 (RTL8852)",
        "supports_6ghz": True,
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
        "chip_label": "WiFi 6E (MT7922)",
        "supports_6ghz": True,
        "sysfs_power_fixes": [
            "/sys/module/mt7921e/parameters/disable_aspm",
        ],
        "modprobe_options": [
            "options mt7921e disable_aspm=Y",
        ],
    },
    "iwlwifi": {
        "chip_label": "Intel WiFi",
        "supports_6ghz": True,
        "sysfs_power_fixes": [],
        # iwlmvm is a separate module from iwlwifi, so the modules it may
        # configure are listed rather than inferred from the driver name.
        "modules": ["iwlwifi", "iwlmvm"],
        "modprobe_options": [
            "options iwlwifi power_save=0 uapsd_disable=3",
            "options iwlmvm power_scheme=1",
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
# What the PCIe power files held before we first changed them this boot.
PCIE_BASELINE_FILE = "/run/wifi-optimizer/pcie-baseline"
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


def _with_pin(settings: dict, prop: str) -> list:
    """settings["pins_owned"] with prop added."""
    owned = list(settings.get("pins_owned", []))
    if prop not in owned:
        owned.append(prop)
    return owned


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
    kept, refused = [], []
    for token in re.split(r"[\s,]+", (text or "").lower()):
        if not token or token in kept:
            continue
        if (STREAM_PATTERN_MIN <= len(token) <= STREAM_PATTERN_MAX
                and not any(token in line for line in _ALWAYS_RUNNING)
                and len(kept) < STREAM_PATTERNS_MAX):
            kept.append(token)
        else:
            refused.append(token)
    return kept, refused


_PCIE_VALUES = {"control": ("on", "auto")}


def _read_pcie_baseline() -> dict:
    """What the PCIe power files held before we changed them this boot."""
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
        and v in _PCIE_VALUES.get(os.path.basename(k), ("0", "1"))
    }


def _first_existing(paths: list[str]) -> str | None:
    """First path in the list that exists, or None."""
    for path in paths:
        if os.path.isfile(path):
            return path
    return None


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


def _set_aside_corrupt_settings(read_stat: os.stat_result, reason: Exception):
    """Move a settings file that does not parse to settings.json.corrupt.

    The loader falls back to defaults and _main saves them straight away,
    which used to overwrite the only copy of the tracked profile lists and
    the recorded baselines. Moved rather than copied, so the next load finds
    no file and takes the ordinary missing-file path instead of setting it
    aside again, and a previous .corrupt file is replaced: one is enough to
    recover from, and they cannot pile up.
    """
    try:
        dfd = _open_dir_no_follow(os.path.dirname(SETTINGS_FILE))
        name = os.path.basename(SETTINGS_FILE)
        try:
            # Only the file that was read. A save that landed since then put
            # a good file at this name, and moving that aside would lose it.
            now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if (now.st_dev, now.st_ino) != (read_stat.st_dev, read_stat.st_ino):
                return
            os.replace(name, name + ".corrupt", src_dir_fd=dfd, dst_dir_fd=dfd)
        finally:
            os.close(dfd)
        decky.logger.error(
            f"Settings file could not be read ({type(reason).__name__}); "
            "kept it as settings.json.corrupt and started from defaults"
        )
    except OSError as e:
        decky.logger.error(f"Settings file could not be read or set aside: {e}")


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
        with os.fdopen(fd, "rb") as f:
            read_stat = os.fstat(f.fileno())
            if not _is_regular(read_stat.st_mode):
                raise ValueError("settings file is not a regular file")
            raw = f.read(65536)
            if f.read(1):
                raise ValueError("settings file too large")
    except Exception:
        # Missing, not a regular file, too large or unreadable. Nothing was
        # parsed, so there is nothing to keep: defaults stand in as always,
        # and whatever sits at the path is left where it is.
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("settings file does not hold a JSON object")
    except (ValueError, RecursionError) as e:
        _set_aside_corrupt_settings(read_stat, e)
        return copy.deepcopy(DEFAULT_SETTINGS)
    try:
        return _clean_settings(data)
    except Exception:
        return copy.deepcopy(DEFAULT_SETTINGS)


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
                "dns_uuids"):
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


def _save_settings(data: dict):
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

STATE=/run/wifi-optimizer/update-state
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
if command -v flock >/dev/null 2>&1; then
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
# version whole. package.json goes last because it carries the version: if
# anything before it fails, the installed version is unchanged and the same
# update is offered again.
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
if systemctl restart plugin_loader; then
    FINISHED=1
    set_state done
    logger -t wifi-optimizer "Updated to $LABEL, restarted plugin_loader"
else
    logger -t wifi-optimizer "Updated to $LABEL, but Decky did not restart. Restart the Deck to load it."
    exit 1
fi
"""


class Plugin:
    """Root plugin instance. Decky exposes every async method here as a
    callable from the frontend. Synchronous helpers prefixed with `_` are
    for internal use only."""

    # ---- Helpers ----

    def _run_cmd(self, cmd: list[str], timeout: int = 5) -> dict:
        """Run a subprocess and return a result dict.

        Every child gets _child_env(), so it loads the system's libraries
        rather than the ones bundled inside Decky. See _child_env for why
        this cannot be left to the commands that happen to need it.
        """
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
                env=_child_env(),
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "stdout": "",
                "stderr": "Command timed out",
                "returncode": -1,
            }
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
        if os.path.isfile("/usr/lib/systemd/system/iwd.service"):
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
        return os.path.isfile("/usr/lib/systemd/system/iwd.service")

    def _get_current_backend(self) -> str | None:
        """Return 'iwd', 'wpa_supplicant', or None if unknown.

        Checks config files in priority order, then falls back to checking
        which systemd service is active.
        """
        found = self._configured_backend_from_files()
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

    def _configured_backend_from_files(self) -> str | None:
        """The backend the known conf files name, highest priority first.

        NetworkManager reads conf.d in name order and the later file wins, so
        Bazzite's iwd.conf outranks our 99-wifi-optimizer-backend.conf, which
        outranks Valve's 99-valve-wifi-backend.conf; /usr/lib defaults last.
        """
        for path in (
            BAZZITE_IWD_CONF,
            GENERIC_BACKEND_CONF,
            WIFI_BACKEND_CONF,
            NM_DEFAULT_CONF,
            HOLO_NM_DEFAULT_CONF,
        ):
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

    def _release_generic_backend_conf(self):
        """Remove our backend conf file (Reset, uninstall) without stranding WiFi.

        A switch from here disabled the backend it switched away from, and
        that also removes the D-Bus alias NetworkManager starts wpa_supplicant
        through. Deleting only the file would send NetworkManager back to its
        default at the next start with that backend still disabled: no WiFi.
        So the backend it will fall back to is enabled and the one our file
        named is disabled. Nothing is stopped or restarted now, so the
        connection in use carries on and the change lands at the next boot.
        """
        try:
            with open(GENERIC_BACKEND_CONF) as f:
                text = f.read(4096)
        except FileNotFoundError:
            return
        except OSError as e:
            decky.logger.error(f"Could not read our backend setting: {e}")
            return
        found = re.search(r"^\s*wifi\.backend\s*=\s*(iwd|wpa_supplicant)\s*$", text, re.M)
        try:
            os.remove(GENERIC_BACKEND_CONF)
        except FileNotFoundError:
            pass
        except OSError as e:
            decky.logger.error(f"Could not remove our backend setting: {e}")
            return
        if not found:
            return
        named = found.group(1)
        after = (self._merged_nm_backend()
                 or self._configured_backend_from_files()
                 or "wpa_supplicant")
        if after == named:
            return
        for cmd in (["/usr/bin/systemctl", "enable", after],
                    ["/usr/bin/systemctl", "disable", named]):
            self._run_cmd(cmd, timeout=15)
        decky.logger.info(
            f"Removed our backend setting ({named}); NetworkManager uses {after} "
            f"from the next boot, so {after} is enabled and {named} disabled"
        )

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
            return "A system-library conflict occurred. Please reboot and try again."
        if "permission denied" in d:
            return "The system denied permission. Try rebooting."
        if "command not found" in d or "no such file" in d:
            return "A required system tool is missing. Your OS version may not be supported."
        if "timed out" in d or "timeout" in d:
            return "The system didn't respond in time. Try again in a moment."
        if "network is unreachable" in d or "connection refused" in d:
            return "Network problem during the switch. Check WiFi and try again."
        return "The WiFi backend switch didn't take effect."

    def _require_wifi(self) -> tuple:
        iface = self._get_wifi_interface()
        if not iface:
            return None, None, {
                "success": False,
                "error": "no_wifi",
                "message": "Not connected to WiFi",
            }
        uuid = self._get_active_connection_uuid()
        if not uuid:
            return iface, None, {
                "success": False,
                "error": "no_wifi",
                "message": "No active WiFi connection",
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

    def _clear_profile_pins(self):
        """Remove the pins this plugin added from every profile it touched.

        The lock follows whichever profile NetworkManager uses, so discarding
        the settings without clearing them would leave profiles pinned to an
        access point with nothing left that knows about them - and a pinned
        profile fails to associate once that access point is out of range.

        The band preference is cleared alongside it, for the same reason: a
        profile left demanding a band its network does not offer will not
        connect, and after teardown there is nothing left to undo it.
        """
        settings = _load_settings()
        uuids = list(settings.get("bssid_lock_uuids", []))
        current = settings.get("bssid_lock_connection_uuid", "")
        if current and current not in uuids:
            uuids.append(current)
        # Unconditional. Gating on the toggle skipped exactly the profiles
        # left behind by turning it OFF, which is when they are stranded.
        for uuid in uuids:
            result = self._nmcli_modify(uuid, "802-11-wireless.bssid", "")
            decky.logger.info(
                f"Clearing BSSID lock from {uuid}: "
                f"{'ok' if result['success'] else 'not found'}"
            )
        last = settings.get("last_connection_uuid", "")
        band_uuids = settings.get("band_preference_uuids", [])
        for uuid in {u for u in list(band_uuids) + uuids + [last] if u}:
            self._nmcli_modify(uuid, "802-11-wireless.band", "", timeout=2)
        # Same reasoning as the pins: after teardown nothing is left that
        # knows this plugin turned IPv6 off, so it has to be undone here or
        # not at all. Bounded to profiles we recorded writing to.
        for uuid in {u for u in settings.get("ipv6_uuids", []) if u}:
            self._nmcli_modify(uuid, "ipv6.method", "auto", timeout=2)
        # And DNS, which no reset or uninstall used to touch at all: the
        # profile kept public servers and ignored the router's afterwards.
        for uuid in {u for u in settings.get("dns_uuids", []) if u}:
            self._clear_dns(uuid)

    def _clear_dns(self, uuid: str) -> bool:
        """Give a profile back its automatic DNS. True when done, or when the
        profile no longer exists and there is nothing left to undo."""
        results = [
            self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "no", timeout=2),
            self._nmcli_modify(uuid, "ipv4.dns", "", timeout=2),
        ]
        gone = any("unknown connection" in (r.get("stderr") or "").lower()
                   for r in results)
        return gone or all(r["success"] for r in results)

    # What a profile setter answers while another is still mid-change. Two at
    # once interleave their radio cycles and pin writes on one profile.
    _BUSY_RESULT = {
        "success": False,
        "error": "busy",
        "message": "Another WiFi change is still in progress. Try again in a moment.",
    }

    _RADIO_OFF_RESULT = {
        "success": False,
        "error": "iw_failed",
        "message": "The WiFi radio didn't come back on. Try toggling WiFi in Steam settings.",
    }

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
                f"{back.get('stderr', '')[:120]}"
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

    def _apply_driver_fixes(self, enable: bool, persist: bool = True):
        """Apply or revert driver-specific power save fixes from DRIVER_PROFILES.
        Silently no-ops for drivers with no sysfs paths or modprobe options.

        persist=False leaves out the modprobe options, which take effect at
        the next module load and so cannot follow a streaming session."""
        settings = _load_settings()
        profile = DRIVER_PROFILES.get(settings.get("driver"), {})

        val = "Y" if enable else "N"
        for path in self._safe_sysfs_fixes(profile):
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
                os.makedirs(os.path.dirname(MODPROBE_CONF_PATH), exist_ok=True)
                with open(MODPROBE_CONF_PATH, "w") as f:
                    f.write("# WiFi Optimizer - driver power save fixes\n")
                    for opt in options:
                        f.write(opt + "\n")
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
            # Discover WiFi PCI device path dynamically
            iface = self._get_wifi_interface()
            if not iface or not IFACE_RE.match(iface):
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
            # before our first change this boot; the kernel's usual value
            # otherwise. Streaming auto mode restores after every stream, so
            # turning on a state the firmware had left off would add up.
            baseline = _read_pcie_baseline()
            recorded = enable and self._record_pcie_baseline(targets, baseline, record_all)
            for path, val in targets:
                if not os.path.exists(path):
                    continue
                if not enable:
                    val = baseline.get(path, val)
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

            if recorded:
                try:
                    os.makedirs(os.path.dirname(PCIE_BASELINE_FILE), mode=0o755, exist_ok=True)
                except OSError:
                    pass
                _write_no_follow(PCIE_BASELINE_FILE, json.dumps(baseline))
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

    def _install_dispatcher(self) -> bool:
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
            tmp = os.path.join(
                os.path.dirname(DISPATCHER_PATH),
                "." + os.path.basename(DISPATCHER_PATH) + ".new",
            )
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
            return False

    def _remove_dispatcher(self) -> bool:
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
            info = await self.get_device_info()
            settings = _load_settings()
            settings["model"] = info.get("model", "unknown")
            settings["driver"] = info.get("driver", "unknown")
            settings["device_family"] = info.get("device_family", "unknown")
            settings["device_label"] = info.get("device_label", "Unknown Device")
            settings["chip_label"] = info.get("chip_label", "unknown")
            settings["supports_6ghz"] = info.get("supports_6ghz", False)
            distro = self._detect_distro()
            settings["distro_id"] = distro["id"]
            settings["distro_name"] = distro["name"]
            # Read here so the status collector can use it without forking.
            await asyncio.to_thread(self._nm_version)
            # `iw reg set` does not survive a reboot, so a region the user
            # chose has to be put back here. This runs when the plugin loads,
            # which can be after the first association - so the very first
            # connection of a boot may still use the old domain.
            chosen_region = settings.get("regdomain", "")
            if chosen_region and REGDOMAIN_RE.match(chosen_region):
                self._run_cmd(
                    ["/usr/bin/iw", "reg", "set", chosen_region], timeout=5
                )
            self._migrate_settings(settings)
            self._heal_orphaned_iwd_conf()
            await self._unpin_if_iwd()
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

            _save_settings(settings)
            await self._startup_streaming(settings)

            # Apply volatile settings that may have been lost on reboot.
            # The dispatcher handles reconnects, but on a fresh boot WiFi
            # connects before the plugin starts, so we apply here too.
            # Order: buffer tuning first (sets txqueuelen), then CAKE
            # (overrides txqueuelen to 256), then power_save last (sticks
            # after any reconnects the dispatcher might trigger).
            iface = self._get_wifi_interface()
            if iface and self._volatile_gate_open(_load_settings()):
                if settings.get("buffer_tuning_enabled"):
                    try:
                        await self.set_buffer_tuning(True)
                    except Exception as e:
                        decky.logger.error(f"Startup buffer tuning failed: {e}")
                if settings.get("cake_enabled"):
                    try:
                        await self.set_cake(True)
                    except Exception as e:
                        decky.logger.error(f"Startup CAKE apply failed: {e}")
                if settings.get("power_save_disabled"):
                    try:
                        await self.set_power_save(True)
                    except Exception as e:
                        decky.logger.error(f"Startup power save failed: {e}")
                if any(settings.get(k) for k in (
                    "buffer_tuning_enabled", "cake_enabled", "power_save_disabled"
                )):
                    self._record_reapplied(settings.get("last_applied", 0))

            # Sanity check: does the conf-declared backend match what's actually
            # running? Divergence would indicate a previous switch got interrupted
            # (plugin_loader crash, external tool, etc.). Log only; user can
            # re-toggle to resolve.
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
                adopted = self._get_profile_ssid(
                    settings.get("last_connection_uuid", "")
                ) or self._get_profile_ssid(
                    self._get_active_connection_uuid() or ""
                )
                if adopted:
                    settings["band_preference_ssid"] = adopted
                    _save_settings(settings)
                    decky.logger.info(
                        f"Band preference scoped to {adopted!r} on upgrade"
                    )
                else:
                    # Leaving it enabled with no network attached is the state
                    # that lets it be applied to the wrong one. Turn it off so
                    # the toggle tells the truth; one tap re-enables it against
                    # whatever network the user is actually on.
                    settings["band_preference_enabled"] = False
                    _save_settings(settings)
                    decky.logger.info(
                        "Band preference disabled: could not establish which "
                        "network it belongs to"
                    )

            # Warm the steamos-manager probe here, off the polling path, so
            # the collector never has to fork or memoise from its thread.
            backend_method = await asyncio.to_thread(self._get_backend_method)
            if backend_method != "none":
                conf_backend = self._get_current_backend()
                if conf_backend:
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

            decky.logger.info(
                f"WiFi Optimizer ready: device={info.get('device_label')}, "
                f"family={info.get('device_family')}, driver={info.get('driver')}, "
                f"chip={info.get('chip_label')}, distro={distro['id']}"
            )
        except Exception as e:
            decky.logger.error(f"WiFi Optimizer _main error: {e}")
        self._stranded_task = asyncio.create_task(self._stranded_watch())

    _SETTINGS_VERSION = 2

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
        if settings.get("settings_version", 0) >= self._SETTINGS_VERSION:
            return
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

        settings["settings_version"] = self._SETTINGS_VERSION
        decky.logger.info(f"Settings migrated to version {self._SETTINGS_VERSION}")

    async def _unload(self):
        try:
            decky.logger.info("WiFi Optimizer unloading")
            for name in ("_backend_switch_task", "_stranded_task", "_stream_task"):
                task = getattr(self, name, None)
                if task and not task.done():
                    task.cancel()
        except Exception as e:
            decky.logger.error(f"_unload error: {e}")

    async def _uninstall(self):
        try:
            decky.logger.info("WiFi Optimizer uninstalling")
            self._clear_profile_pins()
            self._remove_dispatcher()
            self._apply_driver_fixes(False)
            self._apply_pcie_aspm_fix(False)
            self._revert_queue_tuning(_load_settings())
            self._release_valve_powersave(_load_settings())
            self._release_generic_backend_conf()
            # The diagnostics file and a settings file set aside as unreadable
            # are ours too, and nothing else would ever remove them.
            users = (SETTINGS_FILE, SETTINGS_FILE + ".corrupt", _diagnostics_path(),
                     LEGACY_ENFORCED_FILE)
            for path in [NM_CONF_PATH, MODPROBE_CONF_PATH,
                         *users, ENFORCED_FILE, STREAM_FLAG_FILE, STREAM_HOLD_FILE]:
                # One that cannot go (the settings directory is the user's,
                # so anything can sit at these names) does not stop the rest.
                try:
                    (_remove_no_follow if path in users else os.remove)(path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    decky.logger.error(f"_uninstall could not remove {path}: {e}")
        except Exception as e:
            decky.logger.error(f"_uninstall error: {e}")

    async def _migration(self):
        pass

    # ---- Hardware detection ----

    def _detect_device_family(self) -> tuple[str, str, str]:
        """Read DMI product_name and return (raw_product, family_id, display_label)."""
        try:
            with open("/sys/devices/virtual/dmi/id/product_name", "r") as f:
                product = f.read().strip()
        except Exception:
            return ("unknown", "unknown", "Unknown Device")

        if product in DMI_DEVICES:
            info = DMI_DEVICES[product]
            return (product, info["family"], info["label"])

        for prefix, info in DMI_SUBSTRING_DEVICES:
            if product.startswith(prefix):
                return (product, info["family"], info["label"])

        return (product, "unknown", "Unknown Device")

    def _detect_wifi_driver(self) -> str:
        """Detect the kernel driver of the active WiFi interface via sysfs.
        Normalizes sub-module names (e.g. rtw88_pci) to the canonical
        DRIVER_PROFILES key (rtw88)."""
        iface = self._get_wifi_interface()
        if not iface or not IFACE_RE.match(iface):
            return "unknown"
        try:
            driver_path = os.path.realpath(os.path.join(SYSFS_NET, iface, "device", "driver", "module"))
            module = os.path.basename(driver_path)
            if module in DRIVER_PROFILES:
                return module
            for key in DRIVER_PROFILES:
                if module.startswith(key):
                    return key
            return module
        except Exception:
            return "unknown"

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
            product, device_family, device_label = self._detect_device_family()
            driver = self._detect_wifi_driver()
            profile = DRIVER_PROFILES.get(driver, {})

            chip_label = profile.get("chip_label", "unknown")
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

        The exception text is NOT returned. The panel appends `detail` to
        `message` when it is present, so putting it there would print the
        traceback under the control the user just touched - and for every
        setter rather than the few nmcli failures where stderr is useful.
        Callers log the exception, so nothing is lost.
        """
        return {
            "success": False,
            "error": "unexpected",
            "message": "Something went wrong. Check the Decky log for details.",
        }

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
                "message": "No connection UUID found. Connect to WiFi first.",
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
            return {"success": False, "error": str(e), "readable": False,
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
                    "message": "Couldn't read the current wireless region.",
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
                return {
                    "success": False, "error": "iw_failed",
                    "message": "Couldn't set the wireless region.",
                    "detail": (applied.get("stderr") or "")[:200],
                }
            # Re-read: anything saved while iw ran would otherwise be undone
            # by writing back the copy taken before it.
            settings = _load_settings()
            settings["regdomain_previous"] = previous
            settings["regdomain"] = country
            _save_settings(settings)
            after = await asyncio.to_thread(self._regdomain_info)
            result = {
                "success": True,
                "regdomain": country,
                "governing": after.get("governing", ""),
                "self_managed": after.get("self_managed", False),
                "changeable": after.get("changeable", False),
            }
            if after.get("self_managed"):
                result["message"] = (
                    "This device's WiFi carries its own region "
                    f"({after.get('governing', 'unknown')}) and ignores this "
                    "setting, so the channels available to you have not "
                    "changed. That is normal on the Steam Deck and does not "
                    "mean a band is missing."
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
    _DIAG_IPV4_RE = re.compile(
        r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])"
    )
    # Either the full eight groups or a form shortened with "::". Requiring
    # one of the two keeps times and channel lists, which also have colons,
    # out of it.
    _DIAG_IPV6_RE = re.compile(
        r"(?<![0-9A-Fa-f:])(?:"
        r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?::"
        r"(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?"
        r")(?![0-9A-Fa-f:])"
    )

    @classmethod
    def _redact_diagnostics(cls, value, ssids: tuple = ()):
        """The diagnostics with network names and addresses replaced.

        Placeholders rather than removal, so the shape of the output still
        says what was there: an interface with an SSID is associated, and
        one without is not. Applied to every string, keys included, because
        anything added to the diagnostics later should be covered without
        someone having to remember this.
        """
        if isinstance(value, dict):
            return {cls._redact_diagnostics(k, ssids): cls._redact_diagnostics(v, ssids)
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
        # IPv4 first: an IPv4-mapped address (::ffff:192.168.1.5) would
        # otherwise lose only its first octet to the IPv6 pattern.
        text = cls._DIAG_IPV4_RE.sub("<ip>", text)
        return cls._DIAG_IPV6_RE.sub(
            lambda m: m.group(0) if m.group(0) == "::" else "<ip>", text
        )

    async def get_diagnostic_info(self) -> dict:
        """Collect system info for remote debugging. Sanitized: no passwords,
        and no network names or addresses (see _redact_diagnostics)."""
        try:
            info = await self.get_device_info()
            iface = self._get_wifi_interface() or "none"
            iw_dev = self._run_cmd(["/usr/bin/iw", "dev"], timeout=3)
            iw_reg = self._run_cmd(["/usr/bin/iw", "reg", "get"], timeout=3)
            uname = self._run_cmd(["/usr/bin/uname", "-r"], timeout=3)
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
            # The report predates these parts, so one that fails is noted
            # rather than costing the person the whole report.
            try:
                extras = await asyncio.to_thread(
                    self._diagnostic_extras, info.get("driver", ""), iface
                )
            except Exception as e:
                decky.logger.error(f"diagnostic extras error: {e}")
                extras = {"extras_error": str(e)}
            return self._redact_diagnostics({
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
    # wlp1s0 or a driver missing from this list is covered too.
    _DIAG_KERNEL_TERMS = (
        r"rtw88|rtw89|rtw_|ath1[012]k|mt7\d|iwlwifi|brcmfmac|wlan\d|"
        r"ieee80211 phy\d|cfg80211|PM: |ACPI: PM|pcieport|AER"
    )
    # Connection chatter: on a healthy boot it is most of what matches, and it
    # pushed the suspend lines out of the report on Jason's own Deck.
    _DIAG_CHATTER_RE = re.compile(
        r": (?:authenticate with|send auth to|authenticated|associate with|"
        r"RX AssocResp|associated|deauthenticating from|disassociated from|"
        r"Limiting TX power|aborting authentication|authentication with)"
    )
    _DIAG_KERNEL_LINES = 50
    # How much of a boot's kernel log is read. Bounds memory and time when a
    # driver has been logging errors for days.
    _DIAG_JOURNAL_MAX = "5000"
    # With --no-hostname a kernel record reads "[  12.345678] kernel: msg".
    # Anything else (systemd's "Hostname set to ..." also lands in -k) is left out.
    _DIAG_KERNEL_LINE_RE = re.compile(r"^(\[\s*[\d.]+\]) kernel: (.*)$")

    @classmethod
    def _diag_kernel_lines(cls, text: str, terms: str) -> list:
        """The lines of one boot's kernel log worth reading for a WiFi problem.

        Repeats are folded into a count, since a wedged driver logs the same
        error thousands of times, and connection chatter is dropped. If what
        is left is still too long, it keeps the stretch from the last suspend
        onwards (where a failed wake shows) plus the very end of the boot.
        """
        wanted = re.compile(terms)
        lines, last_key, repeats = [], None, 0
        for raw in text.splitlines():
            found = cls._DIAG_KERNEL_LINE_RE.match(raw)
            if not found:
                continue
            stamp, message = found.groups()
            if not wanted.search(message) or cls._DIAG_CHATTER_RE.search(message):
                continue
            key = re.sub(r"\d+", "#", message)
            if key == last_key:
                repeats += 1
                continue
            if repeats:
                lines.append(f"(repeated {repeats} more times)")
            lines.append(f"{stamp} {message}")
            last_key, repeats = key, 0
        if repeats:
            lines.append(f"(repeated {repeats} more times)")
        cap = cls._DIAG_KERNEL_LINES
        if len(lines) <= cap:
            return lines
        starts = [i for i, line in enumerate(lines) if "PM: suspend entry" in line]
        if not starts:
            return [f"({len(lines) - cap} earlier lines left out)"] + lines[-cap:]
        head = lines[starts[-1]:starts[-1] + cap - 10]
        tail_from = max(starts[-1] + len(head), len(lines) - 10)
        out = []
        if starts[-1]:
            out.append(f"({starts[-1]} earlier lines left out)")
        out += head
        if tail_from > starts[-1] + len(head):
            out.append(f"({tail_from - starts[-1] - len(head)} lines left out)")
        return out + lines[tail_from:]

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
            extras["wifi_backend"] = self._get_current_backend() or "unknown"
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
        extras["modprobe_conf"] = read(MODPROBE_CONF_PATH)
        # Options in that file only take effect when the driver loads, so
        # whether it predates this boot says if they are in force from the
        # start or were only written into sysfs later.
        try:
            with open("/proc/stat", "r") as f:
                boot = next(int(line.split()[1]) for line in f
                            if line.startswith("btime "))
            extras["modprobe_conf_before_boot"] = (
                os.stat(MODPROBE_CONF_PATH).st_mtime < boot
            )
        except (OSError, StopIteration, ValueError, IndexError):
            extras["modprobe_conf_before_boot"] = None
        extras["dispatcher_installed"] = os.path.isfile(DISPATCHER_PATH)

        pcie = {}
        if iface and iface != "none" and IFACE_RE.match(iface):
            device = os.path.realpath(os.path.join(SYSFS_NET, iface, "device"))
            for name in ("l0s_aspm", "l1_aspm", "l1_1_aspm", "l1_2_aspm",
                         "l1_1_pcipm", "l1_2_pcipm"):
                path = os.path.join(device, "link", name)
                if os.path.exists(path):
                    pcie[name] = read(path)
            pcie["power_control"] = read(os.path.join(device, "power", "control"))
            pcie["runtime_status"] = read(os.path.join(device, "power", "runtime_status"))
        extras["pcie"] = pcie

        rfkill = []
        try:
            for entry in sorted(os.listdir("/sys/class/rfkill")):
                base = os.path.join("/sys/class/rfkill", entry)
                rfkill.append(f"{read(os.path.join(base, 'type'))} "
                              f"soft={read(os.path.join(base, 'soft'))} "
                              f"hard={read(os.path.join(base, 'hard'))}")
        except OSError:
            pass
        extras["rfkill"] = rfkill

        # DEVICE,TYPE,STATE only: the CONNECTION column is the network's name.
        # "-e no" because terse mode otherwise escapes the colons in a device
        # named by its address (a paired phone), which the redaction would miss.
        devices = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-e", "no", "-f", "DEVICE,TYPE,STATE",
             "device", "status"],
            timeout=3,
        )
        extras["nm_devices"] = devices.get("stdout", "") or devices.get("stderr", "")

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

        terms = self._DIAG_KERNEL_TERMS
        for name in (iface, driver):
            if name and name not in ("none", "unknown"):
                terms += "|" + re.escape(name)
        for key, boot in (("kernel_log_this_boot", "0"), ("kernel_log_previous_boot", "-1")):
            # --no-hostname: the hostname can be a person's name.
            log = self._run_cmd(
                ["/usr/bin/journalctl", "-k", "-b", boot, "--no-hostname",
                 "-o", "short-monotonic", "-n", self._DIAG_JOURNAL_MAX, "--no-pager"],
                timeout=5,
            )
            if not log.get("success"):
                extras[key] = [f"unavailable: {(log.get('stderr') or '')[:120]}"]
                continue
            extras[key] = self._diag_kernel_lines(log.get("stdout", ""), terms)
        return extras

    async def save_diagnostic_info(self) -> dict:
        """Write diagnostics to a file in the settings directory as a
        fallback when clipboard is unavailable."""
        try:
            info = await self.get_diagnostic_info()
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
            return {"success": False, "error": str(e)}

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
            # subprocess.run waits again after killing a timed-out child, so
            # a process stuck against a wedged driver can do exactly that.
            started = getattr(self, "_collect_started_at", 0.0)
            if started and (time.monotonic() - started) > 90:
                self._log_throttled(
                    "collect_wedged",
                    "Status collection has not returned in 90s; reporting a "
                    "read failure rather than leaving the panel empty",
                )
                return {
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
                }

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
                return cached
            # No previous result to hand back. Falling through here is what
            # the guard exists to prevent: the first collection of a cold boot
            # routinely outlives the poll interval, so every caller would
            # start its own. Report not-ready instead; the panel renders a
            # disconnected state correctly.
            return {
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
            }

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
                # keep showing stale values with no signal.
                decky.logger.error(f"status reconciliation error: {e}")

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
                if settings.get("ipv6_disabled") and not settings.get("ipv6_uuids"):
                    pending["ipv6_uuids"] = [action["uuid"]]
            elif kind == "band":
                self._apply_band(action, settings, status, pending)
            elif kind == "cake":
                self._apply_cake(action, settings, status, pending)
            elif kind == "pin_cleanup":
                self._apply_pin_cleanup(action, settings, status, pending)
            elif kind == "bssid_repoint":
                self._apply_bssid_repoint(action, settings, status, pending)

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
        else:
            # Don't record it as done when it wasn't, or the profile
            # never gets the priority that stops NM preferring a
            # duplicate on boot.
            self._log_throttled(
                "priority",
                f"Couldn't set autoconnect priority on {uuid}: "
                f"{bumped.get('stderr', '')[:120]}",
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
                pending["band_preference_uuids"] = known[
                    -self._MAX_TRACKED_LOCK_UUIDS:
                ]
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
        if self._profile_change_in_flight():
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
            pending["bssid_lock_uuids"] = known[
                -self._MAX_TRACKED_LOCK_UUIDS:
            ]
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
                f"{retarget.get('stderr', '')[:120]}",
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
                await self._stranded_watch_once()
            except Exception as e:
                decky.logger.error(f"stranded watch error: {e}")

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
                f"{(scan.get('stderr', '') or 'timed out')[:160]}"
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
            link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
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
            if bssid or time.monotonic() >= deadline:
                break
            iface = self._get_wifi_interface() or iface
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
        # What actually governs this radio: the self-managed phy when there is
        # one, otherwise the global domain.
        out["governing"] = managed[0]["country"] if managed else out["global"]
        return out

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
        _save_settings_with_timestamp(settings)

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
            f"Refusing band {band!r}: no {name} access point for "
            f"{self._get_profile_ssid(uuid)!r} in the scan of {iface}"
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
                f"{(scan.get('stderr', '') or 'timed out')[:160]}"
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
        expected_band = settings.get("band_preference", "a")
        if (
            not self._band_preference_applies_here(settings, active_ssid)
            or live_band == expected_band
        ):
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
        "password instead of connecting."
    )

    def _backend_is_iwd(self) -> bool:
        return self._get_current_backend() == "iwd"

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
            status["live"]["wifi_backend"] = self._get_current_backend() or ""

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
        current_rmem = self._run_cmd(
            ["/usr/bin/sysctl", "-n", "net.core.rmem_max"], timeout=timeout
        ).get("stdout", "").strip()
        # In auto mode with no stream running these are off on purpose.
        gate_open = self._volatile_gate_open(settings)
        status["live"]["buffer_tuning_applied"] = current_rmem == TUNED_RMEM_MAX
        if (settings.get("buffer_tuning_enabled") and current_rmem != TUNED_RMEM_MAX
                and gate_open):
            status["drift"]["buffer_tuning"] = True

        cake_active = self._get_cake_status(iface)
        status["live"]["cake_applied"] = cake_active
        if settings.get("cake_enabled") and not cake_active and gate_open:
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
        log = self._run_cmd(
            ["/usr/bin/journalctl", "-u", "NetworkManager", "--since", "-10min",
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
            return
        want = str(settings.get("dns_servers", "")).replace(",", " ").split()
        if not want:
            return
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "ipv4.dns,ipv4.ignore-auto-dns",
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
        if pinned and not self._lock_scan_is_fresh(settings or {}, iface, now,
                                                   "_reach_scan_used"):
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
        self._lock_weaker_checked_at = now
        self._lock_weaker_checked_for = key
        if not self._lock_scan_is_fresh(settings, iface, now):
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
                            used: str = "_lock_scan_used") -> bool:
        """Whether nmcli's cached list is fresh enough to judge the lock on.

        Asks for a scan when one is due, and then says no: the answer takes a
        few seconds to land (_LOCK_SCAN_SETTLE_SECONDS), and asking returns at
        once rather than holding the poll. A read counts once it is past that
        and before the next scan is due, and only one read per scan counts,
        since two reads of one scan are one sample. Each check keeps its own
        record of that (used) and they share the scans, so a second check
        never adds one. Under a stream no scan is asked for, so nothing is
        fresh and nothing new is said until it ends.
        """
        asked = getattr(self, "_lock_scan_at", None)
        if asked is None or now - asked >= self._LOCK_SCAN_SECONDS:
            if not self._stream_running(settings):
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

    async def allow_wpa2(self) -> dict:
        """Let a WPA3-only profile use the WPA2-only access points of its network.

        NetworkManager's wpa-psk key management offers WPA2 and WPA3 alike,
        and the supplicant still chooses WPA3 wherever an access point offers
        it, so this only widens what can be joined. If the profile cannot
        connect afterwards it is put back as it was.
        """
        if self._profile_change_in_flight() or self._update_running():
            return dict(self._BUSY_RESULT)
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        held = True
        try:
            uuid, err = self._resolve_uuid("Connect to WiFi first")
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
            done = self._nmcli_modify(
                uuid, "802-11-wireless-security.key-mgmt", "wpa-psk"
            )
            if not done["success"]:
                return {"success": False, "error": "nmcli_failed",
                        "message": "Couldn't change the network's security",
                        "detail": done["stderr"]}
            if not await self._reconnected_to(uuid):
                self._nmcli_modify(uuid, "802-11-wireless-security.key-mgmt", "sae")
                await self._reconnected_to(uuid)
                decky.logger.error(
                    f"Allowing WPA2 on {uuid} did not reconnect; put back to WPA3 only"
                )
                return {"success": False, "error": "reconnect_failed",
                        "message": "Couldn't reconnect, so the network was left as it was."}
            decky.logger.info(f"Allowed WPA2 alongside WPA3 on {uuid}")
            self._wpa2_blocked = None
            self._reach_checked_at = 0.0
            # The lock picks its access point from those the profile can join,
            # so with the stronger one now eligible, let it choose again.
            if _load_settings().get("bssid_lock_enabled"):
                # Hand the guard to the lock setter rather than hold it
                # across the call, or it refuses as busy. Nothing awaits
                # between the release and the setter taking it.
                self._profile_change_depth = max(0, self._profile_change_depth - 1)
                held = False
                await self.set_bssid_lock(True)
            return {"success": True, "reconnected": True}
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
        if not self._hard_reconnect(uuid):
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
        # them again.
        if rivals and settings.get("priority_set"):
            settled = self._precedence_priority(uuid, rivals=rivals)
            if settled is not None:
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
                status["live"]["dispatcher_installed"] = os.path.isfile(
                    DISPATCHER_PATH
                )
                return status, pending, actions

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
            if (settings.get("power_save_disabled") and not ps_off
                    and self._volatile_gate_open(settings)):
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

    async def set_power_save(self, disabled: bool) -> dict:
        try:
            if disabled and self._paused_by_streaming():
                return self._record_paused("power_save_disabled")
            # Auto mode keeps power save to the session: nothing persistent.
            auto = bool(_load_settings().get("streaming_mode_enabled"))

            iface = self._get_wifi_interface()

            # Apply immediately if connected - verify before saving
            if iface:
                state = "off" if disabled else "on"
                result = self._run_cmd(
                    ["/usr/bin/iw", "dev", iface, "set", "power_save", state]
                )
                if not result["success"]:
                    return {
                        "success": False,
                        "error": "iw_failed",
                        "message": "Couldn't change WiFi power save",
                        "detail": result["stderr"],
                    }

            # Write or remove NM config (persistent layer)
            before = _load_settings().get("valve_powersave_before")
            if disabled and not auto:
                # What Valve's file said before we first took power save
                # over, so stopping can put it back. "" records "nothing".
                if not os.path.exists(NM_CONF_PATH):
                    found = self._valve_powersave()
                    before = (
                        "" if found is None
                        else found if found in _VALVE_POWERSAVE_VALUES else "?"
                    )
                os.makedirs(os.path.dirname(NM_CONF_PATH), exist_ok=True)
                with open(NM_CONF_PATH, "w") as f:
                    f.write("[connection]\nwifi.powersave = 2\n")
            elif not disabled:
                # Only our own file can have put our value into Valve's. With
                # none to remove (always so in streaming auto mode, which never
                # writes it), a 2 there is the user's own Steam setting, and
                # releasing it at the end of every stream turned it back on.
                try:
                    os.remove(NM_CONF_PATH)
                    self._reload_nm_conf()
                    self._release_valve_powersave(_load_settings())
                    before = "?"
                except FileNotFoundError:
                    pass

            self._apply_driver_fixes(disabled, persist=not auto)
            # Only when auto mode is putting the fixes on from off. Any other
            # call finds them already on, and would record our own values.
            not_applied = self._apply_pcie_aspm_fix(
                disabled, record_all=auto and getattr(self, "_gate_opening", False)
            )

            # Save settings only after success
            settings = _load_settings()
            settings["power_save_disabled"] = disabled
            settings["valve_powersave_before"] = before
            _save_settings_with_timestamp(settings)

            result = {"success": True, "power_save_off": disabled}
            if not_applied:
                # WiFi power save itself is off, which is most of the benefit,
                # but the toggle also promises the PCIe power states and some
                # did not take. Say which rather than claim all of it.
                result["message"] = (
                    "WiFi power save is off, but some PCIe power states could "
                    f"not be disabled: {', '.join(not_applied)}."
                )
            return result
        except Exception as e:
            decky.logger.error(f"set_power_save error: {e}")
            return self._unexpected_response(e)

    async def set_auto_fix(self, enabled: bool) -> dict:
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
                    "message": "Couldn't remove the auto-fix script.",
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
            return {
                "success": False,
                "error": "write_failed",
                "message": "Couldn't change the auto-fix script.",
            }

    def _release_lock_from_all_profiles(self) -> dict | None:
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

        Deliberately await-free, as the rest of this setter's body is.
        """
        # Disabling works on saved profiles - no active WiFi needed
        _iface, uuid, _err = self._require_wifi()
        if not uuid:
            uuid = self._get_saved_connection_uuid()
        if not uuid:
            return {
                "success": False,
                "error": "nmcli_failed",
                "message": "No connection UUID found. Connect to WiFi first.",
            }

        result = self._nmcli_modify(uuid, "802-11-wireless.bssid", "")
        if not result["success"]:
            return {
                "success": False,
                "error": "nmcli_failed",
                "message": "Couldn't unlock BSSID",
                "detail": result["stderr"],
            }

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
                f"{stale_uuid}: {detail[:120]}"
            )
            # A profile that is merely gone needs no further thought.
            # Anything else is still pinned, so keep the record of it
            # rather than forgetting the only reference to a profile
            # that will now fail to associate.
            if "unknown connection" not in detail.lower():
                unresolved.append(stale_uuid)

        settings["bssid_lock_enabled"] = False
        settings["bssid_lock_value"] = ""
        settings["bssid_lock_connection_uuid"] = ""
        settings["bssid_lock_uuids"] = unresolved
        _save_settings_with_timestamp(settings)
        if not self._hard_reconnect(uuid):
            return dict(self._RADIO_OFF_RESULT)

        return None

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
                    f"{iface} after the band reconnect - {detail[:200]}"
                )
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't read the WiFi link.",
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
        # Held for the whole call. The enable path awaits while it waits for
        # an association, so a status poll can now land in the middle of this
        # setter where previously none could - and the one it would run is
        # exactly the re-point that writes a BSSID to this profile.
        if self._profile_change_in_flight() or self._update_running():
            return dict(self._BUSY_RESULT)
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

            err = self._release_lock_from_all_profiles()
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

    async def _lock_onto_best(self, iface: str, uuid: str, moving: bool) -> dict:
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
            if read_error:
                decky.logger.error(
                    f"BSSID lock: could not read the link on {iface} "
                    f"- {read_error[:200]}"
                )
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't read the WiFi link.",
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
        if moving and not moved_to:
            decky.logger.info(
                f"Not moving the BSSID lock: nothing clearly stronger than "
                f"{bssid} (signal {here_signal}) on a fresh look"
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
                f"Moving the BSSID lock from {bssid} (signal {here_signal}) "
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
                "message": "Couldn't lock BSSID",
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
        settings["bssid_lock_uuids"] = known[-self._MAX_TRACKED_LOCK_UUIDS:]
        _save_settings_with_timestamp(settings)
        if not self._hard_reconnect(uuid):
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
                f"{put_back.get('stderr', '')[:120]}; turning the lock off"
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
            if not self._hard_reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)
            return {"success": False, "error": "move_failed",
                    "reconnected": True, "message": message}
        restored["bssid_lock_value"] = previous
        _save_settings_with_timestamp(restored)
        if not self._hard_reconnect(uuid):
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
        if (
            self._profile_change_in_flight()
            or self._update_running()
            or (getattr(self, "_backend_switch", None) or {}).get("in_progress")
        ):
            return dict(self._BUSY_RESULT)
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

    def _relock_after_band_change(self, uuid: str):
        """Pin the access point NetworkManager chose on the new band.

        Only while the lock is still on: the band setter awaits before this,
        and the lock can have been turned off in between.
        """
        if not _load_settings().get("bssid_lock_enabled"):
            return
        iface = self._get_wifi_interface()
        # The reconnect may have landed on a different profile - a
        # duplicate for the same network, which is the situation this
        # plugin exists to handle. Pinning what we see onto the profile
        # we meant to change would write a foreign address, possibly on
        # the other band, into a profile that now demands this one.
        landed_on = self._get_active_connection_uuid()
        if iface and landed_on == uuid:
            link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
            if not link_result["success"]:
                # Not fatal - reconciliation re-points the lock on a
                # later poll - but silence here left the lock enabled
                # in settings and absent from the profile with nothing
                # said about why.
                decky.logger.error(
                    f"Band change: could not read the link on {iface} "
                    f"to restore the access point lock - "
                    f"{(link_result.get('stderr', '') or '')[:200]}"
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
                            settings = _load_settings()
                            settings["bssid_lock_value"] = new_bssid
                            _save_settings(settings)
                            decky.logger.info(
                                f"Re-locked BSSID to {new_bssid} after band change"
                            )
                        else:
                            # Recording a lock that was not applied
                            # would report it as present forever.
                            decky.logger.error(
                                f"Could not re-lock BSSID after band change: "
                                f"{relocked.get('stderr', '')[:120]}"
                            )
                    break

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
            return refuse(
                "6 GHz uses WPA3, and this network is saved for WPA2. "
                "NetworkManager 1.58.1 or newer can use WPA3 for it."
            )
        pmf = self._read_profile_field(uuid, "802-11-wireless-security.pmf")
        if pmf.split(" ")[0] in ("1", "disable"):
            return refuse(
                "This network is saved with protected management frames "
                "turned off, and 6 GHz requires them."
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
        iface = self._get_wifi_interface()
        seen = await self._await_association(iface) if iface else {"link": "", "error": "no interface"}
        frequency = self._parse_link(seen.get("link", "")).get("frequency", "")
        if (self._freq_band(frequency) == "6GHz"
                and self._get_active_connection_uuid() == uuid):
            return None
        decky.logger.error(
            f"Band '6GHz' on {uuid} did not connect on 6 GHz "
            f"(frequency {frequency!r}{', ' + seen['error'] if seen.get('error') else ''}); "
            f"putting the band back"
        )
        undone = self._nmcli_modify(uuid, "802-11-wireless.band", previous_band)
        if not undone["success"]:
            decky.logger.error(
                f"Could not put the band back on {uuid}: {undone.get('stderr', '')[:120]}"
            )
        settings = _load_settings()
        for key in ("band_preference_enabled", "band_preference",
                    "band_preference_uuids", "band_preference_ssid", "pins_owned"):
            settings[key] = copy.deepcopy(before.get(key, DEFAULT_SETTINGS[key]))
        _save_settings_with_timestamp(settings)
        if not self._hard_reconnect(uuid):
            return dict(self._RADIO_OFF_RESULT)
        if had_lock:
            await asyncio.sleep(3)
            self._relock_after_band_change(uuid)
        return {
            "success": False,
            "error": "band_unavailable",
            "message": "Couldn't connect on 6 GHz, so the band was put back to how it was.",
            "reconnected": True,
        }

    async def set_band_preference(self, enabled: bool, band: str = "a") -> dict:
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
        if self._profile_change_in_flight() or self._update_running():
            return dict(self._BUSY_RESULT)
        self._profile_change_depth = getattr(self, "_profile_change_depth", 0) + 1
        try:

            if band not in BAND_NAMES:
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": f"Invalid band '{band}'. Must be 'a' (5 GHz), 'bg' (2.4 GHz) or '6GHz'.",
                }
            if enabled and await asyncio.to_thread(self._backend_is_iwd):
                return {"success": False, "error": "needs_wpa_supplicant",
                        "message": "The band preference " + self._NEEDS_WPA_SUPPLICANT}

            uuid, err = self._resolve_uuid(
                "Connect to WiFi first to set band preference" if enabled else None
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
                            f"The band preference is set for {scoped_to}. "
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
                        f"Could not clear band from profile {other}: {detail[:120]}"
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
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't update band preference",
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
                            f"{rollback.get('stderr', '')[:120]}"
                        )
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't release the access point lock for the band change",
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
                settings["band_preference_uuids"] = known[
                    -self._MAX_TRACKED_LOCK_UUIDS:
                ]
                # Only claim the network on a fresh enable. reapply_all calls
                # this setter too, so rewriting it every time would move a
                # preference set at home onto whatever network Force Reapply
                # happened to be pressed on.
                if not settings.get("band_preference_ssid"):
                    settings["band_preference_ssid"] = scope_ssid
            _save_settings_with_timestamp(settings)

            if not self._hard_reconnect(uuid):
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
                self._relock_after_band_change(uuid)

            return {"success": True, "band": value, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_band_preference error: {e}")
            return self._unexpected_response(e)
        finally:
            self._profile_change_depth = max(
                0, getattr(self, "_profile_change_depth", 1) - 1
            )

    async def set_dns(
        self, enabled: bool, provider: str = "cloudflare", custom_servers: str = ""
    ) -> dict:
        try:

            uuid, err = self._resolve_uuid(
                "Connect to WiFi first to set DNS" if enabled else None
            )
            if err:
                return err

            if enabled:
                if provider == "custom":
                    if not custom_servers or not custom_servers.strip():
                        return {
                            "success": False,
                            "error": "nmcli_failed",
                            "message": "Custom DNS servers cannot be empty",
                        }
                    # Older versions stored the field as typed, and nmcli
                    # also accepts commas, so a list saved that way is valid.
                    servers = " ".join(custom_servers.replace(",", " ").split())
                    parts = servers.split()
                    if len(parts) > 6 or not all(
                        DNS_SERVER_RE.match(part) for part in parts
                    ):
                        return {
                            "success": False,
                            "error": "nmcli_failed",
                            "message": "DNS servers must be a space-separated list of IP addresses.",
                        }
                elif provider in DNS_PROVIDERS:
                    servers = DNS_PROVIDERS[provider]
                else:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": f"Unknown DNS provider '{provider}'",
                    }

                previous = self._read_profile_field(uuid, "ipv4.dns")
                result = self._nmcli_modify(uuid, "ipv4.dns", servers)
                if not result["success"]:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't set DNS",
                        "detail": result["stderr"],
                    }

                result2 = self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "yes")
                if not result2["success"]:
                    # Half applied: our servers merged in beside the router's.
                    # Put the profile back the way it was rather than leave a
                    # change the settings do not record.
                    self._nmcli_modify(uuid, "ipv4.dns", previous.replace(",", " "))
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't set ignore-auto-dns",
                        "detail": result2["stderr"],
                    }
            else:
                # The provider is reset alongside the servers. Leaving it on
                # custom with nothing stored is a dead end: the panel only
                # renders the provider dropdown and the servers field while
                # DNS is on, and turning it on with an empty custom list is
                # refused - so after a reload there is no way back except
                # resetting every setting.
                provider = "cloudflare"
                # Order matters and both results matter. Clearing the servers
                # first and then failing to re-enable the automatic ones
                # leaves a profile that ignores DHCP DNS and has none of its
                # own, so it reconnects with no resolvers at all - and with
                # DNS recorded as off, the drift check would not report it.
                restored = self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "no")
                if not restored["success"]:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't restore automatic DNS",
                        "detail": restored["stderr"],
                    }
                cleared = self._nmcli_modify(uuid, "ipv4.dns", "")
                if not cleared["success"]:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't clear the custom DNS servers",
                        "detail": cleared["stderr"],
                    }
                servers = ""

            settings = _load_settings()
            known = [u for u in settings.get("dns_uuids", []) if u != uuid]
            if enabled:
                known.append(uuid)
            else:
                # Every other profile we wrote to, not just the one in use.
                # One we cannot reach stays listed for the next attempt.
                known = [u for u in known if not self._clear_dns(u)]
            settings["dns_uuids"] = known[-self._MAX_TRACKED_LOCK_UUIDS:]
            settings["dns_enabled"] = enabled
            settings["dns_provider"] = provider
            settings["dns_servers"] = servers
            _save_settings_with_timestamp(settings)

            if not self._hard_reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)
            return {"success": True, "dns_set": enabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_dns error: {e}")
            return self._unexpected_response(e)

    async def set_ipv6(self, disabled: bool) -> dict:
        try:

            uuid, err = self._resolve_uuid(
                "Connect to WiFi first to disable IPv6" if disabled else None
            )
            if err:
                return err

            method = "disabled" if disabled else "auto"
            result = self._nmcli_modify(uuid, "ipv6.method", method)
            if not result["success"]:
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't update IPv6 setting",
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
                settings["ipv6_uuids"] = known[-self._MAX_TRACKED_LOCK_UUIDS:]
            else:
                settings["ipv6_uuids"] = [u for u in known if u != uuid]
            _save_settings_with_timestamp(settings)

            if not self._hard_reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)
            return {"success": True, "ipv6_disabled": disabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_ipv6 error: {e}")
            return self._unexpected_response(e)

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
                decky.logger.error(f"sysctl {key}={value} failed: {result['stderr']}")
        return failed

    def _buffer_tuning_in_place(self, settings: dict) -> bool:
        """Whether reset or uninstall has anything of ours to undo. Writing the
        restore values unconditionally replaced whatever the system had even
        for people who never turned tuning on."""
        if settings.get("buffer_tuning_enabled"):
            return True
        live = self._run_cmd(["/usr/bin/sysctl", "-n", "net.core.rmem_max"])
        return (live.get("stdout") or "").strip() == TUNED_RMEM_MAX

    def _revert_queue_tuning(self, settings: dict):
        """Undo buffer tuning and CAKE for reset and uninstall, and only what
        we actually changed: the system's own values are left alone for
        someone who never turned either on."""
        tuned = self._buffer_tuning_in_place(settings)
        if tuned:
            self._restore_buffer_sysctls(settings)
        iface = self._get_wifi_interface()
        if iface:
            cake = settings.get("cake_enabled") or self._get_cake_status(iface)
            if tuned or cake:
                self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", "1000"])
            if cake:
                self._run_cmd(["/usr/bin/tc", "qdisc", "del", "dev", iface, "root"])

    async def set_buffer_tuning(self, enabled: bool) -> dict:
        try:
            if enabled and self._paused_by_streaming():
                return self._record_paused("buffer_tuning_enabled")
            settings = _load_settings()
            if enabled:
                self._capture_sysctl_baseline(settings)
                failed_keys = []
                for key, value in SYSCTL_PARAMS.items():
                    result = self._run_cmd(
                        ["/usr/bin/sysctl", "-w", f"{key}={value}"]
                    )
                    if not result["success"]:
                        failed_keys.append(key)
                        decky.logger.error(f"sysctl {key}={value} failed: {result['stderr']}")
                if len(failed_keys) == len(SYSCTL_PARAMS):
                    # Reporting success here recorded the setting as on, which
                    # then showed as drift on every poll with no control able
                    # to clear it: Optimize Safe just calls back into here and
                    # fails again.
                    return {
                        "success": False,
                        "error": "unexpected",
                        "message": "Couldn't apply network buffer settings.",
                    }
            else:
                # Refusing to disable would leave the setting recorded as on
                # with a toggle that will not move, which is worse than
                # recording the user's intent and logging that the restore
                # did not take.
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
                if settings.get("cake_enabled") and self._volatile_gate_open(settings):
                    txq = "256"
                else:
                    txq = "2000" if enabled else "1000"
                self._run_cmd(
                    ["/usr/bin/ip", "link", "set", iface, "txqueuelen", txq]
                )

            settings["buffer_tuning_enabled"] = enabled
            _save_settings_with_timestamp(settings)
            result = {"success": True, "buffer_tuning": enabled}
            if failed_keys:
                # Some took and some did not. The status poll only checks one
                # of them, so without saying so here a partial apply looks
                # exactly like a full one.
                names = ", ".join(k.rsplit(".", 1)[-1] for k in failed_keys)
                result["message"] = f"Applied, except: {names}."
            return result
        except Exception as e:
            decky.logger.error(f"set_buffer_tuning error: {e}")
            return self._unexpected_response(e)

    def _get_cake_status(self, iface: str) -> bool:
        """Check if CAKE qdisc is active on the interface."""
        result = self._run_cmd(["/usr/bin/tc", "qdisc", "show", "dev", iface])
        return "cake" in result.get("stdout", "")

    async def set_cake(self, enabled: bool) -> dict:
        """Enable or disable CAKE QoS (unlimited mode: FQ + AQM + ack-filter, no bandwidth shaper)."""
        try:
            if enabled and self._paused_by_streaming():
                return self._record_paused("cake_enabled")
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
                if not result["success"]:
                    return {
                        "success": False,
                        "error": "unexpected",
                        "message": "Failed to apply CAKE qdisc.",
                        "detail": result.get("stderr", ""),
                    }
                # Lower txqueuelen to complement CAKE's queue management
                self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", "256"])
                decky.logger.info(f"CAKE enabled (unlimited) on {iface}")
            else:
                removal = self._run_cmd(
                    ["/usr/bin/tc", "qdisc", "del", "dev", iface, "root"]
                )
                if not removal["success"]:
                    # Not fatal - tc reports an error when there is no qdisc
                    # to remove - but worth a line when it is something else.
                    decky.logger.info(
                        f"tc qdisc del on {iface}: {removal.get('stderr', '')[:120]}"
                    )
                    if self._get_cake_status(iface):
                        return {
                            "success": False,
                            "error": "unexpected",
                            "message": "Couldn't remove CAKE.",
                            "detail": removal.get("stderr", ""),
                        }
                # Restore txqueuelen based on whether buffer tuning is active
                settings = _load_settings()
                tuned = settings.get("buffer_tuning_enabled") and self._volatile_gate_open(settings)
                txq = "2000" if tuned else "1000"
                self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", txq])
                decky.logger.info(f"CAKE disabled on {iface}")

            settings = _load_settings()
            settings["cake_enabled"] = enabled
            _save_settings_with_timestamp(settings)
            return {"success": True, "cake": enabled}
        except Exception as e:
            decky.logger.error(f"set_cake error: {e}")
            return self._unexpected_response(e)

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
            # so Optimize Safe leaves it out rather than report it failed.
            if await asyncio.to_thread(self._backend_is_iwd):
                total -= 1
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
                "reconnected": True,
            }
        except Exception as e:
            decky.logger.error(f"optimize_safe error: {e}")
            return self._unexpected_response(e)

    def _record_reapplied(self, last_changed: int):
        """Put back "Last changed" after a re-application, and stamp it as one.

        Re-applying reuses the setters, and every setter stamps the time of
        the user's last change. So each boot and each game launch read in the
        panel as a change made just now. Re-applying is what the dispatcher
        does after a reconnect, and the panel already shows that separately as
        "Auto-applied", from the file the dispatcher writes. Use that.
        """
        settings = _load_settings()
        if settings.get("last_applied", 0) != last_changed:
            settings["last_applied"] = last_changed
            _save_settings(settings)
        try:
            os.makedirs(os.path.dirname(ENFORCED_FILE), mode=0o755, exist_ok=True)
        except OSError:
            pass
        _write_no_follow(ENFORCED_FILE, str(int(time.time())))

    async def reapply_volatile(self) -> dict:
        """Reapply volatile (non-reconnecting) settings. Safe to call mid-stream."""
        try:
            settings = _load_settings()
            if not self._volatile_gate_open(settings):
                # Auto mode with no stream: the fixes stay off until one starts.
                return {"success": True, "applied": 0, "total": 0, "paused": True}
            applied = 0
            total = 0

            if settings.get("power_save_disabled"):
                total += 1
                r = await self.set_power_save(True)
                if r.get("success"):
                    applied += 1

            if settings.get("buffer_tuning_enabled"):
                total += 1
                r = await self.set_buffer_tuning(True)
                if r.get("success"):
                    applied += 1

            if settings.get("cake_enabled"):
                total += 1
                r = await self.set_cake(True)
                if r.get("success"):
                    applied += 1

            if total > 0:
                decky.logger.info(f"reapply_volatile: {applied}/{total} applied")
                self._record_reapplied(settings.get("last_applied", 0))

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

            if settings.get("buffer_tuning_enabled"):
                total += 1
                r = await self.set_buffer_tuning(True)
                results["buffer_tuning"] = r
                if r.get("success"):
                    applied += 1

            if settings.get("cake_enabled"):
                total += 1
                r = await self.set_cake(True)
                results["cake"] = r
                if r.get("success"):
                    applied += 1

            # Reconnecting (each does hard_reconnect)
            here_uuid = self._get_active_connection_uuid() or ""
            if self._backend_is_iwd():
                # Neither pin can be used under iwd; see _pins_for_backend.
                settings = {**settings, "bssid_lock_enabled": False,
                            "band_preference_enabled": False}
            if settings.get("bssid_lock_enabled") and not self._lock_is_for_this_network(
                settings, here_uuid, self._get_profile_ssid(here_uuid)
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
                here = self._get_profile_ssid(
                    self._get_active_connection_uuid() or ""
                )
                # An unknown scope is not permission. Everything else treats
                # it as "do not enforce"; treating it as "go ahead" here would
                # write the band into whatever network is active and reconnect
                # into it, which is the stranding this scoping exists to stop.
                if not scoped_to or here != scoped_to:
                    decky.logger.info(
                        f"Skipping band preference: it belongs to {scoped_to!r}, "
                        f"this is {here!r}"
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
            active_uuid = self._get_active_connection_uuid() or ""

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

            # Power save last (sticks after any reconnects, dispatcher also reapplies)
            if settings.get("power_save_disabled"):
                total += 1
                r = await self.set_power_save(True)
                results["power_save"] = r
                if r.get("success"):
                    applied += 1

            if total == 0:
                return {
                    "success": True,
                    "total": 0,
                    "applied": 0,
                    "results": {},
                    "message": "No optimizations enabled",
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
            steps = (
                ("buffer_tuning", "buffer_tuning_enabled",
                 lambda: self.set_buffer_tuning(True)),
                ("cake", "cake_enabled", lambda: self.set_cake(True)),
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
                 lambda: self.set_power_save(True)),
            )
            results = {}
            applied = 0
            reconnected = False
            for key, enabled_key, run in steps:
                if not drift.get(key) or not settings.get(enabled_key):
                    continue
                r = await run()
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

    async def reset_settings(self) -> dict:
        """Delete settings and revert to defaults."""
        # An update reloads the plugin when it finishes, which would cut a
        # reset off between clearing the pins and saving the defaults.
        if self._update_running():
            return dict(self._BUSY_RESULT)
        try:
            # Before the settings naming them are discarded.
            self._clear_profile_pins()
            # Revert runtime state
            self._apply_driver_fixes(False)
            self._apply_pcie_aspm_fix(False)
            self._revert_queue_tuning(_load_settings())
            try:
                os.remove(NM_CONF_PATH)
            except FileNotFoundError:
                pass
            # A settings file set aside as unreadable is kept through a reset,
            # since it may be the only copy of what the user had. Whether
            # they have already dismissed the notice about it is kept too:
            # dropping that would bring back a notice they have seen, about a
            # file that has not changed.
            acknowledged = _load_settings().get("corrupt_acknowledged", [])
            self._release_valve_powersave(_load_settings())
            try:
                os.remove(MODPROBE_CONF_PATH)
            except FileNotFoundError:
                pass
            # In the user's directory: _remove_no_follow, which does not
            # follow a link swapped in anywhere along the way.
            for path in (SETTINGS_FILE, LEGACY_ENFORCED_FILE):
                try:
                    _remove_no_follow(path)
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
            await asyncio.to_thread(self._release_generic_backend_conf)

            # Repopulate model/driver so the plugin doesn't show as "UNKNOWN /
            # Unsupported device" until the next plugin reload. Mirrors the
            # hardware detection _main does on startup.
            info = await self.get_device_info()
            fresh = copy.deepcopy(DEFAULT_SETTINGS)
            fresh["model"] = info.get("model", "unknown")
            fresh["driver"] = info.get("driver", "unknown")
            fresh["device_family"] = info.get("device_family", "unknown")
            fresh["device_label"] = info.get("device_label", "Unknown Device")
            fresh["chip_label"] = info.get("chip_label", "unknown")
            fresh["supports_6ghz"] = info.get("supports_6ghz", False)
            distro = self._detect_distro()
            fresh["distro_id"] = distro["id"]
            fresh["distro_name"] = distro["name"]
            fresh["corrupt_acknowledged"] = acknowledged
            _save_settings(fresh)

            decky.logger.info("Settings reset to defaults")
            return {"success": True, "message": "Settings reset to defaults"}
        except Exception as e:
            decky.logger.error(f"reset_settings error: {e}")
            return self._unexpected_response(e)

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

    def _find_stream(self, patterns: list[tuple[str, str]]) -> dict | None:
        """The first running process whose command line has a pattern in it."""
        if not patterns:
            return None
        own = str(os.getpid())
        try:
            pids = [d for d in os.listdir(PROC_DIR) if d.isdigit() and d != own]
        except OSError:
            return None
        for pid in pids:
            try:
                with open(os.path.join(PROC_DIR, pid, "cmdline"), "rb") as f:
                    raw = f.read(4096)
            except OSError:
                continue
            if not raw:
                continue  # a kernel thread
            cmdline = raw.replace(b"\0", b" ").decode("utf-8", "replace").lower()
            for label, pattern in patterns:
                if pattern in cmdline:
                    try:
                        with open(os.path.join(PROC_DIR, pid, "comm"), "rb") as f:
                            name = f.read(64).decode("utf-8", "replace").strip()
                    except OSError:
                        name = ""
                    return {"app": label, "process": name or pid}
        return None

    def _set_stream_flag(self, on: bool, path: str | None = None):
        path = path or STREAM_FLAG_FILE
        if on:
            try:
                os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
            except OSError:
                pass
            _write_no_follow(path, f"{int(time.time())}\n")
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def _revert_volatile_fixes(self, settings: dict):
        """Take power save, buffer tuning and CAKE off, keeping them chosen."""
        if settings.get("power_save_disabled"):
            iface = self._get_wifi_interface()
            # A 2 in Valve's file is Steam's own Wi-Fi power management
            # switched off; power save then stays off because the user said.
            if iface and self._valve_powersave() != "2":
                self._run_cmd(["/usr/bin/iw", "dev", iface, "set", "power_save", "on"])
            self._apply_driver_fixes(False)
            self._apply_pcie_aspm_fix(False)
        self._revert_queue_tuning(settings)

    async def _stream_pass(self, settle: bool = False):
        """Look for a stream, and apply or take off the fixes if that changed.

        settle skips the wait before taking them off, for when the panel
        knows something just exited or the user changed what is watched.
        """
        async with self._stream_guard():
            await self._stream_pass_locked(settle)

    def _stream_guard(self) -> asyncio.Lock:
        """Held for every change to whether the fixes are on, so a pass and a
        toggle never apply and take off the fixes at the same time."""
        if getattr(self, "_stream_lock", None) is None:
            self._stream_lock = asyncio.Lock()
        return self._stream_lock

    async def _stream_pass_locked(self, settle: bool = False):
        settings = _load_settings()
        if not settings.get("streaming_mode_enabled"):
            return
        found = await asyncio.to_thread(self._find_stream, self._stream_patterns(settings))
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
        self._set_stream_flag(want)
        if want:
            why = f"{found['app']} ({found['process']})" if found else "held on"
            decky.logger.info(f"Streaming auto mode: {why}, applying the fixes")
            self._gate_opening = True
            try:
                await self.reapply_volatile()
            finally:
                self._gate_opening = False
        else:
            decky.logger.info("Streaming auto mode: no stream running, fixes off")
            await asyncio.to_thread(self._revert_volatile_fixes, settings)

    async def _streaming_watch(self):
        """Look every few seconds for as long as auto mode is on."""
        while True:
            await asyncio.sleep(STREAM_POLL_SECONDS)
            try:
                if not _load_settings().get("streaming_mode_enabled"):
                    return
                await self._stream_pass()
            except Exception as e:
                self._log_throttled("stream_watch", f"streaming watch error: {e}")

    def _start_streaming_watch(self):
        task = getattr(self, "_stream_task", None)
        if task is None or task.done():
            self._stream_task = asyncio.create_task(self._streaming_watch())

    def _drop_persistent_power_save(self):
        """Remove what keeps power save off beyond a session.

        NetworkManager's file holds it off on every connect and the modprobe
        options at every module load, both whether or not anything streams.
        """
        had_conf = os.path.exists(NM_CONF_PATH)
        for path in (NM_CONF_PATH, MODPROBE_CONF_PATH):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        settings = _load_settings()
        if had_conf:
            self._reload_nm_conf()
            # Only while our file existed can a 2 in Valve's be ours. On a
            # load with none, it is the user's Steam setting and stays.
            self._release_valve_powersave(settings)
        # With our file gone nothing of ours can reach Valve's any more, so
        # what it says now is the user's own (Steam's Wi-Fi power management
        # setting). Recorded, so a later Reset or uninstall leaves it alone
        # rather than taking an unrecorded 2 for ours.
        found = self._valve_powersave()
        settings["valve_powersave_before"] = (
            "" if found is None else found if found in _VALVE_POWERSAVE_VALUES else "?"
        )
        _save_settings(settings)

    def _reload_nm_conf(self):
        """Have NetworkManager read its config files again.

        It reads them at startup and keeps them, so a file of ours removed
        mid-boot would otherwise go on holding power save off at every
        reconnect until the next reboot. Reloading the config leaves
        connections alone.
        """
        result = self._run_cmd(["/usr/bin/nmcli", "general", "reload", "conf"], timeout=10)
        if not result["success"]:
            decky.logger.info(f"nmcli general reload conf: {result['stderr'][:120]}")

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
        await asyncio.to_thread(self._drop_persistent_power_save)
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
        try:
            if self._profile_change_in_flight() or self._update_running():
                return dict(self._BUSY_RESULT)
            # Taken first, so a pass already applying or taking off the fixes
            # (in a worker thread, which cancelling would not stop) finishes
            # before this changes anything. The watch then ends on its own.
            async with self._stream_guard():
                settings = _load_settings()
                settings["streaming_mode_enabled"] = bool(enabled)
                _save_settings_with_timestamp(settings)
                if enabled:
                    await asyncio.to_thread(self._drop_persistent_power_save)
                    # Until now the fixes applied all the time, so they are
                    # on. A settled pass takes them off unless a stream runs.
                    self._set_stream_flag(True)
                    self._stream_misses = 0
                    await self._stream_pass_locked(settle=True)
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
            return self._unexpected_response(e)

    async def set_streaming_hold(self, held: bool) -> dict:
        """Keep the fixes on until reboot, for a stream that is not detected."""
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
            kept, refused = _parse_stream_patterns(str(text)[:512])
            settings = _load_settings()
            settings["streaming_custom_patterns"] = " ".join(kept)
            _save_settings(settings)
            await self._stream_pass(settle=True)
            result = {"success": True, "patterns": " ".join(kept)}
            if refused:
                result["message"] = (
                    f"Left out: {' '.join(refused)}. Patterns need at least "
                    f"{STREAM_PATTERN_MIN} characters and must not match "
                    "something that is always running, like Steam."
                )
            return result
        except Exception as e:
            decky.logger.error(f"set_streaming_patterns error: {e}")
            return self._unexpected_response(e)

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
                return {"success": False, "message": "Channel must be 'stable' or 'beta'"}
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
                decky.logger.error(f"Update check: curl failed - rc={result.get('returncode')}, stderr={result.get('stderr', '')[:200]}")
                return {
                    "success": False,
                    "current_version": current,
                    "update_available": False,
                    "channel": channel,
                    "message": "Couldn't reach GitHub",
                }

            data = json.loads(result["stdout"])

            if channel == "beta":
                latest = self._newest_release(data)
            else:
                tag = data.get("tag_name", "") if isinstance(data, dict) else ""
                latest = tag.lstrip("v") if isinstance(tag, str) else ""

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
                # GitHub explains a refusal (a rate limit) in "message".
                msg = data.get("message") if isinstance(data, dict) else None
                if not isinstance(msg, str) or not msg:
                    msg = "No releases found." if channel == "beta" else "couldn't parse version"
                decky.logger.error(f"Update check: no version - {msg}")
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
                    f"http={http[:8]!r}, {result.get('stderr', '')[:200]}"
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
            if await asyncio.to_thread(self._game_or_stream_running, settings):
                return {"success": False,
                        "message": "Close your game or stream first, then update."}
            if not isinstance(version, str) or len(version) > 64 or not VERSION_RE.match(version):
                decky.logger.error(f"Update refused: approved version {version!r} malformed")
                return {"success": False, "message": "Check for updates again, then retry."}
            info = await self.check_for_update()
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
            label = f"beta v{latest}" if channel == "beta" else f"v{latest}"

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
            try:
                await asyncio.to_thread(
                    self._launch_updater,
                    ["/bin/bash", script_path, script_dir, plugin_dir,
                     download_url, src_dir, label, latest, sha256],
                )
            except Exception:
                # Nothing started, so nothing will remove the directory, and
                # its presence would read as an update running until Decky
                # restarts - refusing every later update and backend switch.
                for leftover in (script_path, script_dir):
                    try:
                        (os.remove if leftover == script_path else os.rmdir)(leftover)
                    except OSError:
                        pass
                raise
            # The script removes its own directory when it exits, so while
            # the directory exists an update is running.
            self._update_dir = script_dir

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
                f"systemd-run could not start the update ({result['stderr'][:200]}), "
                "starting it directly"
            )
        subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_child_env(),
        )

    def _update_running(self) -> bool:
        if getattr(self, "_update_starting", False):
            return True
        directory = getattr(self, "_update_dir", None)
        if directory and os.path.isdir(directory):
            return True
        # After a reload this is a new backend that did not start the script,
        # which is still finishing. What the script reports covers that.
        phase, _, age = _read_update_state()
        return phase in UPDATE_ACTIVE_PHASES and age < UPDATE_STATE_MAX_AGE

    async def get_update_progress(self) -> dict:
        """How far a running update has got, for the panel to show and act on."""
        try:
            phase, version, age = _read_update_state()
            if phase in UPDATE_ACTIVE_PHASES and age >= UPDATE_STATE_MAX_AGE:
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

    async def _backend_switch_worker(self, target: str):
        """Background task that switches the WiFi backend with phase transitions.

        Invokes the privileged helper directly (at /usr/bin/steamos-polkit-helpers/…)
        to bypass pkexec, which fails from a rootful systemd context with no polkit
        agent. The helper handles wlan0 recovery on ath11k devices internally; we parse its
        output to report whether recovery fired.
        """
        try:
            settings = _load_settings()
            has_wlan0_quirk = settings.get("driver") == "ath11k_pci"
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
                f"stdout={write_result.get('stdout', '')[:200]!r} "
                f"stderr={write_result.get('stderr', '')[:200]!r}"
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
                    f"detail={detail!r}"
                )
                return

            restart_result = await asyncio.to_thread(
                self._run_cmd, [BACKEND_HELPER, "restart_units", other], 45
            )
            rs_stdout = restart_result.get("stdout", "")
            rs_stderr = restart_result.get("stderr", "")
            recovery_performed = "missing wlan0" in rs_stdout
            needs_reboot = "wlan0 could not be created" in rs_stderr

            await asyncio.sleep(1)
            if has_wlan0_quirk and target == "wpa_supplicant":
                iface_check = await asyncio.to_thread(self._get_wifi_interface)
                # Absence is the failure, not the name. An interface that came
                # back as wlpXsY is working, and treating that as a failure
                # tells the user to reboot a system that is fine.
                if not iface_check:
                    needs_reboot = True

            # The helper reports "wlan0 could not be created" by name, but an
            # interface that came back as wlpXsY is working. Check before
            # acting on that claim, the same way the quirk branch above does.
            if needs_reboot and await asyncio.to_thread(self._get_wifi_interface):
                needs_reboot = False

            # The helper's own recovery only knows about phy0. Try again with
            # the real phy before making the user reboot.
            if needs_reboot:
                if await asyncio.to_thread(self._recover_wlan0):
                    await asyncio.sleep(2)
                    if await asyncio.to_thread(self._get_wifi_interface):
                        needs_reboot = False
                        recovery_performed = True

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
                    "message": "Backend switched but wlan0 didn't come back. Reboot required.",
                }
            elif not restart_result["success"] or final_backend != target:
                detail = rs_stderr[:200] or rs_stdout[:200]
                self._backend_switch["phase"] = "failed"
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
                "message": "Backend switch cancelled",
            }
            raise
        except Exception as e:
            decky.logger.error(f"_backend_switch_worker error: {e}")
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                "message": str(e),
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
            phys = os.listdir("/sys/class/ieee80211")
        except Exception:
            return False
        if not phys:
            return False

        def already_has_netdev(phy: str) -> bool:
            # Adding a second station interface to a phy that already has one
            # is a genuine mess, and the caller reaches here whenever the
            # interface is merely named something other than wlan0.
            try:
                return bool(os.listdir(f"/sys/class/ieee80211/{phy}/device/net"))
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
                path = f"/sys/class/ieee80211/{phy}/{probe}"
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

        phys.sort(key=lambda phy: (mismatch(phy), phy))

        for phy in phys:
            result = self._run_cmd(
                ["/usr/bin/iw", "phy", phy, "interface", "add", "wlan0",
                 "type", "station"],
                timeout=10,
            )
            if result["success"]:
                decky.logger.info(f"Recreated wlan0 on {phy}")
                self._run_cmd(
                    ["/usr/bin/systemctl", "restart", "NetworkManager"],
                    timeout=30,
                )
                return True
            decky.logger.error(
                f"wlan0 recovery on {phy} failed: {result.get('stderr', '')[:120]}"
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
                    "message": "Couldn't reach the SteamOS settings service.",
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
                decky.logger.error(f"steamos-manager switch failed: {detail!r}")
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
                    "message": "Backend switched but the WiFi interface didn't come back. Reboot required.",
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
                        f"{final_backend or 'no backend'}. A reboot may help."
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
                "message": "Backend switch cancelled",
            }
            raise
        except Exception as e:
            decky.logger.error(f"_steamos_manager_backend_switch_worker error: {e}")
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                "message": str(e),
            }
        finally:
            self._backend_switch["in_progress"] = False

    async def _settle_interface(self, backend: str | None) -> tuple:
        """(interface, whether we recreated it) once a backend restart settles.

        Recreating wlan0 is the wpa_supplicant case only. iwd creates its own
        netdev and names it after the phy (wlan31 on a Deck OLED), so adding
        one under it puts a second station interface on the phy iwd is about
        to claim; under iwd the right move is to give it longer.
        """
        # Let the restart settle before deciding the interface is missing.
        # Checking immediately can catch the moment it is down and add a
        # second station netdev to a phy that was about to get its own.
        await asyncio.sleep(2)
        iface = await asyncio.to_thread(self._get_wifi_interface)
        if iface:
            return iface, False
        if backend == "iwd":
            for _ in range(8):
                await asyncio.sleep(1)
                iface = await asyncio.to_thread(self._get_wifi_interface)
                if iface:
                    break
            return iface, False
        if not await asyncio.to_thread(self._recover_wlan0):
            return None, False
        await asyncio.sleep(2)
        iface = await asyncio.to_thread(self._get_wifi_interface)
        return iface, bool(iface)

    def _merged_nm_backend(self) -> str | None:
        """The backend NetworkManager's merged configuration names, from NM
        itself, so the order conf.d files are read in is NM's own."""
        for binary in ("/usr/bin/NetworkManager", "/usr/sbin/NetworkManager"):
            if not os.path.isfile(binary):
                continue
            result = self._run_cmd([binary, "--print-config"], timeout=5)
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
        if any(os.path.exists(unit) for unit in self._IWD_UNITS):
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
        tmp = WIFI_BACKEND_CONF + ".wifiopt-new"
        try:
            with open(tmp, "w") as f:
                f.write("\n".join(out) + "\n")
            os.replace(tmp, WIFI_BACKEND_CONF)
        except OSError as e:
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

    def _put_back_backend_conf(self, before: str | None):
        """Return our backend conf file to what it held before a switch."""
        try:
            if before is None:
                os.remove(GENERIC_BACKEND_CONF)
            else:
                with open(GENERIC_BACKEND_CONF, "w") as f:
                    f.write(before)
        except OSError as e:
            decky.logger.error(f"restoring backend conf: {e}")

    def _restore_generic_backend(self, backend: str):
        """Point NM back at a backend and start it, after a failed switch.

        The one we tried is stopped and disabled first: left enabled, both
        start at the next boot and fight over the interface.
        """
        tried = "iwd" if backend == "wpa_supplicant" else "wpa_supplicant"
        self._run_cmd(["/usr/bin/systemctl", "disable", "--now", tried], timeout=15)
        try:
            with open(GENERIC_BACKEND_CONF, "w") as f:
                if backend == "iwd":
                    f.write("[device]\nwifi.backend=iwd\nwifi.iwd.autoconnect=yes\n")
                else:
                    f.write("[device]\nwifi.backend=wpa_supplicant\n")
        except OSError as e:
            decky.logger.error(f"restoring backend conf: {e}")
        for cmd in (["/usr/bin/systemctl", "enable", backend],
                    ["/usr/bin/systemctl", "start", backend],
                    ["/usr/bin/systemctl", "restart", "NetworkManager"]):
            self._run_cmd(cmd, timeout=15)

    async def _generic_backend_switch_worker(self, target: str):
        """Backend switch for non-SteamOS systems (Bazzite, CachyOS, etc.).
        Writes NM config directly and manages systemd services."""
        try:
            other = "iwd" if target == "wpa_supplicant" else "wpa_supplicant"

            self._backend_switch["phase"] = "switching"
            decky.logger.info(f"generic backend switch: {other} -> {target}")

            os.makedirs(os.path.dirname(GENERIC_BACKEND_CONF), exist_ok=True)
            try:
                with open(GENERIC_BACKEND_CONF) as f:
                    before = f.read(4096)
            except OSError:
                before = None
            if target == "iwd":
                with open(GENERIC_BACKEND_CONF, "w") as f:
                    f.write("[device]\nwifi.backend=iwd\nwifi.iwd.autoconnect=yes\n")
            else:
                with open(GENERIC_BACKEND_CONF, "w") as f:
                    f.write("[device]\nwifi.backend=wpa_supplicant\n")

            # Ask NetworkManager which backend it will now use BEFORE stopping
            # anything. Another conf.d file can outrank ours, and stopping the
            # backend it still names leaves no WiFi at all.
            merged = await asyncio.to_thread(self._merged_nm_backend)
            if merged is not None and merged != target:
                self._put_back_backend_conf(before)
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": merged,
                    "target": target,
                    "message": (
                        f"Another NetworkManager setting on this system keeps "
                        f"{merged} in force, so nothing was changed."
                    ),
                }
                return

            # Stop old, enable + start new, restart NM
            for cmd in [
                ["/usr/bin/systemctl", "stop", other],
                ["/usr/bin/systemctl", "disable", other],
                ["/usr/bin/systemctl", "enable", target],
            ]:
                await asyncio.to_thread(self._run_cmd, cmd, 10)
            started = await asyncio.to_thread(
                self._run_cmd, ["/usr/bin/systemctl", "start", target], 10
            )
            if not started["success"]:
                # Going on would leave NetworkManager pointed at a backend
                # that is not running and the other one stopped: no WiFi.
                # Put the previous one back first.
                await asyncio.to_thread(self._restore_generic_backend, other)
                detail = started.get("stderr", "")[:200]
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "message": f"{target} would not start, so {other} was kept.",
                    "detail": detail,
                }
                return

            restart = await asyncio.to_thread(
                self._run_cmd,
                ["/usr/bin/systemctl", "restart", "NetworkManager"],
                15,
            )
            if not restart["success"]:
                detail = restart.get("stderr", "")[:200]
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "message": self._friendly_backend_error(detail),
                    "detail": detail,
                }
                return

            # Phase: reconnecting
            self._backend_switch["phase"] = "reconnecting"
            reconnect_timed_out = True
            for _ in range(15):
                await asyncio.sleep(1)
                iface = await asyncio.to_thread(self._get_wifi_interface)
                if iface:
                    uuid = await asyncio.to_thread(self._get_active_connection_uuid)
                    if uuid:
                        reconnect_timed_out = False
                        break

            # Judged from what is running, not from the file just written:
            # reading that back always agreed with itself. Another file in
            # conf.d can still win (Bazzite's iwd.conf sorts after ours), and
            # the service can fail after starting. Allow a few seconds for a
            # service that is still coming up.
            final_backend = None
            for _ in range(5):
                final_backend = await asyncio.to_thread(self._effective_generic_backend)
                if final_backend == target:
                    break
                await asyncio.sleep(1)

            if final_backend == target:
                self._backend_switch["phase"] = "done"
                self._backend_switch["result"] = {
                    "success": True,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": False,
                    "needs_reboot": False,
                    "reconnect_timed_out": reconnect_timed_out,
                }
            else:
                # Put the previous backend back rather than leave a half
                # switch: the new one is not in force and the old one is
                # stopped and disabled.
                await asyncio.to_thread(self._restore_generic_backend, other)
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": False,
                    "needs_reboot": False,
                    "reconnect_timed_out": reconnect_timed_out,
                    "message": (
                        f"{target} did not take over, so {other} was put back. "
                        f"A reboot may help."
                    ),
                }

            decky.logger.info(
                f"generic backend switch: target={target}, final={final_backend}, "
                f"reconnect_timed_out={reconnect_timed_out}"
            )
        except asyncio.CancelledError:
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                "message": "Backend switch cancelled",
            }
            raise
        except Exception as e:
            decky.logger.error(f"_generic_backend_switch_worker error: {e}")
            self._backend_switch["phase"] = "failed"
            self._backend_switch["result"] = {
                "success": False,
                "target": target,
                "message": str(e),
            }
        finally:
            self._backend_switch["in_progress"] = False

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
                    "message": "Backend must be 'iwd' or 'wpa_supplicant'.",
                }
            if self._backend_switch.get("in_progress"):
                return {
                    "accepted": False,
                    "reason": "in_progress",
                    "message": "Backend switch already in progress.",
                }
            if self._update_running():
                return {
                    "accepted": False,
                    "reason": "update_in_progress",
                    "message": "An update is being installed. Try again once it finishes.",
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
                    "message": "WiFi backend switch tool not found on this system.",
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
                    "message": f"Backend is already {backend}.",
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
                "message": str(e),
            }
        finally:
            # Anything that returned without starting a worker gives the
            # reservation back, or every later switch reads "in progress".
            if reserved and not launched and previous is not None:
                self._backend_switch.clear()
                self._backend_switch.update(previous)

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
                    "message": f"Couldn't read backend switch status: {e}",
                },
                "message": str(e),
            }
