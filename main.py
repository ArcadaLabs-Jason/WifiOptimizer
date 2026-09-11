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
import pwd
import shlex
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
NM_CONF_PATH = "/etc/NetworkManager/conf.d/99-wifi-optimizer.conf"
MODPROBE_CONF_PATH = "/etc/modprobe.d/99-wifi-optimizer.conf"
BACKEND_HELPER = "/usr/bin/steamos-polkit-helpers/steamos-wifi-set-backend-privileged"
STEAMOSCTL = "/usr/bin/steamosctl"
WIFI_BACKEND_CONF = "/etc/NetworkManager/conf.d/99-valve-wifi-backend.conf"
NM_DEFAULT_CONF = "/usr/lib/NetworkManager/conf.d/10-steamos-defaults.conf"
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
]

try:
    SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
    ENFORCED_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "last_enforced")
except Exception:
    SETTINGS_FILE = "/tmp/wifi-optimizer/settings.json"
    ENFORCED_FILE = "/tmp/wifi-optimizer/last_enforced"

# A version string from the network ends up in a filename, a URL and a root
# shell script, so it is validated the moment it is parsed rather than at each
# use. Digits and dots, with an optional alphanumeric prerelease suffix.
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}(-[A-Za-z0-9.]+)?\Z")

# Interface names are used to build sysfs paths. Kernel names cannot contain a
# separator, but the value is checked rather than assumed.
IFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}\Z")

# NetworkManager connection uuids, as stored in our own settings.
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}\Z")

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

# Kernel defaults, restored when buffer tuning is disabled.
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
    "ipv6_disabled": False,
    "buffer_tuning_enabled": False,
    "cake_enabled": False,
    "last_connection_uuid": "",
    "priority_set": False,
    "distro_id": "unknown",
    "distro_name": "Unknown",
    "update_channel": "stable",
    "last_applied": 0,
}


def _write_no_follow(path: str, data: str) -> bool:
    """Write a file without following a symlink at the final component.

    The settings directory belongs to the desktop user, so anything we write
    there can be replaced with a link to a file we should not be touching.
    Plain open(path, "w") follows that link and truncates the target as root.
    O_NOFOLLOW refuses instead, and O_EXCL means we only ever create.

    This covers the FINAL path component only. The directory itself is equally
    user-owned and could be swapped for a link, redirecting these writes
    wholesale; closing that means holding the directory open with
    O_DIRECTORY|O_NOFOLLOW and writing relative to it throughout. Left open
    deliberately - said here rather than letting the next reader assume the
    whole path is guarded.
    """
    try:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
        )
        with os.fdopen(fd, "w") as f:
            f.write(data)
        return True
    except Exception:
        return False


def _first_existing(paths: list[str]) -> str | None:
    """First path in the list that exists, or None."""
    for path in paths:
        if os.path.isfile(path):
            return path
    return None


def _load_settings() -> dict:
    try:
        # Bounded read. This file belongs to the desktop user and root parses
        # it on every status poll, so an oversized one is a way to make the
        # plugin allocate arbitrarily much, repeatedly. The dispatcher applies
        # the same cap.
        with open(SETTINGS_FILE, "rb") as f:
            raw = f.read(65536)
            if f.read(1):
                raise ValueError("settings file too large")
        data = json.loads(raw)
        # Merge with defaults (adds new keys), then strip stale keys
        merged = {**DEFAULT_SETTINGS, **data}
        merged = {k: v for k, v in merged.items() if k in DEFAULT_SETTINGS}
        # This file lives in a directory the desktop user owns, so its types
        # are not guaranteed. A driver of the wrong type makes the profile
        # lookup raise on an unhashable key and silently kills the feature.
        for key, default in DEFAULT_SETTINGS.items():
            if not isinstance(merged.get(key), type(default)):
                merged[key] = default
        # An out-of-range band would be rejected by the setter, including on
        # the path that turns the preference OFF - leaving a profile pinned to
        # a band with no control able to clear it.
        if merged.get("band_preference") not in ("a", "bg"):
            merged["band_preference"] = "a"
        # These lists are handed to nmcli as root. argv rather than a shell,
        # so junk is not dangerous, but a list of it turns a reset into a
        # long series of doomed calls on the event loop.
        for key in ("last_connection_uuid", "bssid_lock_connection_uuid"):
            value = merged.get(key, "")
            if value and not UUID_RE.match(value):
                merged[key] = ""
        for key in ("bssid_lock_uuids", "band_preference_uuids"):
            merged[key] = [
                u for u in merged.get(key, [])
                if isinstance(u, str) and UUID_RE.match(u)
            ]
        return merged
    except Exception:
        return dict(DEFAULT_SETTINGS)


def _save_settings(data: dict):
    directory = os.path.dirname(SETTINGS_FILE)
    os.makedirs(directory, exist_ok=True)
    # Write to a temp file then rename, so a crash cannot leave a half-written
    # settings file. The temp name is unique per call rather than fixed: with a
    # shared name, two writers race and the loser's os.replace finds its own
    # file already renamed away. Every caller is on the event loop today, but
    # that is not something a future change should have to know.
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, SETTINGS_FILE)
    except Exception:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _save_settings_with_timestamp(data: dict):
    """Save settings and update last_applied timestamp in one write."""
    data["last_applied"] = int(time.time())
    _save_settings(data)


