import { PanelSectionRow } from "@decky/ui";
import type {
  BadgeStatus,
  BackendSwitchPhase,
  BackendSwitchResult,
  BackendSwitchStatus,
  PluginStatus,
} from "../types";
import { InfoRow } from "./InfoRow";
import { theme } from "../theme";

const PHASE_TEXT: Record<BackendSwitchPhase, string> = {
  idle: "",
  switching: "Switching backend...",
  reconnecting: "Reconnecting...",
  done: "",
  failed: "",
};

const EXPLANATION =
  "Linux handhelds use one of two WiFi backends, and which one your " +
  "system is on varies by device and OS version. Some devices see " +
  "disconnects after sleep, 5 GHz dropouts, or 'invalid password' errors " +
  "with iwd. Switching to wpa_supplicant trades a slightly slower reconnect " +
  "(about 5 seconds instead of 1 or 2) for broader compatibility and better " +
  "stability on some routers. The setting survives reboots and OS updates. " +
  "Switching takes WiFi down for up to a minute. On some devices the WiFi " +
  "card has to be restarted along the way, which the plugin does by itself, " +
  "and if that doesn't work a restart of the device finishes the switch. " +
  "Some networks (WPA3-only, some work or school networks) behave " +
  "differently between the two. If WiFi stops connecting after a switch, " +
  "switch back.";

// What a finished switch says, in the banner at the top of the panel and
// under the toggle. A failure is checked first: a switch that failed and
// left no WiFi card also asks for a restart, and was announced as
// "Switched to".
export function backendResultText(r: BackendSwitchResult): string {
  if (!r.success) {
    // The backend's own words say whether the switch itself took; this
    // fallback is true either way.
    if (r.needs_reboot) {
      return r.message ?? "WiFi didn't come back after the backend switch. Restart the device.";
    }
    return r.message ?? "The backend switch didn't work. If WiFi doesn't come back, restart the device.";
  }
  if (r.needs_reboot) {
    return `Switched to ${r.backend || r.target}, but the WiFi card didn't come back. Restart the device to finish.`;
  }
  const parts = [`Switched to ${r.backend || r.target}.`];
  if (r.recovery_performed) parts.push("The WiFi card had to be restarted along the way.");
  if (r.reconnect_timed_out) {
    parts.push("WiFi hasn't reconnected yet. Pick your network in the WiFi settings, or switch back here.");
  }
  return parts.join(" ");
}

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
  // The backend is genuinely unknown when no config declares one and neither
  // service reports active. Defaulting the label to iwd states something we
  // have not established, on a machine that may well be on wpa_supplicant.
  const currentBackend = status.live?.wifi_backend || "";
  const isWpa = currentBackend === "wpa_supplicant";
  const switching = backendSwitch?.in_progress ?? false;

  // Optimistic: during a switch, reflect the target so the toggle matches
  // the user's click until the operation completes. On failure, it snaps
  // back to the actual backend.
  const checked = switching && backendSwitch?.target
    ? backendSwitch.target === "wpa_supplicant"
    : isWpa;

  const phaseText = switching
    ? PHASE_TEXT[backendSwitch!.phase] || "Working..."
    : null;

  // On the generic method a change made by Reset (or an uninstall and
  // reinstall without a reboot) lands at the next boot; until then the
  // backend running is the one shown, and this says what comes next.
  const next = status.live?.wifi_backend_next;
  const subtitle =
    phaseText ??
    (next && next !== currentBackend
      ? `Changes to ${next} from the next boot.`
      : isWpa
        ? "In use. Turn off to go back to iwd."
        : currentBackend === "iwd"
          ? "Instead of iwd. Can fix sleep and stability problems."
          : "Can fix sleep and stability problems.");

  const badge: { badge: BadgeStatus; text: string } = error
    ? { badge: "error", text: "failed" }
    : switching
      ? { badge: "unknown", text: "switching" }
      : isWpa
        ? { badge: "active", text: "wpa_supplicant" }
        : currentBackend === "iwd"
          ? { badge: "off", text: "iwd" }
          : { badge: "unknown", text: "unknown" };

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
      subtitle={subtitle}
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
        return (
          <PanelSectionRow>
            <div
              style={{
                fontSize: theme.fontSize.small,
                color: timedOut ? theme.warning.text : theme.success.text,
                padding: "2px 0",
              }}
            >
              {backendResultText(lastResult)}
            </div>
          </PanelSectionRow>
        );
      })()}
    </InfoRow>
  );
}
