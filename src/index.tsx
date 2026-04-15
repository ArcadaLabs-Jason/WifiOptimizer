import { useState, useEffect, useCallback, useRef } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  DropdownItem,
  TextField,
  staticClasses,
  Spinner,
} from "@decky/ui";
import { definePlugin } from "@decky/api";
import { FaWifi } from "react-icons/fa";

import * as backend from "./backend";
import type { PluginStatus, MethodResult, OptimizeSafeResult, BadgeStatus } from "./types";
import { ERROR_MESSAGES } from "./types";
import { InfoRow } from "./components/InfoRow";
import { StatsGrid } from "./components/StatsGrid";

const REFRESH_INTERVAL = 3000;
const RECONNECT_DELAY = 4000;

function timeAgo(ts: number): string {
  if (!ts) return "never";
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function getBadge(
  enabled: boolean,
  driftKey: string | undefined,
  status: PluginStatus | null,
  errorKey: string | null,
  activeText: string = "active"
): { badge: BadgeStatus; text: string } {
  if (errorKey) return { badge: "error", text: "failed" };
  if (!status?.connected) return { badge: "unknown", text: "?" };
  if (!enabled) return { badge: "off", text: "off" };
  if (driftKey && status.drift?.[driftKey]) return { badge: "drifted", text: "drifted" };
  return { badge: "active", text: activeText };
}

const DNS_OPTIONS = [
  { data: "cloudflare", label: "Cloudflare (1.1.1.1)" },
  { data: "google", label: "Google (8.8.8.8)" },
  { data: "quad9", label: "Quad9 (9.9.9.9)" },
  { data: "custom", label: "Custom" },
];

function Content() {
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [applyingAll, setApplyingAll] = useState(false);
  const [optimizeResult, setOptimizeResult] = useState<OptimizeSafeResult | null>(null);
  const [customDnsInput, setCustomDnsInput] = useState("");
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const busyRef = useRef(false);

  const refreshStatus = useCallback(async () => {
    if (busyRef.current) return;
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

  useEffect(() => {
    refreshStatus().finally(() => setLoading(false));
    intervalRef.current = setInterval(refreshStatus, REFRESH_INTERVAL);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [refreshStatus]);

  const handleToggle = async (key: string, fn: () => Promise<MethodResult>) => {
    busyRef.current = true;
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
      busyRef.current = false;
    }
    await refreshStatus();
  };

  const handleOptimize = async () => {
    busyRef.current = true;
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
      busyRef.current = false;
      setApplyingAll(false);
    }
    await refreshStatus();
  };

  if (loading) {
    return (
      <PanelSection title="WiFi Optimizer">
        <PanelSectionRow>
          <Spinner />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const s = status?.settings;
  const connected = status?.connected ?? false;
  const driftCount = status?.drift ? Object.keys(status.drift).length : 0;
  const isOled = s?.model === "oled";

  const modelLabel = s
    ? `${(s.model || "unknown").toUpperCase()} - ${s.driver || "?"}`
    : "";

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
      {/* Header */}
      <PanelSection>
        <PanelSectionRow>
          <span
            style={{
              fontSize: "10px",
              background: "rgba(255,255,255,0.06)",
              padding: "2px 8px",
              borderRadius: "10px",
              color: "#8a8a9a",
            }}
          >
            Device: {modelLabel}
          </span>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: "10px", color: "#6a6a7a" }}>
            Tap (i) on any toggle for details
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: "10px", color: "#6a6a7a" }}>
            Last changed: {timeAgo(s?.last_applied ?? 0)}
            {status?.live?.last_enforced ? (<><br />Auto-applied: {timeAgo(status.live.last_enforced)}</>) : ""}
          </div>
        </PanelSectionRow>
      </PanelSection>

      {/* First-run prompt */}
      {connected && !s?.last_applied && (
        <PanelSection>
          <PanelSectionRow>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                padding: "8px 12px",
                background: "rgba(55,138,221,0.08)",
                border: "0.5px solid rgba(55,138,221,0.2)",
                borderRadius: "8px",
                fontSize: "12px",
                color: "#60baff",
                width: "100%",
                boxSizing: "border-box",
              }}
            >
              <span>Tap <strong>Optimize Safe</strong> to get started.</span>
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Drift alert */}
      {connected && !!s?.last_applied && driftCount > 0 && (
        <PanelSection>
          <PanelSectionRow>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                padding: "8px 12px",
                background: "rgba(223,138,0,0.08)",
                border: "0.5px solid rgba(223,138,0,0.2)",
                borderRadius: "8px",
                fontSize: "12px",
                color: "#ffc669",
                width: "100%",
                boxSizing: "border-box",
              }}
            >
              <span style={{ fontSize: "14px" }}>&#9888;</span>
              <span>
                {driftCount} setting{driftCount > 1 ? "s" : ""} drifted after wake.{" "}
                <span
                  style={{ textDecoration: "underline", cursor: "pointer" }}
                  onClick={handleOptimize}
                >
                  Fix now
                </span>
              </span>
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Disconnected banner */}
      {!connected && (
        <PanelSection>
          <PanelSectionRow>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                padding: "8px 12px",
                background: "rgba(211,36,43,0.08)",
                border: "0.5px solid rgba(211,36,43,0.2)",
                borderRadius: "8px",
                fontSize: "12px",
                color: "#ff878c",
                width: "100%",
                boxSizing: "border-box",
              }}
            >
              <span style={{ fontSize: "14px" }}>&#10005;</span>
              <span>Not connected to WiFi. Connect first, then optimize.</span>
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Optimize result banner */}
      {optimizeResult && (
        <PanelSection>
          <PanelSectionRow>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: "6px",
                padding: "8px 12px",
                background:
                  optimizeResult.applied === optimizeResult.total
                    ? "rgba(29,158,117,0.08)"
                    : "rgba(223,138,0,0.08)",
                border: `0.5px solid ${
                  optimizeResult.applied === optimizeResult.total
                    ? "rgba(29,158,117,0.2)"
                    : "rgba(223,138,0,0.2)"
                }`,
                borderRadius: "8px",
                fontSize: "12px",
                color:
                  optimizeResult.applied === optimizeResult.total
                    ? "#3fc56e"
                    : "#ffc669",
                width: "100%",
                boxSizing: "border-box",
              }}
            >
              <span>
                {optimizeResult.applied === optimizeResult.total
                  ? "All applied"
                  : `${optimizeResult.applied}/${optimizeResult.total} applied`}
              </span>
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Optimize button */}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!connected || applyingAll}
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
          subtitle="Disables WiFi power save (iw power_save off)"
          explanation="SteamOS enables WiFi power saving by default, which lets the chip batch network packets and briefly sleep between transmissions. This saves a small amount of battery but causes latency spikes, micro-stutters in online games, and choppy streaming. Disabling it keeps the WiFi chip fully awake at all times. Battery impact is minimal."
          {...getBadge(s?.power_save_disabled ?? false, "power_save", status, errors.power_save ?? null)}
          checked={s?.power_save_disabled ?? false}
          error={errors.power_save}
          onChange={(val: boolean) =>
            handleToggle("power_save", () => backend.setPowerSave(val))
          }
        />
        <InfoRow
          label="Stop background scanning"
          subtitle="Locks to your current access point (BSSID lock)"
          explanation="Your Steam Deck scans for other WiFi networks every 2 minutes even while connected. Each scan causes a brief interruption that can drop packets and stutter game streaming. Locking to your current access point stops these scans entirely. You'll need to disable this before switching to a different network or access point."
          {...getBadge(s?.bssid_lock_enabled ?? false, "bssid_lock", status, errors.bssid_lock ?? null, "locked")}
          checked={s?.bssid_lock_enabled ?? false}
          disabled={!connected && !s?.bssid_lock_enabled}
          error={errors.bssid_lock}
          onChange={(val: boolean) =>
            handleToggle("bssid_lock", () => backend.setBssidLock(val))
          }
        />
        <InfoRow
          label="Auto-fix on wake"
          subtitle="Reapplies settings after sleep (NM dispatcher)"
          explanation="SteamOS often resets WiFi settings when the Deck wakes from sleep. This installs a small script that automatically re-applies your optimizations every time the WiFi reconnects. It runs outside of Decky, so it works even if Decky has issues. Removing the plugin will also remove this script."
          {...getBadge(s?.auto_fix_on_wake ?? false, undefined, status, errors.auto_fix ?? null)}
          checked={s?.auto_fix_on_wake ?? false}
          error={errors.auto_fix}
          onChange={(val: boolean) =>
            handleToggle("auto_fix", () => backend.setAutoFix(val))
          }
        />
        <InfoRow
          label="Network buffer tuning"
          subtitle="Optimize UDP buffers and TX queue for streaming"
          explanation="Increases kernel network buffer sizes and transmit queue length to handle the bursty UDP traffic that game streaming produces. Without this, packets can be dropped during high-bitrate moments, causing frame drops or brief quality dips. These settings reset on every reboot."
          {...getBadge(s?.buffer_tuning_enabled ?? false, "buffer_tuning", status, errors.buffer_tuning ?? null)}
          checked={s?.buffer_tuning_enabled ?? false}
          error={errors.buffer_tuning}
          onChange={(val: boolean) =>
            handleToggle("buffer_tuning", () => backend.setBufferTuning(val))
          }
        />
      </PanelSection>

      {/* Advanced */}
      <PanelSection title="Advanced">
        <InfoRow
          label={isOled ? "Prefer 5 GHz / 6 GHz" : "Prefer 5 GHz band"}
          subtitle="Avoid 2.4 GHz Bluetooth interference"
          explanation={`Bluetooth operates on the 2.4 GHz band${
            !isOled ? ", and on the LCD model the antennas are shared" : ""
          }. Using 5 GHz${
            isOled ? " or 6 GHz" : ""
          } for WiFi avoids this interference entirely, giving you a cleaner, faster connection. Only enable this if your router supports 5 GHz. If your network is 2.4 GHz only, this will prevent you from connecting.`}
          {...getBadge(
            s?.band_preference_enabled ?? false,
            undefined,
            status,
            errors.band_preference ?? null,
            "5 GHz"
          )}
          checked={s?.band_preference_enabled ?? false}
          disabled={!connected && !s?.band_preference_enabled}
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
          explanation="Your internet provider's DNS servers translate domain names (like store.steampowered.com) into IP addresses. They can be slow or unreliable. Switching to a public DNS like Cloudflare (1.1.1.1) or Google (8.8.8.8) can speed up initial connections and improve reliability. This only affects the current WiFi network."
          {...getBadge(s?.dns_enabled ?? false, undefined, status, errors.dns ?? null, "set")}
          checked={s?.dns_enabled ?? false}
          disabled={!connected && !s?.dns_enabled}
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
          {...getBadge(s?.ipv6_disabled ?? false, undefined, status, errors.ipv6 ?? null)}
          checked={s?.ipv6_disabled ?? false}
          disabled={!connected && !s?.ipv6_disabled}
          error={errors.ipv6}
          onChange={(val: boolean) =>
            handleToggle("ipv6", () => backend.setIpv6(val))
          }
        />
      </PanelSection>

      {/* Live status */}
      <PanelSection title="Live status">
        <PanelSectionRow>
          <StatsGrid live={status?.live ?? {}} connected={connected} />
        </PanelSectionRow>
      </PanelSection>

      {/* Actions */}
      <PanelSection title="Actions">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!connected}
            onClick={() => handleToggle("reapply", () => backend.reapplyAll())}
          >
            Force Reapply All
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={async () => {
              await backend.resetSettings();
              await refreshStatus();
            }}
          >
            Reset Settings
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* Footer */}
      <PanelSection>
        <PanelSectionRow>
          <div style={{ fontSize: "10px", color: "#4a4a5a" }}>
            v0.3.1 - by jasonridesabike
          </div>
        </PanelSectionRow>
        <PanelSectionRow>
          <div style={{ fontSize: "10px", color: "#4a4a5a" }}>
            If WiFi won't reconnect, a reboot usually fixes it.
            <br />
            Bugs? Report at github.com/ArcadaLabs-Jason/WifiOptimizer
          </div>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  return {
    name: "WiFi Optimizer",
    titleView: <div className={staticClasses.Title}>WiFi Optimizer</div>,
    content: <Content />,
    icon: <FaWifi />,
    onDismount() {},
  };
});