class Plugin:
    """Root plugin instance. Decky exposes every async method here as a
    callable from the frontend. Synchronous helpers prefixed with `_` are
    for internal use only."""

    # ---- Helpers ----

    def _run_cmd(self, cmd: list[str], timeout: int = 5, clean_env: bool = False) -> dict:
        """Run a subprocess and return a result dict.

        clean_env strips LD_LIBRARY_PATH so children use system libraries
        instead of Decky's PyInstaller-bundled ones. Required for curl
        (OpenSSL mismatch) and bash (readline symbol mismatch); without it,
        those binaries fail with cryptic symbol-lookup errors.
        """
        try:
            env = None
            if clean_env:
                env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env
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
        result = self._run_cmd(cmd, timeout=5, clean_env=True)
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

        Checks config files in priority order: our own generic conf, Bazzite's
        iwd conf, SteamOS override, SteamOS defaults. Falls back to checking
        which systemd service is active.
        """
        for path in (
            GENERIC_BACKEND_CONF,
            BAZZITE_IWD_CONF,
            WIFI_BACKEND_CONF,
            NM_DEFAULT_CONF,
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

    def _apply_driver_fixes(self, enable: bool):
        """Apply or revert driver-specific power save fixes from DRIVER_PROFILES.
        Silently no-ops for drivers with no sysfs paths or modprobe options."""
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
        if enable and options:
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

    def _apply_pcie_aspm_fix(self, enable: bool):
        """Disable or restore PCIe ASPM for the WiFi device.
        Prevents throughput degradation during sustained streaming.
        Works on all PCIe-attached WiFi adapters."""
        try:
            # Discover WiFi PCI device path dynamically
            iface = self._get_wifi_interface()
            if not iface or not IFACE_RE.match(iface):
                return
            device_link = os.path.realpath(f"/sys/class/net/{iface}/device")
            if not os.path.isdir(device_link):
                return

            # Disable/restore PCIe ASPM L-states
            link_dir = os.path.join(device_link, "link")
            if os.path.isdir(link_dir):
                val = "0" if enable else "1"
                for aspm_file in ["l0s_aspm", "l1_aspm", "l1_1_aspm", "l1_2_aspm",
                                   "l1_1_pcipm", "l1_2_pcipm"]:
                    path = os.path.join(link_dir, aspm_file)
                    try:
                        with open(path, "w") as f:
                            f.write(val)
                    except (FileNotFoundError, PermissionError):
                        pass

            # Disable/restore PCI runtime power management
            power_control = os.path.join(device_link, "power", "control")
            try:
                with open(power_control, "w") as f:
                    f.write("on" if enable else "auto")
            except (FileNotFoundError, PermissionError):
                pass

            if enable:
                decky.logger.info(f"PCIe ASPM disabled for {device_link}")
            else:
                decky.logger.info(f"PCIe ASPM restored for {device_link}")
        except Exception as e:
            decky.logger.error(f"PCIe ASPM fix error: {e}")

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
            script = script.replace("__SETTINGS_PATH__", shlex.quote(SETTINGS_FILE))
            script = script.replace(
                "__PLUGIN_DIR__", shlex.quote(decky.DECKY_PLUGIN_DIR)
            )
            with open(DISPATCHER_PATH, "w") as f:
                f.write(script)
            os.chmod(DISPATCHER_PATH, 0o755)
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
            if not log_dir or not os.path.isdir(log_dir):
                return
            files = [
                os.path.join(log_dir, f)
                for f in os.listdir(log_dir)
                if f.endswith(".log")
            ]
            if len(files) <= keep:
                return
            files.sort(key=os.path.getmtime, reverse=True)
            current_log = getattr(decky, "DECKY_PLUGIN_LOG", None)
            removed = 0
            for path in files[keep:]:
                # Paranoia: never delete the file we're currently writing to.
                if current_log and os.path.realpath(path) == os.path.realpath(current_log):
                    continue
                try:
                    os.remove(path)
                    removed += 1
                except Exception:
                    pass
            if removed:
                decky.logger.info(f"Rotated logs: removed {removed} old file(s), kept {keep} newest")
        except Exception as e:
            decky.logger.error(f"Log rotation error: {e}")

    # ---- Lifecycle ----

    async def _main(self):
        try:
            decky.logger.info("WiFi Optimizer starting")
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
            if settings.get("auto_fix_on_wake", True):
                if not self._install_dispatcher():
                    # Don't leave the toggle reading on with nothing installed.
                    # There is no drift indicator for auto-fix, so a silent
                    # failure here is invisible until WiFi stops being fixed.
                    # Decided before the save below on purpose: correcting it
                    # afterwards is discarded, and the volatile-apply block
                    # then re-reads the stale value and writes it back.
                    settings["auto_fix_on_wake"] = False
                    decky.logger.error(
                        "Auto-fix disabled: the dispatcher could not be installed"
                    )

            _save_settings(settings)

            # Apply volatile settings that may have been lost on reboot.
            # The dispatcher handles reconnects, but on a fresh boot WiFi
            # connects before the plugin starts, so we apply here too.
            # Order: buffer tuning first (sets txqueuelen), then CAKE
            # (overrides txqueuelen to 256), then power_save last (sticks
            # after any reconnects the dispatcher might trigger).
            iface = self._get_wifi_interface()
            if iface:
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

    async def _unload(self):
        try:
            decky.logger.info("WiFi Optimizer unloading")
            task = getattr(self, "_backend_switch_task", None)
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
            for key, value in SYSCTL_DEFAULTS.items():
                self._run_cmd(["/usr/bin/sysctl", "-w", f"{key}={value}"])
            iface = self._get_wifi_interface()
            if iface:
                self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", "1000"])
                self._run_cmd(["/usr/bin/tc", "qdisc", "del", "dev", iface, "root"])
            for path in [NM_CONF_PATH, MODPROBE_CONF_PATH, GENERIC_BACKEND_CONF,
                         SETTINGS_FILE, ENFORCED_FILE]:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
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
            driver_path = os.path.realpath(f"/sys/class/net/{iface}/device/driver/module")
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

    async def get_diagnostic_info(self) -> dict:
        """Collect system info for remote debugging. Sanitized (no passwords)."""
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
            return {
                "success": True,
                "device_info": info,
                "wifi_interface": iface,
                "iw_dev": iw_dev.get("stdout", ""),
                "iw_reg": iw_reg.get("stdout", ""),
                "kernel": uname.get("stdout", "").strip(),
                "os_release": os_release,
                "distro_id": distro["id"],
                "distro_name": distro["name"],
                "support_tier": self._get_support_tier(),
            }
        except Exception as e:
            decky.logger.error(f"get_diagnostic_info error: {e}")
            return {"success": False, "error": str(e)}

    async def save_diagnostic_info(self) -> dict:
        """Write diagnostics to a file in the settings directory as a
        fallback when clipboard is unavailable."""
        try:
            info = await self.get_diagnostic_info()
            diag_path = os.path.join(
                os.path.dirname(SETTINGS_FILE), "diagnostics.json"
            )
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
                return cached
            # A collection that never returns would otherwise hold this
            # branch forever and leave the panel blank with nothing to say.
            # subprocess.run waits again after killing a timed-out child, so
            # a process stuck against a wedged driver can do exactly that.
            started = getattr(self, "_collect_started_at", 0.0)
            if started and (time.monotonic() - started) > 90:
                decky.logger.error(
                    "Status collection has not returned in 90s; reporting a "
                    "read failure rather than leaving the panel empty"
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
                }

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
            }

        self._collect_depth = getattr(self, "_collect_depth", 0) + 1
        if self._collect_depth == 1:
            self._collect_started_at = time.monotonic()
        try:
            status, pending, actions = await asyncio.to_thread(self._collect_status)
        finally:
            self._collect_depth = max(0, getattr(self, "_collect_depth", 1) - 1)
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
            except Exception as e:
                decky.logger.error(f"settings refresh error: {e}")

        self._last_status = status
        return status

    def _apply_status_actions(self, status: dict, pending: dict, actions: list):
        """Carry out what _collect_status proposed, on the event loop.

        Every guard is re-checked against a FRESH read of settings rather than
        the worker thread's snapshot, which may be up to ~20s old. Without
        that, a poll begun before the user touched a toggle would undo the
        change they just made.

        This deliberately runs ON the event loop rather than in a thread.
        Every setter but one is an await-free coroutine, so the loop is what
        serializes them against this. The exception is set_band_preference,
        which sleeps mid-way and is covered separately by the band-change
        gate. Moving this to a thread would make it concurrent with all of
        them, which a lock could still handle - but that is an eight-setter
        refactor, not a property of the current arrangement. The cost is
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
            uuid = action["uuid"]

            if kind == "bssid_repoint" and band_conflict(action):
                continue

            if kind == "priority":
                if settings.get("priority_set"):
                    continue
                if self._reassert_exhausted("priority"):
                    continue
                bumped = self._nmcli_modify(
                    uuid, "connection.autoconnect-priority", "100", timeout=2
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

            elif kind == "ipv6":
                if not settings.get("ipv6_disabled"):
                    status["drift"].pop("ipv6", None)
                    continue
                if self._reassert_exhausted("ipv6"):
                    continue
                healed = self._nmcli_modify(uuid, "ipv6.method", "disabled", timeout=2)
                self._record_reassert("ipv6", healed["success"])
                self._log_throttled(
                    "ipv6",
                    f"IPv6 drifted to {action['observed']!r} on {uuid}, "
                    f"reasserting disabled: "
                    f"{'ok' if healed['success'] else 'failed'}",
                )

            elif kind == "band":
                if not settings.get("band_preference_enabled"):
                    status["drift"].pop("band_preference", None)
                    continue
                if settings.get("band_preference") != action["value"]:
                    status["drift"].pop("band_preference", None)
                    continue
                if self._reassert_exhausted("band"):
                    continue
                healed = self._nmcli_modify(
                    uuid, "802-11-wireless.band", action["value"], timeout=2
                )
                self._record_reassert("band", healed["success"])
                if healed["success"]:
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

            elif kind == "cake":
                if not settings.get("cake_enabled"):
                    status["drift"].pop("cake", None)
                    continue
                # This runs on the event loop, and a kernel without sch_cake
                # would otherwise retry three subprocesses every three seconds
                # forever, blocking every other call for as long as it takes.
                # Give up after a few consecutive failures and leave the drift
                # flag standing, which is the honest report anyway.
                if self._reassert_exhausted("cake"):
                    continue
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

            elif kind == "pin_cleanup":
                list_key = action["list_key"]
                prop = action["property"]
                enabled_key = next(
                    e for e, l, _ in self._PIN_PROPERTIES if l == list_key
                )
                if settings.get(enabled_key):
                    continue
                if self._reassert_exhausted(f"pin_cleanup:{list_key}"):
                    continue
                done = self._nmcli_modify(uuid, prop, "", timeout=2)
                detail = done.get("stderr", "")
                # A profile that no longer exists is resolved, not failed.
                # Counting it as a failure pushed cleanup into its cooldown
                # while it was in fact draining the list.
                resolved = done["success"] or "unknown connection" in detail.lower()
                self._record_reassert(f"pin_cleanup:{list_key}", resolved)
                if resolved:
                    pending[list_key] = [
                        u for u in settings.get(list_key, []) if u != uuid
                    ]
                    self._log_throttled(
                        f"pin_cleanup:{list_key}",
                        f"Cleared a leftover {prop} from {uuid}",
                    )
                else:
                    # Move it to the back. Only the head is attempted each
                    # poll, so one profile that cannot be cleared would
                    # otherwise keep every other entry from ever being tried.
                    rest = [u for u in settings.get(list_key, []) if u != uuid]
                    if rest:
                        pending[list_key] = rest + [uuid]

            elif kind == "bssid_repoint":
                # The lock may have been switched off while this poll was in
                # flight. Re-pointing then would restore a BSSID the user just
                # cleared, leaving the profile pinned with the toggle showing
                # off and nothing left to clear it.
                if not settings.get("bssid_lock_enabled"):
                    # The user turned the lock off while this poll was in
                    # flight. Nothing has drifted; leaving the flag set shows
                    # a drift warning for a setting that is no longer on.
                    status["drift"].pop("bssid_lock", None)
                    continue
                # A band change clears the BSSID on purpose so NM can find an
                # AP on the other band, and only re-locks once it associates.
                # Writing the old BSSID back mid-flight can leave the profile
                # demanding a band and an AP that cannot both be satisfied,
                # which stops it associating at all.
                if self._band_change_in_flight():
                    continue
                if self._reassert_exhausted("bssid_repoint"):
                    continue
                retarget = self._nmcli_modify(
                    uuid, "802-11-wireless.bssid", action["value"], timeout=2
                )
                self._record_reassert("bssid_repoint", retarget["success"])
                if retarget["success"]:
                    pending["bssid_lock_value"] = action["value"]
                    pending["bssid_lock_connection_uuid"] = uuid
                    known = list(settings.get("bssid_lock_uuids", []))
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

        if pending:
            settings.update(pending)
            _save_settings(settings)

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

    _PIN_PROPERTIES = (
        ("bssid_lock_enabled", "bssid_lock_uuids", "802-11-wireless.bssid"),
        ("band_preference_enabled", "band_preference_uuids", "802-11-wireless.band"),
    )

    def _propose_pin_cleanup(self, settings: dict) -> list[dict]:
        """One cleanup per property per poll for anything we could not unpin.

        Both properties need this, not just the address: a profile left
        demanding a band its network does not offer fails to associate the
        same way, and once the preference is off nothing else would look at
        it again.
        """
        out = []
        for enabled_key, list_key, prop in self._PIN_PROPERTIES:
            if settings.get(enabled_key):
                continue
            for stale_uuid in settings.get(list_key, [])[:1]:
                out.append({
                    "kind": "pin_cleanup", "uuid": stale_uuid,
                    "property": prop, "list_key": list_key,
                })
        return out

    def _band_is_reachable(self, iface: str, uuid: str, want: str) -> bool:
        """Whether this network has a visible AP on the wanted band.

        Checked before cycling the radio, because a cycle that cannot get
        back leaves the user disconnected - worse than the state the cycle
        was meant to improve.
        """
        ssid = self._get_profile_ssid(uuid)
        if not ssid:
            return False
        scan = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "SSID,FREQ", "dev", "wifi", "list",
             "ifname", iface],
            timeout=5,
        )
        if not scan["success"]:
            return False
        for line in scan.get("stdout", "").split("\n"):
            name, _, freq = line.rpartition(":")
            # Unescape BOTH sides. _get_profile_ssid returns nmcli's terse
            # value with its escapes intact, so comparing it against an
            # unescaped scan result fails for any SSID containing : or \.
            if name.replace("\\", "") != ssid.replace("\\", ""):
                continue
            found = re.match(r"\s*(\d+)", freq.strip())
            if not found:
                continue
            if (int(found.group(1)) >= 5000) == (want == "a"):
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
        # cannot read must refuse rather than guess.
        found = re.match(r"\s*(\d+)", str(frequency or ""))
        if not found:
            return True
        # NetworkManager's band property has no 6 GHz value; a 6 GHz
        # association is satisfied by "a", so anything at or above 5 GHz
        # counts as the 5 GHz band here.
        on_5ghz = int(found.group(1)) >= 5000
        return on_5ghz != (settings.get("band_preference") == "a")

    def _band_change_in_flight(self) -> bool:
        # The count is released in a finally, including on cancellation, and a
        # process that dies mid-change takes the whole instance with it - so
        # there is nothing a deadline here could catch that this does not.
        return getattr(self, "_band_change_depth", 0) > 0

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
            }

            # Backend info is system-wide; populate regardless of connection state
            # Uses the memoised probe result only. Probing here would both
            # fork from the worker thread and write to the instance, which
            # this function promises not to do; _main warms it instead.
            backend_available = (
                getattr(self, "_steamos_manager_available", False)
                or self._get_backend_method_cached()
            )
            status["live"]["backend_tool_available"] = backend_available
            if backend_available:
                status["live"]["wifi_backend"] = self._get_current_backend() or ""

            # Leftover pins, proposed BEFORE the not-connected return. A pin
            # we failed to clear is what stops a profile associating, so the
            # user is most likely to be DISCONNECTED when it needs clearing -
            # and this needs neither an interface nor an active connection,
            # only a saved profile.
            actions.extend(self._propose_pin_cleanup(settings))

            if not connected:
                status["live"]["dispatcher_installed"] = os.path.isfile(
                    DISPATCHER_PATH
                )
                return status, pending, actions

            # Remember UUID and ensure high autoconnect-priority so NM
            # prefers this profile over duplicates on boot (fixes 2.4GHz issue)
            if uuid and uuid != settings.get("last_connection_uuid"):
                settings["last_connection_uuid"] = uuid
                settings["priority_set"] = False
                pending["last_connection_uuid"] = uuid
                pending["priority_set"] = False

            if uuid and not settings.get("priority_set"):
                # Bump priority to favor this profile over duplicates on boot.
                actions.append({"kind": "priority", "uuid": uuid})

            # Resolved lazily by the blocks below that need it; declared here
            # so every path has it, and None keeps meaning "not established".
            active_ssid: str | None = None

            # Power save
            ps_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "get", "power_save"], timeout=T
            )
            ps_off = "Power save: off" in ps_result.get("stdout", "")
            status["live"]["power_save_off"] = ps_off
            if settings.get("power_save_disabled") and not ps_off:
                status["drift"]["power_save"] = True

            # Link info
            link_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "link"], timeout=T
            )
            link_out = link_result.get("stdout", "")
            for line in link_out.split("\n"):
                line = line.strip()
                if line.startswith("signal:"):
                    status["live"]["signal_dbm"] = line.split(":", 1)[1].strip()
                elif "tx bitrate:" in line:
                    status["live"]["tx_bitrate"] = line.split("tx bitrate:", 1)[
                        1
                    ].strip()
                elif line.startswith("freq:"):
                    status["live"]["frequency"] = line.split(":", 1)[1].strip()
                elif "Connected to" in line:
                    parts = line.split()
                    if len(parts) >= 3:
                        status["live"]["connected_bssid"] = parts[2]

            # Channel info - parse to "36 (80 MHz)" format
            info_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "info"], timeout=T
            )
            for line in info_result.get("stdout", "").split("\n"):
                line = line.strip()
                if line.startswith("channel"):
                    # Raw: "channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz"
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
                        status["live"]["channel"] = f"{chan_num} ({width})"
                    elif chan_num:
                        status["live"]["channel"] = chan_num
                    else:
                        status["live"]["channel"] = line

            # BSSID lock
            bssid_result = self._run_cmd(
                [
                    "/usr/bin/nmcli",
                    "-t",
                    "-f",
                    "802-11-wireless.bssid",
                    "con",
                    "show",
                    "uuid",
                    uuid,
                ],
                timeout=T,
            )
            bssid_out = bssid_result.get("stdout", "")
            current_bssid_lock = ""
            if ":" in bssid_out:
                # Format: 802-11-wireless.bssid:AA\:BB\:CC\:DD\:EE\:FF
                parts = bssid_out.split(":", 1)
                if len(parts) == 2:
                    current_bssid_lock = parts[1].replace("\\", "").strip()
            status["live"]["bssid_lock"] = current_bssid_lock
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
                # Only follow the connection within the SAME network. Without
                # this, leaving home with the lock still enabled would pin the
                # user to the first AP of whatever network they joined next,
                # which they never asked for and which blocks roaming on it.
                # Resolved once, here, because two things need it: deciding
                # whether this is the locked network, and telling the applier
                # which network the pin would land on so it can honour the
                # band preference's scope. None means "could not tell", and
                # every consumer treats that as a reason to hold off.
                active_ssid = self._get_profile_ssid(uuid, timeout=T)
                locked_uuid = settings.get("bssid_lock_connection_uuid", "")
                same_network = False
                if locked_uuid == uuid:
                    same_network = True
                elif locked_uuid:
                    locked_ssid = self._get_profile_ssid(locked_uuid, timeout=T)
                    same_network = bool(
                        locked_ssid and active_ssid and locked_ssid == active_ssid
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
                            "active_ssid": active_ssid,
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

            # IP address
            ip_result = self._run_cmd(
                ["/usr/bin/nmcli", "-t", "-f", "IP4.ADDRESS", "dev", "show", iface],
                timeout=T,
            )
            ip_out = ip_result.get("stdout", "")
            # Format: IP4.ADDRESS[1]:192.168.1.100/24
            if ":" in ip_out:
                ip_addr = ip_out.split(":", 1)[1].split("/")[0].strip()
                status["live"]["ip_address"] = ip_addr

            # DNS
            dns_result = self._run_cmd(
                ["/usr/bin/nmcli", "-t", "-f", "IP4.DNS", "dev", "show", iface],
                timeout=T,
            )
            status["live"]["dns"] = dns_result.get("stdout", "")

            # IPv6
            ipv6_result = self._run_cmd(
                [
                    "/usr/bin/nmcli",
                    "-t",
                    "-f",
                    "ipv6.method",
                    "con",
                    "show",
                    "uuid",
                    uuid,
                ],
                timeout=T,
            )
            ipv6_out = ipv6_result.get("stdout", "")
            live_ipv6 = ipv6_out.split(":", 1)[1].strip() if ":" in ipv6_out else ""
            status["live"]["ipv6_method"] = live_ipv6
            if settings.get("ipv6_disabled") and live_ipv6 != "disabled":
                status["drift"]["ipv6"] = True
                actions.append({
                    "kind": "ipv6", "uuid": uuid, "observed": live_ipv6,
                })

            # Band preference
            band_result = self._run_cmd(
                [
                    "/usr/bin/nmcli",
                    "-t",
                    "-f",
                    "802-11-wireless.band",
                    "con",
                    "show",
                    "uuid",
                    uuid,
                ],
                timeout=T,
            )
            band_out = band_result.get("stdout", "")
            live_band = band_out.split(":", 1)[1].strip() if ":" in band_out else ""
            status["live"]["band"] = live_band
            expected_band = settings.get("band_preference", "a")
            # Only on the network the preference was set on. Enforcing it
            # everywhere writes a band into profiles for networks that may not
            # offer it, which then stop connecting - and the panel gives no
            # sign it is happening. The lock above is scoped the same way.
            band_ssid = settings.get("band_preference_ssid", "")
            # Same rule as the conflict test: without a recorded network the
            # preference is not enforced anywhere.
            band_scope_ok = bool(band_ssid)
            if settings.get("band_preference_enabled") and band_ssid:
                # active_ssid was resolved with the BSSID lock above. Reading
                # it again would be a second subprocess for the same answer.
                if active_ssid is None:
                    active_ssid = self._get_profile_ssid(uuid, timeout=T)
                band_scope_ok = active_ssid == band_ssid
            if (
                settings.get("band_preference_enabled")
                and band_scope_ok
                and live_band != expected_band
            ):
                status["drift"]["band_preference"] = True
                actions.append({
                    "kind": "band", "uuid": uuid, "value": expected_band,
                    "observed": live_band,
                })

            # Buffer tuning
            sysctl_result = self._run_cmd(
                ["/usr/bin/sysctl", "-n", "net.core.rmem_max"], timeout=T
            )
            current_rmem = sysctl_result.get("stdout", "").strip()
            status["live"]["buffer_tuning_applied"] = current_rmem == "16777216"
            if settings.get("buffer_tuning_enabled") and current_rmem != "16777216":
                status["drift"]["buffer_tuning"] = True

            # CAKE QoS
            cake_active = self._get_cake_status(iface)
            status["live"]["cake_applied"] = cake_active
            if settings.get("cake_enabled") and not cake_active:
                status["drift"]["cake"] = True
                # Nothing else clears this. Optimize Safe covers the safe tier
                # only, so without a reassertion here the drift warning stays
                # up forever with no control that resolves it. The dispatcher
                # already reapplies CAKE on every reconnect, so doing it here
                # matches behaviour the user has already opted into.
                actions.append({"kind": "cake", "uuid": uuid, "iface": iface})

            # Dispatcher
            status["live"]["dispatcher_installed"] = os.path.isfile(DISPATCHER_PATH)

            # Last enforced by dispatcher
            try:
                with open(ENFORCED_FILE, "r") as f:
                    status["live"]["last_enforced"] = int(f.read().strip())
            except Exception:
                status["live"]["last_enforced"] = 0

            return status, pending, actions
        except Exception as e:
            decky.logger.error(f"get_status error: {e}")
            # Hand back a COMPLETE status shape. The panel reads settings,
            # live, drift, connected and version unconditionally, so a bare
            # error dict renders as "not connected" with every toggle off -
            # telling the user their optimizations are gone rather than that
            # the status read failed.
            try:
                settings = _load_settings()
            except Exception:
                settings = dict(DEFAULT_SETTINGS)
            return (
                {
                    "success": False,
                    "error": "unexpected",
                    "message": "Couldn't read WiFi status.",
                    "connected": False,
                    # Reads settings only, so it is still meaningful here -
                    # hardcoding 3 made the panel announce unrecognised
                    # hardware because an unrelated read failed.
                    "support_tier": self._get_support_tier(),
                    "version": getattr(decky, "DECKY_PLUGIN_VERSION", "?"),
                    "settings": settings,
                    "live": {},
                    "drift": {},
                },
                {},
                [],
            )

    # ---- Optimization setters ----

    async def set_power_save(self, disabled: bool) -> dict:
        try:

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
            if disabled:
                os.makedirs(os.path.dirname(NM_CONF_PATH), exist_ok=True)
                with open(NM_CONF_PATH, "w") as f:
                    f.write("[connection]\nwifi.powersave = 2\n")
            else:
                try:
                    os.remove(NM_CONF_PATH)
                except FileNotFoundError:
                    pass

            self._apply_driver_fixes(disabled)
            self._apply_pcie_aspm_fix(disabled)

            # Save settings only after success
            settings = _load_settings()
            settings["power_save_disabled"] = disabled
            _save_settings_with_timestamp(settings)

            return {"success": True, "power_save_off": disabled}
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
            settings["auto_fix_on_wake"] = enabled and installed
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

    async def set_bssid_lock(self, enabled: bool) -> dict:
        try:

            if enabled:
                # Enabling requires active WiFi to read current BSSID
                iface, uuid, err = self._require_wifi()
                if err:
                    return err

                link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
                link_out = link_result.get("stdout", "")
                bssid = ""
                for line in link_out.split("\n"):
                    if "Connected to" in line:
                        parts = line.split()
                        if len(parts) >= 3:
                            bssid = parts[2]
                        break

                if not bssid:
                    return {
                        "success": False,
                        "error": "no_wifi",
                        "message": "Could not determine current BSSID",
                    }

                # Refuse rather than write a profile that cannot associate.
                # This is reachable from the drift banner's Fix now, which
                # runs Optimize Safe, and it reconnects immediately after -
                # so getting it wrong strands the user then and there rather
                # than at their next wake.
                frequency = ""
                for line in link_out.split("\n"):
                    line = line.strip()
                    if line.startswith("freq:"):
                        frequency = line.split(":", 1)[1].strip()
                        break
                current = _load_settings()
                active_ssid = self._get_profile_ssid(uuid)
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
                        other = "5 GHz" if want == "a" else "2.4 GHz"
                        return {
                            "success": False,
                            "error": "nmcli_failed",
                            "message": (
                                f"No {other} access point is in range for this "
                                f"network, which it is set to require. Turn the "
                                f"band preference off to lock anyway."
                            ),
                        }
                    decky.logger.info(
                        "Band conflict before locking; reconnecting to let NM "
                        "pick an access point on the preferred band"
                    )
                    if not self._hard_reconnect(uuid):
                        return dict(self._RADIO_OFF_RESULT)
                    # A BLOCKING sleep, on purpose, and it must stay one.
                    # This setter's whole body is await-free, which is what
                    # lets the event loop serialize it against status
                    # reconciliation. Turning this into asyncio.sleep would
                    # add a suspension point and let a poll write to the
                    # profile mid-change - the race the reader/applier split
                    # exists to prevent. _hard_reconnect immediately above
                    # already blocks for longer.
                    time.sleep(3)
                    iface = self._get_wifi_interface() or iface
                    link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
                    link_out = link_result.get("stdout", "")
                    frequency = ""
                    bssid = ""
                    for line in link_out.split("\n"):
                        line = line.strip()
                        if line.startswith("freq:"):
                            frequency = line.split(":", 1)[1].strip()
                        elif "Connected to" in line:
                            parts = line.split()
                            if len(parts) >= 3:
                                bssid = parts[2]

                    if not bssid or self._band_conflicts(
                        current, frequency, active_ssid
                    ):
                        want = current.get("band_preference")
                        other = "5 GHz" if want == "a" else "2.4 GHz"
                        return {
                            "success": False,
                            "error": "nmcli_failed",
                            "message": (
                                f"Couldn't reach an access point on {other}, "
                                f"which this network is set to require. Turn "
                                f"the band preference off to lock anyway."
                            ),
                        }

                result = self._nmcli_modify(uuid, "802-11-wireless.bssid", bssid)
                if not result["success"]:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't lock BSSID",
                        "detail": result["stderr"],
                    }

                settings = _load_settings()
                settings["bssid_lock_enabled"] = True
                settings["bssid_lock_value"] = bssid
                settings["bssid_lock_connection_uuid"] = uuid
                known = list(settings.get("bssid_lock_uuids", []))
                if uuid not in known:
                    known.append(uuid)
                settings["bssid_lock_uuids"] = known[
                    -self._MAX_TRACKED_LOCK_UUIDS:
                ]
                _save_settings_with_timestamp(settings)
                if not self._hard_reconnect(uuid):
                    return dict(self._RADIO_OFF_RESULT)
            else:
                # Disabling works on saved profiles - no active WiFi needed
                iface, uuid, _ = self._require_wifi()
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

            return {"success": True, "bssid_locked": enabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_bssid_lock error: {e}")
            return self._unexpected_response(e)

    async def set_band_preference(self, enabled: bool, band: str = "a") -> dict:
        try:
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
            self._band_change_depth = getattr(self, "_band_change_depth", 0) + 1

            if band not in ("a", "bg"):
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": f"Invalid band '{band}'. Must be 'a' (5 GHz) or 'bg' (2.4 GHz).",
                }

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
            had_bssid_lock = settings.get("bssid_lock_enabled", False)
            if enabled and had_bssid_lock:
                unlocked = self._nmcli_modify(uuid, "802-11-wireless.bssid", "")
                if not unlocked["success"]:
                    # The band is already written. Leaving an address from the
                    # other band beside it gives a profile no access point
                    # satisfies, and the reconnect below would activate it.
                    # Put the band back and report, rather than strand it.
                    rollback = self._nmcli_modify(
                        uuid, "802-11-wireless.band",
                        settings.get("band_preference", "") if
                        settings.get("band_preference_enabled") else "",
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

            # Re-lock BSSID to whatever AP NM picked on the new band
            if enabled and had_bssid_lock:
                # Give NM time to associate on the new band before reading the
                # BSSID back. Must not be time.sleep here: this is an async
                # method, and a blocking sleep stalls the whole event loop.
                await asyncio.sleep(3)
                iface = self._get_wifi_interface()
                # The reconnect may have landed on a different profile - a
                # duplicate for the same network, which is the situation this
                # plugin exists to handle. Pinning what we see onto the profile
                # we meant to change would write a foreign address, possibly on
                # the other band, into a profile that now demands this one.
                landed_on = self._get_active_connection_uuid()
                if iface and landed_on == uuid:
                    link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
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

            return {"success": True, "band": value, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_band_preference error: {e}")
            return self._unexpected_response(e)
        finally:
            self._band_change_depth = max(
                0, getattr(self, "_band_change_depth", 1) - 1
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
                    servers = custom_servers.strip()
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
                # own, so it reconnects with no resolvers at all - and there
                # is no drift key for DNS, so nothing would report it.
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
            _save_settings_with_timestamp(settings)

            if not self._hard_reconnect(uuid):
                return dict(self._RADIO_OFF_RESULT)
            return {"success": True, "ipv6_disabled": disabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_ipv6 error: {e}")
            return self._unexpected_response(e)

    async def set_buffer_tuning(self, enabled: bool) -> dict:
        try:

            params = SYSCTL_PARAMS if enabled else SYSCTL_DEFAULTS
            failed = 0
            for key, value in params.items():
                result = self._run_cmd(
                    ["/usr/bin/sysctl", "-w", f"{key}={value}"]
                )
                if not result["success"]:
                    failed += 1
                    decky.logger.error(f"sysctl {key}={value} failed: {result['stderr']}")

            if failed == len(params) and not enabled:
                decky.logger.error(
                    "Could not restore any kernel buffer defaults; recording "
                    "the setting as off anyway so the toggle is not stuck"
                )
            if enabled and failed == len(params):
                # Reporting success here recorded the setting as on, which then
                # showed as drift on every poll with no control able to clear
                # it: Optimize Safe just calls back into here and fails again.
                # Only on the enable path. Refusing to disable would leave
                # the setting recorded as on with a toggle that will not move,
                # which is worse than recording the user's intent and logging
                # that the restore did not take.
                return {
                    "success": False,
                    "error": "unexpected",
                    "message": "Couldn't apply network buffer settings.",
                }

            # TX queue length (CAKE needs 256; defer to it if active)
            iface = self._get_wifi_interface()
            settings = _load_settings()
            if iface:
                if settings.get("cake_enabled"):
                    txq = "256"
                else:
                    txq = "2000" if enabled else "1000"
                self._run_cmd(
                    ["/usr/bin/ip", "link", "set", iface, "txqueuelen", txq]
                )

            settings["buffer_tuning_enabled"] = enabled
            _save_settings_with_timestamp(settings)
            return {"success": True, "buffer_tuning": enabled}
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
                # Restore txqueuelen based on whether buffer tuning is active
                settings = _load_settings()
                txq = "2000" if settings.get("buffer_tuning_enabled") else "1000"
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

    async def reapply_volatile(self) -> dict:
        """Reapply volatile (non-reconnecting) settings. Safe to call mid-stream."""
        try:
            settings = _load_settings()
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
            if settings.get("bssid_lock_enabled"):
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
                # access point on that band. A deliberate toggle still
                # re-scopes; this path is programmatic.
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

            if settings.get("dns_enabled"):
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

            if settings.get("ipv6_disabled"):
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

    async def reset_settings(self) -> dict:
        """Delete settings and revert to defaults."""
        try:
            # Before the settings naming them are discarded.
            self._clear_profile_pins()
            # Revert runtime state
            self._apply_driver_fixes(False)
            self._apply_pcie_aspm_fix(False)
            for key, value in SYSCTL_DEFAULTS.items():
                self._run_cmd(["/usr/bin/sysctl", "-w", f"{key}={value}"])
            iface = self._get_wifi_interface()
            if iface:
                self._run_cmd(["/usr/bin/ip", "link", "set", iface, "txqueuelen", "1000"])
                self._run_cmd(["/usr/bin/tc", "qdisc", "del", "dev", iface, "root"])
            try:
                os.remove(NM_CONF_PATH)
            except FileNotFoundError:
                pass
            try:
                os.remove(MODPROBE_CONF_PATH)
            except FileNotFoundError:
                pass
            try:
                os.remove(SETTINGS_FILE)
            except FileNotFoundError:
                pass
            try:
                os.remove(ENFORCED_FILE)
            except FileNotFoundError:
                pass
            try:
                os.remove(GENERIC_BACKEND_CONF)
            except FileNotFoundError:
                pass

            # Repopulate model/driver so the plugin doesn't show as "UNKNOWN /
            # Unsupported device" until the next plugin reload. Mirrors the
            # hardware detection _main does on startup.
            info = await self.get_device_info()
            fresh = dict(DEFAULT_SETTINGS)
            fresh["model"] = info.get("model", "unknown")
            fresh["driver"] = info.get("driver", "unknown")
            fresh["device_family"] = info.get("device_family", "unknown")
            fresh["device_label"] = info.get("device_label", "Unknown Device")
            fresh["chip_label"] = info.get("chip_label", "unknown")
            fresh["supports_6ghz"] = info.get("supports_6ghz", False)
            distro = self._detect_distro()
            fresh["distro_id"] = distro["id"]
            fresh["distro_name"] = distro["name"]
            _save_settings(fresh)

            decky.logger.info("Settings reset to defaults")
            return {"success": True, "message": "Settings reset to defaults"}
        except Exception as e:
            decky.logger.error(f"reset_settings error: {e}")
            return self._unexpected_response(e)

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

    async def check_for_update(self) -> dict:
        """Check GitHub for a newer version (stable release or beta branch)."""
        try:
            current = decky.DECKY_PLUGIN_VERSION
            settings = _load_settings()
            channel = settings.get("update_channel", "stable")
            decky.logger.info(f"Update check: current={current}, channel={channel}")

            if channel == "beta":
                result = await asyncio.to_thread(
                    self._run_cmd,
                    [
                        "/usr/bin/curl", "-sL", "--connect-timeout", "3", "--max-time", "10",
                        "-H", "Accept: application/vnd.github.raw+json",
                        "https://api.github.com/repos/ArcadaLabs-Jason/WifiOptimizer/contents/package.json?ref=beta",
                    ],
                    15,
                    True,
                )
            else:
                result = await asyncio.to_thread(
                    self._run_cmd,
                    [
                        "/usr/bin/curl", "-sL", "--connect-timeout", "3", "--max-time", "10",
                        "-H", "Accept: application/vnd.github.v3+json",
                        "https://api.github.com/repos/ArcadaLabs-Jason/WifiOptimizer/releases/latest",
                    ],
                    15,
                    True,
                )

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
                latest = data.get("version", "")
            else:
                tag = data.get("tag_name", "")
                latest = tag.lstrip("v")

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
                msg = data.get("message", "couldn't parse version")
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

            decky.logger.info(f"Update check: current={current}, latest={latest}, channel={channel}, update={update_available}")

            return {
                "success": True,
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

    async def apply_update(self) -> dict:
        """Download and install update from the selected channel, then restart Decky."""
        try:
            info = await self.check_for_update()
            if not info.get("update_available"):
                return {"success": False, "message": "No update available."}

            channel = info.get("channel", "stable")
            latest = info["latest_version"]
            plugin_dir = decky.DECKY_PLUGIN_DIR

            if channel == "beta":
                download_url = "https://github.com/ArcadaLabs-Jason/WifiOptimizer/archive/refs/heads/beta.tar.gz"
                src_dir = "WifiOptimizer-beta"
                label = f"beta v{latest}"
            else:
                tag = f"v{latest}"
                download_url = f"https://github.com/ArcadaLabs-Jason/WifiOptimizer/archive/refs/tags/{tag}.tar.gz"
                src_dir = f"WifiOptimizer-{latest}"
                label = f"v{latest}"

            # Values reaching the script are passed as ARGUMENTS, never
            # interpolated, so nothing derived from the network can be read as
            # shell. PATH is pinned rather than inherited, and every binary is
            # resolved through it so this still works on distros that lay out
            # /usr differently.
            script = """#!/bin/bash
set -u
export PATH=/usr/bin:/bin:/usr/sbin:/sbin

SELF_DIR="$1"
PLUGIN_DIR="$2"
DOWNLOAD_URL="$3"
SRC_NAME="$4"
LABEL="$5"

sleep 2

TMP=$(mktemp -d) || exit 1
cleanup() { rm -rf "$TMP"; rm -rf "$SELF_DIR"; }
trap cleanup EXIT

curl -sL --proto '=https' --tlsv1.2 "$DOWNLOAD_URL" -o "$TMP/update.tar.gz" || {
    logger -t wifi-optimizer "Update failed: download error"
    exit 1
}

# Recorded so a bad or unexpected payload is traceable after the fact.
SUM=$(sha256sum "$TMP/update.tar.gz" 2>/dev/null | cut -d' ' -f1)
logger -t wifi-optimizer "Update payload sha256=$SUM"

tar xzf "$TMP/update.tar.gz" -C "$TMP" --no-same-owner --no-same-permissions || {
    logger -t wifi-optimizer "Update failed: extract error"
    exit 1
}
SRC="$TMP/$SRC_NAME"

if [ ! -f "$SRC/plugin.json" ] || [ ! -f "$SRC/main.py" ] || [ ! -f "$SRC/dist/index.js" ]; then
    logger -t wifi-optimizer "Update failed: payload incomplete"
    exit 1
fi

cp "$SRC/plugin.json" "$PLUGIN_DIR/"
cp "$SRC/package.json" "$PLUGIN_DIR/"
cp "$SRC/main.py" "$PLUGIN_DIR/"
cp "$SRC/decky.pyi" "$PLUGIN_DIR/"
mkdir -p "$PLUGIN_DIR/dist" "$PLUGIN_DIR/defaults"
cp "$SRC/dist/index.js" "$PLUGIN_DIR/dist/"
cp "$SRC/dist/index.js.map" "$PLUGIN_DIR/dist/" 2>/dev/null || true
cp "$SRC/defaults/dispatcher.sh.tmpl" "$PLUGIN_DIR/defaults/"

logger -t wifi-optimizer "Updated to $LABEL, restarting plugin_loader"
systemctl restart plugin_loader 2>/dev/null || true
"""
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

            clean_env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
            subprocess.Popen(
                ["/bin/bash", script_path, script_dir, plugin_dir,
                 download_url, src_dir, label],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=clean_env,
            )

            decky.logger.info(f"Update to {label} initiated (channel={channel})")
            return {"success": True, "message": f"Updating to {label}..."}
        except Exception as e:
            decky.logger.error(f"apply_update error: {e}")
            return self._unexpected_response(e)

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
            # clean_env=True clears LD_LIBRARY_PATH so bash doesn't hit a symbol
            # lookup error against Decky's bundled readline (same class of bug
            # as the curl/OpenSSL conflict).
            self._backend_switch["phase"] = "switching"
            decky.logger.info(
                f"backend switch: calling helper write_config target={target} "
                f"(euid={os.geteuid()}, helper={BACKEND_HELPER})"
            )
            write_result = await asyncio.to_thread(
                self._run_cmd, [BACKEND_HELPER, "write_config", target], 5, True
            )
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
                self._run_cmd, [BACKEND_HELPER, "restart_units", other], 45, True
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
                    timeout=30, clean_env=True,
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

            result = await asyncio.to_thread(self._run_cmd, cmd, 45, True)
            if not result["success"]:
                detail = (result.get("stderr") or result.get("stdout") or "")[:200]
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "message": self._friendly_backend_error(detail),
                    "detail": detail,
                }
                decky.logger.error(f"steamos-manager switch failed: {detail!r}")
                return

            # Recover the interface BEFORE waiting for a reconnection. iwd
            # destroys the netdev when it stops on ath11k, and waiting first
            # means spending the whole timeout against an interface that does
            # not exist yet, then reporting "WiFi didn't reconnect" about a
            # switch that recovered and works.
            self._backend_switch["phase"] = "reconnecting"
            # Let the restart settle before deciding the interface is missing.
            # Checking immediately can catch the moment it is down and add a
            # second station netdev to a phy that was about to get its own.
            await asyncio.sleep(2)
            iface = await asyncio.to_thread(self._get_wifi_interface)
            recovery_performed = False
            if not iface:
                if await asyncio.to_thread(self._recover_wlan0):
                    await asyncio.sleep(2)
                    iface = await asyncio.to_thread(self._get_wifi_interface)
                    recovery_performed = bool(iface)
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

    async def _generic_backend_switch_worker(self, target: str):
        """Backend switch for non-SteamOS systems (Bazzite, CachyOS, etc.).
        Writes NM config directly and manages systemd services."""
        try:
            other = "iwd" if target == "wpa_supplicant" else "wpa_supplicant"

            self._backend_switch["phase"] = "switching"
            decky.logger.info(f"generic backend switch: {other} -> {target}")

            os.makedirs(os.path.dirname(GENERIC_BACKEND_CONF), exist_ok=True)
            if target == "iwd":
                with open(GENERIC_BACKEND_CONF, "w") as f:
                    f.write("[device]\nwifi.backend=iwd\nwifi.iwd.autoconnect=yes\n")
            else:
                with open(GENERIC_BACKEND_CONF, "w") as f:
                    f.write("[device]\nwifi.backend=wpa_supplicant\n")

            # Stop old, enable + start new, restart NM
            for cmd in [
                ["/usr/bin/systemctl", "stop", other],
                ["/usr/bin/systemctl", "disable", other],
                ["/usr/bin/systemctl", "enable", target],
                ["/usr/bin/systemctl", "start", target],
            ]:
                await asyncio.to_thread(self._run_cmd, cmd, 10, True)

            restart = await asyncio.to_thread(
                self._run_cmd,
                ["/usr/bin/systemctl", "restart", "NetworkManager"],
                15,
                True,
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

            final_backend = await asyncio.to_thread(self._get_current_backend)

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
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": False,
                    "needs_reboot": False,
                    "reconnect_timed_out": reconnect_timed_out,
                    "message": (
                        f"Expected {target} but the system reports "
                        f"{final_backend or 'no backend'}. A reboot may help."
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
        try:
            self._ensure_backend_switch_state()
            if backend not in ("iwd", "wpa_supplicant"):
                return {
                    "accepted": False,
                    "reason": "invalid_backend",
                    "message": "Backend must be 'iwd' or 'wpa_supplicant'.",
                }
            if not await asyncio.to_thread(self._has_backend_tool):
                return {
                    "accepted": False,
                    "reason": "tool_missing",
                    "message": "WiFi backend switch tool not found on this system.",
                }
            if self._backend_switch.get("in_progress"):
                return {
                    "accepted": False,
                    "reason": "in_progress",
                    "message": "Backend switch already in progress.",
                }
            current = await asyncio.to_thread(self._get_current_backend)
            if current == backend:
                return {
                    "accepted": False,
                    "reason": "already_set",
                    "message": f"Backend is already {backend}.",
                    "backend": current,
                }

            self._backend_switch.update({
                "in_progress": True,
                "phase": "switching",
                "target": backend,
                "started_at": int(time.time()),
                "result": None,
            })
            # Route to the appropriate worker based on backend method
            method = await asyncio.to_thread(self._get_backend_method)
            if method == "steamos_manager":
                worker = self._steamos_manager_backend_switch_worker(backend)
            elif method == "steamos":
                worker = self._backend_switch_worker(backend)
            else:
                worker = self._generic_backend_switch_worker(backend)
            self._backend_switch_task = asyncio.create_task(worker)
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
