import { PanelSectionRow } from "@decky/ui";
import type {
  BadgeStatus,
  BackendSwitchPhase,
  BackendSwitchStatus,
  PluginStatus,
} from "../types";
import { InfoRow } from "./InfoRow";
import { theme } from "../theme";

const PHASE_TEXT: Record<BackendSwitchPhase, string> = {
  idle: "",
  switching: "Switching backend…",
  reconnecting: "Reconnecting…",
  done: "",
  failed: "",
};

const EXPLANATION =
  "Your system defaults to iwd for WiFi management. Some devices see " +
  "disconnects after sleep, 5 GHz dropouts, or 'invalid password' errors " +
  "with iwd. Switching to wpa_supplicant trades slightly slower reconnect " +
  "(about 5s vs 1-2s) for broader compatibility and better stability on " +
  "certain routers. The setting survives reboots and OS updates. On some " +
  "devices, the switch may briefly disrupt the WiFi interface - the plugin " +
  "handles recovery automatically, but a reboot is needed as a last resort. " +
  "Note: some networks (WPA3-only, certain enterprise setups) behave " +
  "differently between backends - if your WiFi stops connecting after a " +
  "switch, try switching back.";

interface BackendToggleRowProps {
  status: PluginStatus;
  backendSwitch: BackendSwitchStatus | null;
  error: string | undefined;
  isBusy: boolean;
  onToggle: (on: boolean) => void;
}

export function BackendToggleRow({
  status,
  backendSwitch,
  error,
  isBusy,
  onToggle,
}: BackendToggleRowProps) {
  const currentBackend = status.live?.wifi_backend || "iwd";
  const isWpa = currentBackend === "wpa_supplicant";
  const switching = backendSwitch?.in_progress ?? false;

  // Optimistic: during a switch, reflect the target so the toggle matches
  // the user's click until the operation completes. On failure, it snaps
  // back to the actual backend.
  const checked = switching && backendSwitch?.target
    ? backendSwitch.target === "wpa_supplicant"
    : isWpa;

  const phaseText = switching
    ? PHASE_TEXT[backendSwitch!.phase] || "Working…"
    : null;

  const badge: { badge: BadgeStatus; text: string } = error
    ? { badge: "error", text: "failed" }
    : switching
      ? { badge: "unknown", text: "…" }
      : isWpa
        ? { badge: "active", text: "wpa_supplicant" }
        : { badge: "off", text: "iwd" };

  // Inline result shown right under the toggle so it's visible where the
  // user clicked - the top-of-panel banner is often off-screen when the
  // user is scrolled down to the Advanced section.
  const lastResult =
    !switching && !error && backendSwitch?.result && !backendSwitch.result.needs_reboot
      ? backendSwitch.result
      : null;

  return (
    <InfoRow
      label="Use wpa_supplicant backend"
      subtitle={phaseText ?? "Alternate WiFi backend - can fix sleep/wake and stability issues"}
      explanation={EXPLANATION}
      badge={badge.badge}
      text={badge.text}
      checked={checked}
      disabled={switching || isBusy}
      error={error}
      onChange={onToggle}
    >
      {lastResult?.success && (() => {
        const timedOut = lastResult.reconnect_timed_out;
        const parts: string[] = [`Switched to ${lastResult.backend}`];
        if (lastResult.recovery_performed) parts.push("wlan0 interface recreated");
        if (timedOut) parts.push("WiFi didn't reconnect");
        return (
          <PanelSectionRow>
            <div
              style={{
                fontSize: theme.fontSize.small,
                color: timedOut ? theme.warning.text : theme.success.text,
                padding: "2px 0",
              }}
            >
              {timedOut ? "⚠" : "✓"} {parts.join(" · ")}
            </div>
          </PanelSectionRow>
        );
      })()}
    </InfoRow>
  );
}
