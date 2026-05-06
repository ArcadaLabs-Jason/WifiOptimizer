"""WiFi Optimizer backend for Decky Loader.

Runs as root inside the plugin_loader process. All public async methods on
the Plugin class are callable from the React frontend via Decky's IPC. State
is persisted to settings.json under DECKY_PLUGIN_SETTINGS_DIR and shared with
the NetworkManager dispatcher script at defaults/dispatcher.sh.tmpl, which
reapplies volatile optimizations (power save, PCIe ASPM, buffer tuning, CAKE
QoS) on every WiFi reconnect independently of Decky.
"""

import os
import json
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


def _load_settings() -> dict:
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        # Merge with defaults (adds new keys), then strip stale keys
        merged = {**DEFAULT_SETTINGS, **data}
        return {k: v for k, v in merged.items() if k in DEFAULT_SETTINGS}
    except Exception:
        return dict(DEFAULT_SETTINGS)


def _save_settings(data: dict):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    # Atomic write: write to temp file then rename to prevent corruption on crash
    tmp_path = SETTINGS_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, SETTINGS_FILE)


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

    def _get_backend_method(self) -> str:
        """Return 'steamos', 'generic', or 'none'.
        SteamOS has a privileged helper. Generic uses NM conf + systemctl
        directly and requires iwd to be installed. Non-SteamOS distros
        always use generic even if the SteamOS helper exists (it may
        behave differently on Bazzite/CachyOS)."""
        settings = _load_settings()
        distro = settings.get("distro_id", "unknown")
        if distro == "steamos" and os.path.isfile(BACKEND_HELPER) and os.access(BACKEND_HELPER, os.X_OK):
            return "steamos"
        if os.path.isfile("/usr/lib/systemd/system/iwd.service"):
            return "generic"
        return "none"

    def _has_backend_tool(self) -> bool:
        return self._get_backend_method() != "none"

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
        # No config found — check which service is running
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

    def _hard_reconnect(self, uuid: str | None = None):
        """Reconnect by cycling WiFi radio to fully reset NM connection state."""
        self._run_cmd(["/usr/bin/nmcli", "radio", "wifi", "off"])
        self._run_cmd(["/usr/bin/nmcli", "radio", "wifi", "on"])
        if uuid:
            self._run_cmd(["/usr/bin/nmcli", "con", "up", "uuid", uuid], timeout=10)

    def _apply_driver_fixes(self, enable: bool):
        """Apply or revert driver-specific power save fixes from DRIVER_PROFILES.
        Silently no-ops for drivers with no sysfs paths or modprobe options."""
        settings = _load_settings()
        profile = DRIVER_PROFILES.get(settings.get("driver"), {})

        val = "Y" if enable else "N"
        for path in profile.get("sysfs_power_fixes", []):
            try:
                with open(path, "w") as f:
                    f.write(val)
            except FileNotFoundError:
                pass
            except PermissionError:
                decky.logger.info(f"sysfs path not writable: {path}")

        options = profile.get("modprobe_options", [])
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
            if not iface:
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

    def _install_dispatcher(self):
        try:
            template_path = os.path.join(
                decky.DECKY_PLUGIN_DIR, "defaults", "dispatcher.sh.tmpl"
            )
            with open(template_path, "r") as f:
                script = f.read()
            script = script.replace("__SETTINGS_PATH__", SETTINGS_FILE)
            script = script.replace("__PLUGIN_DIR__", decky.DECKY_PLUGIN_DIR)
            with open(DISPATCHER_PATH, "w") as f:
                f.write(script)
            os.chmod(DISPATCHER_PATH, 0o755)
            decky.logger.info("Dispatcher script installed")
        except Exception as e:
            decky.logger.error(f"Failed to install dispatcher: {e}")

    def _remove_dispatcher(self):
        try:
            os.remove(DISPATCHER_PATH)
            decky.logger.info("Dispatcher script removed")
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.error(f"Failed to remove dispatcher: {e}")

    def _rotate_logs(self, keep: int = 10):
        """Prune old log files on plugin startup. Decky does not rotate plugin
        logs automatically; each plugin load creates a new timestamped file in
        DECKY_PLUGIN_LOG_DIR, so without pruning they accumulate forever.
        Keep the newest `keep` files (typical size ~2-3 KB each, so bounded at
        roughly 30 KB total).
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
            _save_settings(settings)

            if settings.get("auto_fix_on_wake", True):
                self._install_dispatcher()

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
            if self._get_backend_method() != "none":
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
        if not iface:
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
        setter. Callers log the error separately with the setter name."""
        return {"success": False, "error": "unexpected", "message": str(e)}

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
            with open(diag_path, "w") as f:
                json.dump(info, f, indent=2)
            return {"success": True, "path": diag_path}
        except Exception as e:
            decky.logger.error(f"save_diagnostic_info error: {e}")
            return {"success": False, "error": str(e)}

    # ---- Status ----

    async def get_status(self) -> dict:
        # Use shorter timeout for read-only status queries to avoid blocking
        # the event loop if NM is unresponsive (~10 commands × 2s = 20s worst case)
        T = 2

        try:
            settings = _load_settings()
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
            backend_available = self._has_backend_tool()
            status["live"]["backend_tool_available"] = backend_available
            if backend_available:
                status["live"]["wifi_backend"] = self._get_current_backend() or ""

            if not connected:
                status["live"]["dispatcher_installed"] = os.path.isfile(
                    DISPATCHER_PATH
                )
                return status

            # Remember UUID and ensure high autoconnect-priority so NM
            # prefers this profile over duplicates on boot (fixes 2.4GHz issue)
            if uuid and uuid != settings.get("last_connection_uuid"):
                settings["last_connection_uuid"] = uuid
                settings["priority_set"] = False
                _save_settings(settings)

            if uuid and not settings.get("priority_set"):
                # Bump priority to favor this profile over duplicates on boot.
                self._nmcli_modify(
                    uuid, "connection.autoconnect-priority", "100", timeout=T
                )
                settings["priority_set"] = True
                _save_settings(settings)

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
                status["drift"]["bssid_lock"] = True

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
                self._nmcli_modify(uuid, "ipv6.method", "disabled", timeout=T)

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
            if settings.get("band_preference_enabled") and live_band != expected_band:
                status["drift"]["band_preference"] = True
                self._nmcli_modify(uuid, "802-11-wireless.band", expected_band, timeout=T)

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

            # Dispatcher
            status["live"]["dispatcher_installed"] = os.path.isfile(DISPATCHER_PATH)

            # Last enforced by dispatcher
            try:
                with open(ENFORCED_FILE, "r") as f:
                    status["live"]["last_enforced"] = int(f.read().strip())
            except Exception:
                status["live"]["last_enforced"] = 0

            return status
        except Exception as e:
            decky.logger.error(f"get_status error: {e}")
            return self._unexpected_response(e)

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
            settings["auto_fix_on_wake"] = enabled

            if enabled:
                self._install_dispatcher()
            else:
                self._remove_dispatcher()

            _save_settings_with_timestamp(settings)
            return {
                "success": True,
                "dispatcher_installed": os.path.isfile(DISPATCHER_PATH),
            }
        except Exception as e:
            decky.logger.error(f"set_auto_fix error: {e}")
            return {"success": False, "error": "write_failed", "message": str(e)}

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
                _save_settings_with_timestamp(settings)
                self._hard_reconnect(uuid)
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
                settings["bssid_lock_enabled"] = False
                settings["bssid_lock_value"] = ""
                settings["bssid_lock_connection_uuid"] = ""
                _save_settings_with_timestamp(settings)
                self._hard_reconnect(uuid)

            return {"success": True, "bssid_locked": enabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_bssid_lock error: {e}")
            return self._unexpected_response(e)

    async def set_band_preference(self, enabled: bool, band: str = "a") -> dict:
        try:

            if enabled and band not in ("a", "bg"):
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

            value = band if enabled else ""
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
                self._nmcli_modify(uuid, "802-11-wireless.bssid", "")

            settings["band_preference_enabled"] = enabled
            settings["band_preference"] = band
            _save_settings_with_timestamp(settings)

            self._hard_reconnect(uuid)

            # Re-lock BSSID to whatever AP NM picked on the new band
            if enabled and had_bssid_lock:
                time.sleep(3)
                iface = self._get_wifi_interface()
                if iface:
                    link_result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"])
                    for line in link_result.get("stdout", "").split("\n"):
                        if "Connected to" in line:
                            parts = line.split()
                            if len(parts) >= 3:
                                new_bssid = parts[2]
                                self._nmcli_modify(uuid, "802-11-wireless.bssid", new_bssid)
                                settings = _load_settings()
                                settings["bssid_lock_value"] = new_bssid
                                _save_settings(settings)
                                decky.logger.info(f"Re-locked BSSID to {new_bssid} after band change")
                            break

            return {"success": True, "band": value, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_band_preference error: {e}")
            return self._unexpected_response(e)

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
                self._nmcli_modify(uuid, "ipv4.dns", "")
                self._nmcli_modify(uuid, "ipv4.ignore-auto-dns", "no")
                servers = ""

            settings = _load_settings()
            settings["dns_enabled"] = enabled
            settings["dns_provider"] = provider
            settings["dns_servers"] = servers
            _save_settings_with_timestamp(settings)

            self._hard_reconnect(uuid)
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

            self._hard_reconnect(uuid)
            return {"success": True, "ipv6_disabled": disabled, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_ipv6 error: {e}")
            return self._unexpected_response(e)

    async def set_buffer_tuning(self, enabled: bool) -> dict:
        try:

            params = SYSCTL_PARAMS if enabled else SYSCTL_DEFAULTS
            for key, value in params.items():
                result = self._run_cmd(
                    ["/usr/bin/sysctl", "-w", f"{key}={value}"]
                )
                if not result["success"]:
                    decky.logger.error(f"sysctl {key}={value} failed: {result['stderr']}")

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
                self._run_cmd(["/usr/bin/tc", "qdisc", "del", "dev", iface, "root"])
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
                "message": str(e),
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

            script = f"""#!/bin/bash
sleep 2
PLUGIN_DIR="{plugin_dir}"
TMP=$(mktemp -d)
cleanup() {{ rm -rf "$TMP"; rm -f "$0"; }}
trap cleanup EXIT

curl -sL "{download_url}" -o "$TMP/update.tar.gz"
tar xzf "$TMP/update.tar.gz" -C "$TMP"
SRC="$TMP/{src_dir}"

if [ ! -f "$SRC/plugin.json" ]; then
    logger -t wifi-optimizer "Update failed: download error"
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

logger -t wifi-optimizer "Updated to {label}, restarting plugin_loader"
systemctl restart plugin_loader 2>/dev/null || true
"""
            script_path = "/tmp/wifi-optimizer-update.sh"
            with open(script_path, "w") as f:
                f.write(script)
            os.chmod(script_path, 0o700)

            clean_env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
            subprocess.Popen(
                ["/bin/bash", script_path],
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
                if iface_check != "wlan0":
                    needs_reboot = True

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
                    "recovery_performed": False,
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
                    "message": f"Expected {target} but got {final_backend}. A reboot may help.",
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
            if not self._has_backend_tool():
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
            method = self._get_backend_method()
            if method == "steamos":
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
