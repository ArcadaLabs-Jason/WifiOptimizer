import {
  useState,
  useEffect,
  useCallback,
  useRef,
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  DropdownItem,
  TextField,
  staticClasses,
} from "@decky/ui";
import { definePlugin } from "@decky/api";
import { FaWifi } from "react-icons/fa";

import * as backend from "./backend";
import type {
  PluginStatus,
  MethodResult,
  OptimizeSafeResult,
  UpdateCheckResult,
  BadgeStatus,
  BackendSwitchStatus,
} from "./types";
import { ERROR_MESSAGES } from "./types";
import { InfoRow } from "./components/InfoRow";
import { StatsGrid } from "./components/StatsGrid";
import { Banner } from "./components/Banner";
import { BackendToggleRow } from "./components/BackendToggleRow";
import { UpdatesSection } from "./components/UpdatesSection";
import { PanelHeader } from "./components/PanelHeader";
import { PanelFooter } from "./components/PanelFooter";
import { ActionsSection } from "./components/ActionsSection";
import { theme } from "./theme";

const REFRESH_INTERVAL = 3000;
const RECONNECT_DELAY = 4000;
const BACKEND_POLL_INTERVAL = 750;
const UPDATE_CHECK_INTERVAL = 5 * 60 * 1000;
const UPDATE_CHECK_DEDUPE_MS = 60 * 1000;
const UPDATE_TIMEOUT_MS = 60 * 1000;

function getBadge(
  driftKey: string | undefined,
  status: PluginStatus | null,
  errorKey: string | null,
): { badge: BadgeStatus; text: string } | null {
  // Only surface a badge when it tells the user something the toggle position
  // doesn't - failure or drift between our setting and the system state.
  // Binary on/off badges are redundant with the toggle itself and are hidden.
  if (errorKey) return { badge: "error", text: "failed" };
  if (driftKey && status?.drift?.[driftKey]) return { badge: "drifted", text: "drifted" };
  return null;
}

const DNS_OPTIONS = [
  { data: "cloudflare", label: "Cloudflare (1.1.1.1)" },
  { data: "google", label: "Google (8.8.8.8)" },
  { data: "quad9", label: "Quad9 (9.9.9.9)" },
  { data: "custom", label: "Custom" },
];

// Catches render-time exceptions in the panel tree so a bad status shape or
// other unexpected error surfaces a recoverable message instead of Decky
// blanking the panel. The child tree is re-entered on next mount (panel
// reopen), so users can recover without a plugin restart.
class ErrorBoundary extends Component<
  { children: ReactNode },
  { err: Error | null }
> {
  state = { err: null as Error | null };

  static getDerivedStateFromError(err: Error) {
    return { err };
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    console.error("WiFi Optimizer render error:", err, info);
  }

  render() {
    if (this.state.err) {
      return (
        <Banner variant="error">
          WiFi Optimizer hit an unexpected error. Close and reopen the panel to
          recover. If it keeps happening, please report at
          github.com/ArcadaLabs-Jason/WifiOptimizer.
        </Banner>
      );
    }
    return this.props.children;
  }
}

