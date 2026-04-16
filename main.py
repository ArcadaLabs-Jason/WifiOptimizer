import os
import json
import time
import asyncio
import subprocess

try:
    import decky
except ImportError:
    # Fallback if decky module is unavailable (shouldn't happen in normal operation)
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
BACKEND_TOOL = "/usr/bin/steamos-wifi-set-backend"
BACKEND_HELPER = "/usr/bin/steamos-polkit-helpers/steamos-wifi-set-backend-privileged"
WIFI_BACKEND_CONF = "/etc/NetworkManager/conf.d/99-valve-wifi-backend.conf"
NM_DEFAULT_CONF = "/usr/lib/NetworkManager/conf.d/10-steamos-defaults.conf"

# LCD (RTL8822CE) has firmware bugs with deep power save and PCIe ASPM.
# These sysfs paths let us disable them at runtime (no module reload needed).
RTW88_SYSFS_PARAMS = [
    "/sys/module/rtw88_core/parameters/disable_lps_deep",
    "/sys/module/rtw88_pci/parameters/disable_aspm",
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
    "last_connection_uuid": "",
    "priority_set": False,
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
    # ---- Helpers ----

    def _run_cmd(self, cmd: list[str], timeout: int = 5, clean_env: bool = False) -> dict:
        try:
            env = None
            if clean_env:
                # Use system libraries, not Decky's bundled ones
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

    def _has_backend_tool(self) -> bool:
        # Privileged helper is what we actually invoke — calling the wrapper fails
        # from a root systemd context because it goes through pkexec.
        return os.path.isfile(BACKEND_HELPER) and os.access(BACKEND_HELPER, os.X_OK)

    def _get_current_backend(self) -> str | None:
        """Return 'iwd', 'wpa_supplicant', or None if unknown.

        Reads user override (99-valve-wifi-backend.conf) first, then the
        SteamOS default (10-steamos-defaults.conf). Matches the same resolution
        order that steamos-wifi-set-backend itself uses.
        """
        for path in (WIFI_BACKEND_CONF, NM_DEFAULT_CONF):
            try:
                with open(path, "r") as f:
                    for line in f:
                        line = line.strip()
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

    def _ensure_backend_switch_state(self):
        if not hasattr(self, "_backend_switch"):
            self._backend_switch = {
                "in_progress": False,
                "phase": "idle",
                "target": None,
                "started_at": 0,
                "result": None,
            }

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

    def _apply_rtw88_fixes(self, enable: bool):
        """Apply or revert LCD-specific RTL8822CE driver power save fixes.
        Silently no-ops on OLED (sysfs paths don't exist)."""
        val = "Y" if enable else "N"
        for path in RTW88_SYSFS_PARAMS:
            try:
                with open(path, "w") as f:
                    f.write(val)
            except (FileNotFoundError, PermissionError):
                pass

        # Persist via modprobe.d for next boot
        if enable:
            try:
                os.makedirs(os.path.dirname(MODPROBE_CONF_PATH), exist_ok=True)
                with open(MODPROBE_CONF_PATH, "w") as f:
                    f.write(
                        "# WiFi Optimizer - LCD RTL8822CE deep power save fixes\n"
                        "options rtw88_core disable_lps_deep=Y\n"
                        "options rtw88_pci disable_aspm=Y\n"
                    )
            except Exception as e:
                decky.logger.error(f"Failed to write modprobe config: {e}")
        else:
            try:
                os.remove(MODPROBE_CONF_PATH)
            except FileNotFoundError:
                pass

    def _apply_pcie_aspm_fix(self, enable: bool):
        """Disable or restore PCIe ASPM for the WiFi device.
        Prevents throughput degradation during sustained streaming (OLED ath11k).
        Also prevents LCD rtw88 PCIe stalls. Works on both models."""
        try:
            # Discover WiFi PCI device path dynamically
            iface = self._get_wifi_interface() or "wlan0"
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

    # ---- Lifecycle ----

    async def _main(self):
        try:
            decky.logger.info("WiFi Optimizer starting")
            self._ensure_backend_switch_state()
            info = await self.get_device_info()
            settings = _load_settings()
            settings["model"] = info.get("model", "unknown")
            settings["driver"] = info.get("driver", "unknown")
            _save_settings(settings)

            if settings.get("model") in ("lcd", "oled") and settings.get("auto_fix_on_wake", True):
                self._install_dispatcher()

            decky.logger.info(
                f"WiFi Optimizer ready: model={info.get('model')}, driver={info.get('driver')}"
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
            for path in [NM_CONF_PATH, MODPROBE_CONF_PATH]:
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
        except Exception as e:
            decky.logger.error(f"_uninstall error: {e}")

    async def _migration(self):
        pass

    # ---- Hardware detection ----

    async def get_device_info(self) -> dict:
        try:
            model = "unknown"
            driver = "unknown"
            wifi_chip = "unknown"
            bands = ["2.4 GHz", "5 GHz"]

            try:
                with open("/sys/devices/virtual/dmi/id/product_name", "r") as f:
                    product = f.read().strip()
                if product == "Jupiter":
                    model = "lcd"
                    wifi_chip = "WiFi 5 (RTL8822CE)"
                elif product == "Galileo":
                    model = "oled"
                    wifi_chip = "WiFi 6E (QCA206X)"
                    bands.append("6 GHz")
            except Exception:
                pass

            result = self._run_cmd(["/usr/bin/lsmod"])
            if result["success"]:
                if "ath11k_pci" in result["stdout"]:
                    driver = "ath11k_pci"
                elif "rtw88" in result["stdout"]:
                    driver = "rtw88"

            return {
                "success": True,
                "model": model,
                "driver": driver,
                "wifi_chip": wifi_chip,
                "bands": bands,
            }
        except Exception as e:
            decky.logger.error(f"get_device_info error: {e}")
            return {
                "success": True,
                "model": "unknown",
                "driver": "unknown",
                "wifi_chip": "unknown",
                "bands": ["2.4 GHz", "5 GHz"],
            }

    def _is_supported_device(self) -> bool:
        """Check if this is a Steam Deck (Jupiter or Galileo)."""
        settings = _load_settings()
        return settings.get("model") in ("lcd", "oled")

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
            supported = settings.get("model") in ("lcd", "oled")

            status = {
                "success": True,
                "connected": connected,
                "supported": supported,
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
                self._run_cmd(
                    ["/usr/bin/nmcli", "con", "mod", "uuid", uuid,
                     "connection.autoconnect-priority", "100"],
                    timeout=T,
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

            # Channel info — parse to "36 (80 MHz)" format
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
            status["live"]["ipv6_method"] = (
                ipv6_out.split(":", 1)[1].strip() if ":" in ipv6_out else ""
            )

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
            status["live"]["band"] = (
                band_out.split(":", 1)[1].strip() if ":" in band_out else ""
            )

            # Buffer tuning
            sysctl_result = self._run_cmd(
                ["/usr/bin/sysctl", "-n", "net.core.rmem_max"], timeout=T
            )
            current_rmem = sysctl_result.get("stdout", "").strip()
            status["live"]["buffer_tuning_applied"] = current_rmem == "16777216"
            if settings.get("buffer_tuning_enabled") and current_rmem != "16777216":
                status["drift"]["buffer_tuning"] = True

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
            return {"success": False, "error": "unexpected", "message": str(e)}

    # ---- Optimization setters ----

    async def set_power_save(self, disabled: bool) -> dict:
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
            iface = self._get_wifi_interface()

            # Apply immediately if connected — verify before saving
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

            # LCD: disable deep LPS and rtw88 ASPM (no-op on OLED)
            self._apply_rtw88_fixes(disabled)
            # Both: disable PCIe ASPM for WiFi device (prevents throughput degradation)
            self._apply_pcie_aspm_fix(disabled)

            # Save settings only after success
            settings = _load_settings()
            settings["power_save_disabled"] = disabled
            _save_settings_with_timestamp(settings)

            return {"success": True, "power_save_off": disabled}
        except Exception as e:
            decky.logger.error(f"set_power_save error: {e}")
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def set_auto_fix(self, enabled: bool) -> dict:
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
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
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
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

                result = self._run_cmd(
                    [
                        "/usr/bin/nmcli",
                        "con",
                        "mod",
                        "uuid",
                        uuid,
                        "802-11-wireless.bssid",
                        bssid,
                    ]
                )
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
                # Disabling works on saved profiles — no active WiFi needed
                iface, uuid, _ = self._require_wifi()
                if not uuid:
                    uuid = self._get_saved_connection_uuid()
                if not uuid:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "No connection UUID found. Connect to WiFi first.",
                    }

                result = self._run_cmd(
                    [
                        "/usr/bin/nmcli",
                        "con",
                        "mod",
                        "uuid",
                        uuid,
                        "802-11-wireless.bssid",
                        "",
                    ]
                )
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
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def set_band_preference(self, enabled: bool, band: str = "a") -> dict:
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
            if enabled and band not in ("a", "bg"):
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": f"Invalid band '{band}'. Must be 'a' (5 GHz) or 'bg' (2.4 GHz).",
                }

            iface, uuid, _ = self._require_wifi()
            if enabled and not uuid:
                return {
                    "success": False,
                    "error": "no_wifi",
                    "message": "Connect to WiFi first to set band preference",
                }
            if not uuid:
                uuid = self._get_saved_connection_uuid()
            if not uuid:
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "No connection UUID found. Connect to WiFi first.",
                }

            value = band if enabled else ""
            result = self._run_cmd(
                [
                    "/usr/bin/nmcli",
                    "con",
                    "mod",
                    "uuid",
                    uuid,
                    "802-11-wireless.band",
                    value,
                ]
            )
            if not result["success"]:
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "Couldn't update band preference",
                    "detail": result["stderr"],
                }

            settings = _load_settings()
            settings["band_preference_enabled"] = enabled
            settings["band_preference"] = band
            _save_settings_with_timestamp(settings)

            self._hard_reconnect(uuid)
            return {"success": True, "band": value, "reconnected": True}
        except Exception as e:
            decky.logger.error(f"set_band_preference error: {e}")
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def set_dns(
        self, enabled: bool, provider: str = "cloudflare", custom_servers: str = ""
    ) -> dict:
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
            iface, uuid, _ = self._require_wifi()
            if enabled and not uuid:
                return {
                    "success": False,
                    "error": "no_wifi",
                    "message": "Connect to WiFi first to set DNS",
                }
            if not uuid:
                uuid = self._get_saved_connection_uuid()
            if not uuid:
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "No connection UUID found. Connect to WiFi first.",
                }

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

                result = self._run_cmd(
                    [
                        "/usr/bin/nmcli",
                        "con",
                        "mod",
                        "uuid",
                        uuid,
                        "ipv4.dns",
                        servers,
                    ]
                )
                if not result["success"]:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't set DNS",
                        "detail": result["stderr"],
                    }

                result2 = self._run_cmd(
                    [
                        "/usr/bin/nmcli",
                        "con",
                        "mod",
                        "uuid",
                        uuid,
                        "ipv4.ignore-auto-dns",
                        "yes",
                    ]
                )
                if not result2["success"]:
                    return {
                        "success": False,
                        "error": "nmcli_failed",
                        "message": "Couldn't set ignore-auto-dns",
                        "detail": result2["stderr"],
                    }
            else:
                self._run_cmd(
                    [
                        "/usr/bin/nmcli",
                        "con",
                        "mod",
                        "uuid",
                        uuid,
                        "ipv4.dns",
                        "",
                    ]
                )
                self._run_cmd(
                    [
                        "/usr/bin/nmcli",
                        "con",
                        "mod",
                        "uuid",
                        uuid,
                        "ipv4.ignore-auto-dns",
                        "no",
                    ]
                )
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
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def set_ipv6(self, disabled: bool) -> dict:
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
            iface, uuid, _ = self._require_wifi()
            if disabled and not uuid:
                return {
                    "success": False,
                    "error": "no_wifi",
                    "message": "Connect to WiFi first to disable IPv6",
                }
            if not uuid:
                uuid = self._get_saved_connection_uuid()
            if not uuid:
                return {
                    "success": False,
                    "error": "nmcli_failed",
                    "message": "No connection UUID found. Connect to WiFi first.",
                }

            method = "disabled" if disabled else "auto"
            result = self._run_cmd(
                [
                    "/usr/bin/nmcli",
                    "con",
                    "mod",
                    "uuid",
                    uuid,
                    "ipv6.method",
                    method,
                ]
            )
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
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def set_buffer_tuning(self, enabled: bool) -> dict:
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
            params = SYSCTL_PARAMS if enabled else SYSCTL_DEFAULTS
            for key, value in params.items():
                result = self._run_cmd(
                    ["/usr/bin/sysctl", "-w", f"{key}={value}"]
                )
                if not result["success"]:
                    decky.logger.error(f"sysctl {key}={value} failed: {result['stderr']}")

            # TX queue length
            iface = self._get_wifi_interface()
            if iface:
                txq = "2000" if enabled else "1000"
                self._run_cmd(
                    ["/usr/bin/ip", "link", "set", iface, "txqueuelen", txq]
                )

            settings = _load_settings()
            settings["buffer_tuning_enabled"] = enabled
            _save_settings_with_timestamp(settings)
            return {"success": True, "buffer_tuning": enabled}
        except Exception as e:
            decky.logger.error(f"set_buffer_tuning error: {e}")
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def optimize_safe(self) -> dict:
        """Apply universally-safe optimizations: power save, BSSID lock, auto-fix, buffer tuning."""
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
            results = {}
            applied = 0
            total = 4

            # Order matters: BSSID lock reconnects WiFi which resets power_save.
            # Apply auto-fix and buffer tuning first (no reconnect), then BSSID
            # lock (reconnects — dispatcher reapplies settings), then power_save
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
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def reapply_all(self) -> dict:
        """Force reapply all enabled optimizations."""
        try:
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device. This plugin is designed for Steam Deck only."}
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
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def reset_settings(self) -> dict:
        """Delete settings and revert to defaults."""
        try:
            # Revert runtime state
            self._apply_rtw88_fixes(False)
            self._apply_pcie_aspm_fix(False)
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

            decky.logger.info("Settings reset to defaults")
            return {"success": True, "message": "Settings reset to defaults"}
        except Exception as e:
            decky.logger.error(f"reset_settings error: {e}")
            return {"success": False, "error": "unexpected", "message": str(e)}

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
            return {"success": False, "error": "unexpected", "message": str(e)}

    async def check_for_update(self) -> dict:
        """Check GitHub for a newer version (stable release or beta branch)."""
        try:
            current = decky.DECKY_PLUGIN_VERSION
            settings = _load_settings()
            channel = settings.get("update_channel", "stable")
            decky.logger.info(f"Update check: current={current}, channel={channel}")

            if channel == "beta":
                # Fetch package.json from beta branch
                result = self._run_cmd(
                    [
                        "/usr/bin/curl", "-sL", "--max-time", "10",
                        "https://raw.githubusercontent.com/ArcadaLabs-Jason/WifiOptimizer/beta/package.json",
                    ],
                    timeout=15,
                    clean_env=True,
                )
            else:
                # Fetch latest release
                result = self._run_cmd(
                    [
                        "/usr/bin/curl", "-sL", "--max-time", "10",
                        "-H", "Accept: application/vnd.github.v3+json",
                        "https://api.github.com/repos/ArcadaLabs-Jason/WifiOptimizer/releases/latest",
                    ],
                    timeout=15,
                    clean_env=True,
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
                update_available = latest_tuple > current_tuple or "-beta" in current

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
            if not self._is_supported_device():
                return {"success": False, "error": "unexpected", "message": "Unsupported device."}

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
            return {"success": False, "error": "unexpected", "message": str(e)}

    # ---- WiFi backend switch (iwd / wpa_supplicant) ----

    async def _backend_switch_worker(self, target: str):
        """Background task that switches the WiFi backend with phase transitions.

        Invokes the privileged helper directly (at /usr/bin/steamos-polkit-helpers/…)
        to bypass pkexec, which fails from a rootful systemd context with no polkit
        agent. The helper handles wlan0 recovery on OLED internally; we parse its
        output to report whether recovery fired.
        """
        try:
            settings = _load_settings()
            is_oled = settings.get("model") == "oled"
            other = "iwd" if target == "wpa_supplicant" else "wpa_supplicant"

            # Phase: switching — write config then restart services
            self._backend_switch["phase"] = "switching"
            write_result = await asyncio.to_thread(
                self._run_cmd, [BACKEND_HELPER, "write_config", target], 5, False
            )
            if not write_result["success"]:
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "target": target,
                    "message": "Couldn't write WiFi backend config.",
                    "detail": write_result.get("stderr", "")[:200],
                }
                return

            restart_result = await asyncio.to_thread(
                self._run_cmd, [BACKEND_HELPER, "restart_units", other], 45, False
            )
            rs_stdout = restart_result.get("stdout", "")
            rs_stderr = restart_result.get("stderr", "")
            recovery_performed = "missing wlan0" in rs_stdout
            needs_reboot = "wlan0 could not be created" in rs_stderr

            # Defensive: on OLED, confirm wlan0 actually exists post-restart
            await asyncio.sleep(1)
            if is_oled and target == "wpa_supplicant":
                iface_check = await asyncio.to_thread(self._get_wifi_interface)
                if iface_check != "wlan0":
                    needs_reboot = True

            # Phase: reconnecting — wait for NM to reconnect to WiFi
            if not needs_reboot:
                self._backend_switch["phase"] = "reconnecting"
                elapsed = 0
                while elapsed < 15:
                    iface = await asyncio.to_thread(self._get_wifi_interface)
                    uuid = None
                    if iface:
                        uuid = await asyncio.to_thread(self._get_active_connection_uuid)
                    if iface and uuid:
                        break
                    await asyncio.sleep(1)
                    elapsed += 1

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
                self._backend_switch["phase"] = "failed"
                self._backend_switch["result"] = {
                    "success": False,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": False,
                    "message": "Backend switch did not take effect.",
                    "detail": rs_stderr[:200] or rs_stdout[:200],
                }
            else:
                self._backend_switch["phase"] = "done"
                self._backend_switch["result"] = {
                    "success": True,
                    "backend": final_backend,
                    "target": target,
                    "recovery_performed": recovery_performed,
                    "needs_reboot": False,
                }
            decky.logger.info(
                f"backend switch: target={target}, final={final_backend}, "
                f"recovery={recovery_performed}, needs_reboot={needs_reboot}"
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

    async def start_backend_switch(self, backend: str) -> dict:
        """Kick off a backend switch. Returns immediately; poll get_backend_switch_status for progress."""
        try:
            self._ensure_backend_switch_state()
            if not self._is_supported_device():
                return {
                    "accepted": False,
                    "reason": "unsupported_device",
                    "message": "Unsupported device. This plugin is designed for Steam Deck only.",
                }
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
                    "message": "steamos-wifi-set-backend not found. Requires SteamOS 3.6 or later.",
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
            # Keep a reference so the task isn't garbage-collected mid-run
            self._backend_switch_task = asyncio.create_task(
                self._backend_switch_worker(backend)
            )
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
            return {"success": False, "message": str(e)}