// The single component that owns all panel state. It's intentionally flat:
// every section reads from shared state and state setters are passed down to
// leaf components. Organized top-to-bottom as:
//   1. State and refs
//   2. Stable callbacks (setBusy, runUpdateCheck, refreshStatus, poll helpers)
//   3. Lifecycle effects (main refresh, init, connectivity retry, update timeout,
//      update heartbeat)
//   4. User action handlers (toggles, optimize, reset, backend switch, updates)
//   5. Derived render state (connected, supportTier, allSafeActive, etc.)
//   6. JSX for the panel sections in top-to-bottom screen order
function Content() {
  // --- General status and toggle state ---
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [isBusy, setIsBusy] = useState(false);
  const [applyingAll, setApplyingAll] = useState(false);
  const [optimizeResult, setOptimizeResult] = useState<OptimizeSafeResult | null>(null);
  const [customDnsInput, setCustomDnsInput] = useState("");

  // --- Update flow state ---
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResult | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [updateError, setUpdateError] = useState<string | null>(null);

  // --- Backend switch state ---
  const [backendSwitch, setBackendSwitch] = useState<BackendSwitchStatus | null>(null);

  // --- Refs ---
  // busyRef is the synchronous re-entrancy guard; isBusy (above) is the React
  // state mirror so UI can respond. setBusy below writes both together.
  const busyRef = useRef(false);
  const backendPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevConnectedRef = useRef<boolean | null>(null);
  const lastUpdateCheckAtRef = useRef<number>(0);

  const setBusy = useCallback((val: boolean) => {
    busyRef.current = val;
    setIsBusy(val);
  }, []);

  // Runs checkForUpdate with dedupe - skips if a check was issued within the
  // dedupe window. Lowers GitHub API pressure in CGNAT/dorm scenarios where
  // many devices share an IP. Manual button bypasses this (force=true).
  const runUpdateCheck = useCallback((force: boolean = false) => {
    const now = Date.now();
    if (!force && now - lastUpdateCheckAtRef.current < UPDATE_CHECK_DEDUPE_MS) {
      return;
    }
    lastUpdateCheckAtRef.current = now;
    backend.checkForUpdate().then(setUpdateInfo).catch(() => {});
  }, []);

  const refreshStatus = useCallback(async (force: boolean = false) => {
    // Background interval ticks defer while a user operation is in flight;
    // handler-driven refreshes (at the end of an op) force through so the UI
    // catches up immediately instead of waiting for the next interval tick.
    if (!force && busyRef.current) return;
    try {
      const s = await backend.getStatus();
      setStatus(s);
      if (s.settings) {
        if (s.settings.dns_provider === "custom") {
          setCustomDnsInput(s.settings.dns_servers || "");
        }
      }
    } catch (e) {
      console.error("WiFi Optimizer: failed to get status", e);
    }
  }, []);

  const stopBackendPoll = () => {
    if (backendPollRef.current) {
      clearInterval(backendPollRef.current);
      backendPollRef.current = null;
    }
  };

  const beginBackendPoll = useCallback(() => {
    stopBackendPoll();
    backendPollRef.current = setInterval(async () => {
      try {
        const s = await backend.getBackendSwitchStatus();
        if (s.in_progress) {
          // Mid-switch: update so the subtitle can show the current phase.
          setBackendSwitch(s);
          return;
        }
        // Terminal. Stop polling, then refresh status BEFORE flipping backend
        // state and clearing busy - this keeps the optimistic toggle position
        // steady until live.wifi_backend has caught up AND keeps other toggles
        // disabled until the whole UI has consistent state, avoiding any
        // toggle/untoggle flicker at the end of the switch.
        stopBackendPoll();
        if (s.result && !s.result.success && s.result.message) {
          const detail = s.result.detail ? ` (${s.result.detail})` : "";
          setErrors((prev) => ({
            ...prev,
            wifi_backend: s.result!.message! + detail,
          }));
        }
        await refreshStatus(true);
        setBackendSwitch(s);
        setBusy(false);
      } catch (e) {
        stopBackendPoll();
        setBusy(false);
        console.error("backend switch poll error", e);
      }
    }, BACKEND_POLL_INTERVAL);
  }, [refreshStatus, setBusy]);

  // Main refresh interval, paused when the panel is hidden so we don't burn
  // CPU/battery on ~12 subprocess calls every 3s in the background. On return
  // to visible, run one refresh immediately to catch anything that changed
  // while hidden, then resume the interval.
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (id) return;
      refreshStatus();
      id = setInterval(refreshStatus, REFRESH_INTERVAL);
    };
    const stop = () => {
      if (id) {
        clearInterval(id);
        id = null;
      }
    };
    const onVis = () => {
      if (document.hidden) stop();
      else start();
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      stop();
    };
  }, [refreshStatus]);

  // One-time init: initial update check and resume backend polling if a switch
  // was already in flight when the panel opened.
  useEffect(() => {
    // Initial update check. If it fails (e.g., no network yet), the effect
    // below retries on connectivity recovery. QAM tends to cache the panel
    // across close/open, so we can't rely on remount to retry.
    runUpdateCheck();
    // Resume backend-switch polling if one is in flight (panel was reopened mid-switch)
    backend
      .getBackendSwitchStatus()
      .then((s) => {
        if (s.in_progress) {
          setBackendSwitch(s);
          setBusy(true);
          beginBackendPoll();
        }
      })
      .catch(() => {});
    return () => {
      if (backendPollRef.current) clearInterval(backendPollRef.current);
    };
  }, [beginBackendPoll, runUpdateCheck, setBusy]);

  // Retry update check when connectivity recovers. The initial one-shot check
  // in the mount effect misses the case where the panel was already open when
  // the network came back. Skip until status has loaded to avoid a spurious
  // null-to-true transition firing an extra check on every mount.
  useEffect(() => {
    if (!status) return;
    const connected = status.connected;
    const prev = prevConnectedRef.current;
    prevConnectedRef.current = connected;
    if (prev === false && connected === true) {
      runUpdateCheck();
    }
  }, [status?.connected, runUpdateCheck]);

  // Safety timeout: if an update was initiated but plugin_loader hasn't killed
  // us within 60s, the detached update script probably failed (network drop,
  // tarball corruption, etc.). Revert the UI so the user isn't stuck staring
  // at "Updating..." forever, and surface a message they can act on.
  useEffect(() => {
    if (!updating) return;
    const id = setTimeout(() => {
      console.error("WiFi Optimizer: update didn't complete within 60s");
      setUpdating(false);
      setUpdateError(
        "Update didn't complete. Try again, or reinstall manually from Konsole."
      );
    }, UPDATE_TIMEOUT_MS);
    return () => clearTimeout(id);
  }, [updating]);

  // Periodic update re-check - QAM often caches the panel across close/reopen,
  // so the mount-effect check doesn't re-fire. This heartbeat catches new
  // releases when the panel has been left open for a while. Paused when the
  // panel/tab is hidden to avoid pointless GitHub calls accumulating while the
  // user isn't looking; re-fires one check immediately on visibility return.
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (id) return;
      id = setInterval(() => runUpdateCheck(), UPDATE_CHECK_INTERVAL);
    };
    const stop = () => {
      if (id) {
        clearInterval(id);
        id = null;
      }
    };
    const onVis = () => {
      if (document.hidden) {
        stop();
      } else {
        runUpdateCheck();
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      stop();
    };
  }, [runUpdateCheck]);

  // ---- User action handlers ----
  //
  // Generic toggle handler used by every setter toggle (power_save, BSSID,
  // auto_fix, buffer_tuning, band_preference, DNS, IPv6) and by the Force
  // Reapply All button. Takes a key for error tracking and a backend fn.
  const handleToggle = async (key: string, fn: () => Promise<MethodResult>) => {
    if (busyRef.current) return;
    setBusy(true);
    setErrors((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setOptimizeResult(null);

    try {
      const result = await fn();
      if (!result.success) {
        const detail = result.detail ? ` (${result.detail})` : "";
        const msg = (result.message ?? ERROR_MESSAGES[result.error ?? ""] ?? "Unknown error") + detail;
        setErrors((prev) => ({ ...prev, [key]: msg }));
      }
      // Wait for WiFi to reconnect before refreshing status
      if (result.reconnected) {
        await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
      }
    } finally {
      // Refresh before clearing busy so toggles/badges don't briefly flip to
      // stale pre-operation values in the interim render. Refresh runs even on
      // unexpected errors to keep UI consistent with backend state.
      await refreshStatus(true);
      setBusy(false);
    }
  };

  const handleOptimize = async () => {
    if (busyRef.current) return;
    setBusy(true);
    setApplyingAll(true);
    setErrors({});
    setOptimizeResult(null);
    try {
      const result = await backend.optimizeSafe();
      setOptimizeResult(result);
      if (result.results) {
        const newErrors: Record<string, string> = {};
        for (const [k, v] of Object.entries(result.results)) {
          if (!v.success) {
            const det = v.detail ? ` (${v.detail})` : "";
            newErrors[k] = (v.message ?? ERROR_MESSAGES[v.error ?? ""] ?? "Failed") + det;
          }
        }
        setErrors(newErrors);
      }
      // optimize_safe contains BSSID lock which reconnects
      if (result.reconnected) {
        await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
      }
    } catch (e) {
      console.error("optimize error", e);
    } finally {
      // Refresh before clearing applyingAll so button doesn't briefly flip to
      // "All good" from stale status. Refresh runs on errors too.
      await refreshStatus(true);
      setBusy(false);
      setApplyingAll(false);
    }
  };

  const handleForceReapply = () =>
    handleToggle("reapply", () => backend.reapplyAll());

  const handleResetSettings = async () => {
    if (busyRef.current) return;
    setBusy(true);
    setOptimizeResult(null);
    setErrors({});
    try {
      await backend.resetSettings();
    } finally {
      await refreshStatus(true);
      setBusy(false);
    }
  };

  const handleBackendToggle = async (on: boolean) => {
    // Re-entrancy guard: drop rapid clicks or clicks that land while another
    // operation is already running. Protects against duplicate backend calls
    // and the Force-Reapply/backend-switch overlap case.
    if (busyRef.current) return;
    const target = on ? "wpa_supplicant" : "iwd";
    setBusy(true);
    setErrors((prev) => {
      const next = { ...prev };
      delete next.wifi_backend;
      return next;
    });
    setOptimizeResult(null);

    try {
      const res = await backend.startBackendSwitch(target);
      if (!res.accepted) {
        setErrors((prev) => ({
          ...prev,
          wifi_backend: res.message ?? "Could not start backend switch",
        }));
        // Clear any stale result banner/inline from a prior switch so we don't
        // render old success + new error side-by-side.
        setBackendSwitch(null);
        setBusy(false);
        return;
      }
      setBackendSwitch({
        success: true,
        in_progress: true,
        phase: "switching",
        target,
        started_at: Math.floor(Date.now() / 1000),
        result: null,
      });
      beginBackendPoll();
    } catch (e) {
      setBusy(false);
      setErrors((prev) => ({
        ...prev,
        wifi_backend: "Failed to start backend switch",
      }));
      console.error("startBackendSwitch error", e);
    }
  };

  // Update-flow handlers, used by the top update banner and the Updates
  // section. Share the updating/updateError state so either surface can
  // initiate a check or apply an update.
  const handleApplyUpdate = async () => {
    setUpdateError(null);
    setUpdating(true);
    try {
      await backend.applyUpdate();
    } catch {
      // plugin_loader restart killed the connection; expected
    }
  };

  const handleCheckForUpdate = async () => {
    setCheckingUpdate(true);
    setUpdateError(null);
    lastUpdateCheckAtRef.current = Date.now();
    try {
      const result = await backend.checkForUpdate();
      setUpdateInfo(result);
    } catch {
      // refreshStatus has its own catch; no need to log here
    }
    setCheckingUpdate(false);
  };

  const handleChannelChange = async (nextChannel: string) => {
    await backend.setUpdateChannel(nextChannel);
    setUpdateInfo(null);
    await refreshStatus();
  };

  // Don't render content until first status arrives (prevents disconnect flash)
  if (!status) {
    return <PanelSection title="WiFi Optimizer" />;
  }

  const s = status?.settings;
  const connected = status?.connected ?? false;
  const supportTier = status?.support_tier ?? 1;
  const driftCount = status?.drift ? Object.keys(status.drift).length : 0;
  const supports6GHz = s?.supports_6ghz ?? false;
  const isDeckLcd = s?.device_family === "deck_lcd";

  // Check if all safe optimizations are already active
  const allSafeActive =
    connected &&
    status?.live?.power_save_off &&
    !status?.drift?.power_save &&
    s?.bssid_lock_enabled &&
    !status?.drift?.bssid_lock &&
    status?.live?.dispatcher_installed &&
    status?.live?.buffer_tuning_applied &&
    !status?.drift?.buffer_tuning;

  return (
    <>
      <PanelHeader
        driver={s?.driver ?? ""}
        version={status?.version ?? "?"}
        lastApplied={s?.last_applied ?? 0}
        lastEnforced={status?.live?.last_enforced}
        deviceLabel={s?.device_label !== "Unknown Device"
          ? `${s?.device_label} - ${s?.chip_label}`
          : s?.chip_label && s.chip_label !== "unknown"
            ? s.chip_label
            : undefined}
      />

      {/* Unknown hardware info */}
      {supportTier === 3 && (
        <Banner variant="info">
          Unknown WiFi hardware detected. Some optimizations may not apply.
        </Banner>
      )}

      {/* Update available */}
      {updateInfo?.update_available && !updating && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={handleApplyUpdate}>
              Update to v{updateInfo.latest_version}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Updating */}
      {updating && (
        <PanelSection>
          <PanelSectionRow>
            <div style={{ fontSize: theme.fontSize.body, color: theme.info.text }}>
              Updating... plugin will restart momentarily.
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* First-run prompt */}
      {connected && !s?.last_applied && (
        <Banner variant="info">
          Tap <strong>Optimize Safe</strong> to get started.
        </Banner>
      )}

      {/* Drift alert */}
      {connected && !!s?.last_applied && driftCount > 0 && (
        <Banner variant="warning" icon="⚠">
          {driftCount} setting{driftCount > 1 ? "s" : ""} drifted after wake.{" "}
          <span
            style={{ textDecoration: "underline", cursor: "pointer" }}
            onClick={handleOptimize}
          >
            Fix now
          </span>
        </Banner>
      )}

      {/* Disconnected banner */}
      {!connected && (
        <Banner variant="error" icon="✕">
          Not connected to WiFi. Connect first, then optimize.
        </Banner>
      )}

      {/* Backend switch result banner */}
      {backendSwitch && !backendSwitch.in_progress && backendSwitch.result && (() => {
        const r = backendSwitch.result;
        // Treat reconnect timeout as a warning even when the backend-level
        // switch succeeded - the system is switched but WiFi didn't come back.
        const isWarning = !r.success || r.needs_reboot || r.reconnect_timed_out;
        let text: string;
        if (r.needs_reboot) {
          text = `Backend switched to ${r.target}, but wlan0 didn't come back - reboot required`;
        } else if (!r.success) {
          text = r.message ?? "Backend switch failed";
        } else {
          const parts = [`Switched to ${r.backend}`];
          if (r.recovery_performed) parts.push("recreated wlan0 interface");
          if (r.reconnect_timed_out) parts.push("WiFi didn't reconnect");
          text = parts.join(" · ");
        }
        return (
          <Banner variant={isWarning ? "warning" : "success"} icon={isWarning ? "⚠" : "✓"}>
            {text}
          </Banner>
        );
      })()}

      {/* Optimize result banner */}
      {optimizeResult && (
        <Banner
          variant={
            optimizeResult.applied === optimizeResult.total ? "success" : "warning"
          }
        >
          {optimizeResult.applied === optimizeResult.total
            ? "All applied"
            : `${optimizeResult.applied}/${optimizeResult.total} applied`}
        </Banner>
      )}

      {/* Optimize button */}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!connected || applyingAll || isBusy}
            onClick={handleOptimize}
          >
            {applyingAll
              ? "Applying..."
              : allSafeActive
                ? "All good"
                : "Optimize Safe"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* Power & stability */}
      <PanelSection title="Power & stability">
        <InfoRow
          label="Prevent lag spikes"
          subtitle="Disables WiFi power save and PCIe power states"
          explanation="The OS enables WiFi power saving at multiple levels - the wireless chip, the PCIe bus connecting it to the CPU, and driver-level low power modes. These cause latency spikes, packet batching, and throughput degradation during sustained streaming. This toggle disables all of them, keeping the WiFi hardware fully awake. Battery impact is minimal."
          {...getBadge("power_save", status, errors.power_save ?? null)}
          checked={s?.power_save_disabled ?? false}
          disabled={isBusy}
          error={errors.power_save}
          onChange={(val: boolean) =>
            handleToggle("power_save", () => backend.setPowerSave(val))
          }
        />
        <InfoRow
          label="Stop background scanning"
          subtitle="Locks to current AP - disable to switch networks or roam"
          explanation="Your device scans for other WiFi networks every few minutes even while connected. Each scan causes a brief interruption that can drop packets and stutter game streaming. Locking to your current access point stops these scans entirely. You'll need to disable this before switching to a different network or access point."
          {...getBadge("bssid_lock", status, errors.bssid_lock ?? null)}
          checked={s?.bssid_lock_enabled ?? false}
          disabled={isBusy || (!connected && !s?.bssid_lock_enabled)}
          error={errors.bssid_lock}
          onChange={(val: boolean) =>
            handleToggle("bssid_lock", () => backend.setBssidLock(val))
          }
        />
        <InfoRow
          label="Auto-fix on wake"
          subtitle="Reapplies settings after sleep (NM dispatcher)"
          explanation="The OS often resets WiFi settings after sleep or updates. This installs a small script that automatically re-applies your optimizations every time the WiFi reconnects. It runs outside of Decky, so it works even if Decky has issues. Removing the plugin will also remove this script."
          {...getBadge(undefined, status, errors.auto_fix ?? null)}
          checked={s?.auto_fix_on_wake ?? false}
          disabled={isBusy}
          error={errors.auto_fix}
          onChange={(val: boolean) =>
            handleToggle("auto_fix", () => backend.setAutoFix(val))
          }
        />
        <InfoRow
          label="Network buffer tuning"
          subtitle="Optimize UDP buffers and TX queue for streaming"
          explanation="Increases kernel network buffer sizes and transmit queue length to handle the bursty UDP traffic that game streaming produces. Without this, packets can be dropped during high-bitrate moments, causing frame drops or brief quality dips. These settings benefit all network interfaces, including ethernet. They reset on every reboot."
          {...getBadge("buffer_tuning", status, errors.buffer_tuning ?? null)}
          checked={s?.buffer_tuning_enabled ?? false}
          disabled={isBusy}
          error={errors.buffer_tuning}
          onChange={(val: boolean) =>
            handleToggle("buffer_tuning", () => backend.setBufferTuning(val))
          }
        />
        <InfoRow
          label="Traffic shaping (CAKE)"
          subtitle="Reduces bufferbloat and jitter under load"
          explanation="CAKE manages outgoing network queues to prevent bufferbloat - the latency spikes that happen when your device sends bursts of data alongside game traffic. It prioritizes latency-sensitive packets over bulk transfers. Most useful when other devices share your WiFi or background downloads are running."
          {...getBadge("cake", status, errors.cake ?? null)}
          checked={s?.cake_enabled ?? false}
          disabled={isBusy}
          error={errors.cake}
          onChange={(val: boolean) =>
            handleToggle("cake", () => backend.setCake(val))
          }
        />
      </PanelSection>

      {/* Advanced */}
      <PanelSection title="Advanced">
        <InfoRow
          label={supports6GHz ? "Force 5 GHz / 6 GHz" : "Force 5 GHz band"}
          subtitle="Avoid 2.4 GHz Bluetooth interference"
          explanation={`Bluetooth operates on the 2.4 GHz band${
            isDeckLcd ? ", and on the Steam Deck LCD the antennas are shared" : ""
          }. Using 5 GHz${
            supports6GHz ? " or 6 GHz" : ""
          } for WiFi avoids this interference entirely, giving you a cleaner, faster connection. Only enable this if your router supports 5 GHz. If your network is 2.4 GHz only, this will prevent you from connecting.`}
          {...getBadge(undefined, status, errors.band_preference ?? null)}
          checked={s?.band_preference_enabled ?? false}
          disabled={isBusy || (!connected && !s?.band_preference_enabled)}
          error={errors.band_preference}
          onChange={(val: boolean) =>
            handleToggle("band_preference", () =>
              backend.setBandPreference(val, s?.band_preference ?? "a")
            )
          }
        />
        <InfoRow
          label="Custom DNS"
          subtitle="Override DNS servers for this network"
          explanation="Your internet provider's DNS servers translate domain names into IP addresses. They can be slow or unreliable. Switching to a public DNS like Cloudflare (1.1.1.1) or Google (8.8.8.8) can speed up initial connections and improve reliability. This only affects the current WiFi network."
          {...getBadge(undefined, status, errors.dns ?? null)}
          checked={s?.dns_enabled ?? false}
          disabled={isBusy || (!connected && !s?.dns_enabled)}
          error={errors.dns}
          onChange={(val: boolean) =>
            handleToggle("dns", () =>
              backend.setDns(val, s?.dns_provider ?? "cloudflare", customDnsInput)
            )
          }
        >
          {s?.dns_enabled && (
            <>
              <PanelSectionRow>
                <DropdownItem
                  label="DNS Provider"
                  rgOptions={DNS_OPTIONS}
                  selectedOption={s?.dns_provider ?? "cloudflare"}
                  onChange={(option: { data: string }) => {
                    const custom = option.data === "custom" ? customDnsInput : "";
                    handleToggle("dns", () =>
                      backend.setDns(true, option.data, custom)
                    );
                  }}
                />
              </PanelSectionRow>
              {s?.dns_provider === "custom" && (
                <PanelSectionRow>
                  <TextField
                    label="DNS servers (space-separated)"
                    value={customDnsInput}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                      setCustomDnsInput(e.target.value)
                    }
                    onBlur={() => {
                      if (customDnsInput) {
                        handleToggle("dns", () =>
                          backend.setDns(true, "custom", customDnsInput)
                        );
                      }
                    }}
                  />
                </PanelSectionRow>
              )}
            </>
          )}
        </InfoRow>
        <InfoRow
          label="Disable IPv6"
          subtitle="Use IPv4 only on this network"
          explanation="Some networks have poor or misconfigured IPv6 support, which can cause slow DNS resolution, connection timeouts, or routing issues. Disabling IPv6 forces all traffic through IPv4. Only enable this if you're experiencing issues - most modern networks handle IPv6 fine."
          {...getBadge(undefined, status, errors.ipv6 ?? null)}
          checked={s?.ipv6_disabled ?? false}
          disabled={isBusy || (!connected && !s?.ipv6_disabled)}
          error={errors.ipv6}
          onChange={(val: boolean) =>
            handleToggle("ipv6", () => backend.setIpv6(val))
          }
        />
        {status?.live?.backend_tool_available && (
          <BackendToggleRow
            status={status}
            backendSwitch={backendSwitch}
            error={errors.wifi_backend}
            isBusy={isBusy}
            onToggle={handleBackendToggle}
          />
        )}
      </PanelSection>

      {/* Live status */}
      <PanelSection title="Live status">
        {connected && status?.live?.ip_address && (
          <PanelSectionRow>
            <div style={{ fontSize: theme.fontSize.tiny, color: theme.text.tertiary }}>
              IP: {status.live.ip_address}
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <StatsGrid live={status?.live ?? {}} connected={connected} />
        </PanelSectionRow>
      </PanelSection>

      <ActionsSection
        connected={connected}
        isBusy={isBusy}
        onForceReapply={handleForceReapply}
        onReset={handleResetSettings}
      />

      <UpdatesSection
        channel={s?.update_channel ?? "stable"}
        updating={updating}
        checkingUpdate={checkingUpdate}
        updateInfo={updateInfo}
        updateError={updateError}
        onChannelChange={handleChannelChange}
        onApply={handleApplyUpdate}
        onCheck={handleCheckForUpdate}
      />

      <PanelFooter version={status?.version ?? "?"} />
    </>
  );
}

export default definePlugin(() => {
  return {
    name: "WiFi Optimizer",
    titleView: <div className={staticClasses.Title}>WiFi Optimizer</div>,
    content: (
      <ErrorBoundary>
        <Content />
      </ErrorBoundary>
    ),
    icon: <FaWifi />,
    onDismount() {},
  };
});
