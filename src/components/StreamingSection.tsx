import { useEffect, useState } from "react";
import {
  PanelSection,
  PanelSectionRow,
  ToggleField,
  TextField,
  ButtonItem,
} from "@decky/ui";
import type { LiveStatus, MethodResult, PluginSettings, StreamingApp } from "../types";
import * as backend from "../backend";
import { InfoRow, MessageRow } from "./InfoRow";
import { theme } from "../theme";

interface StreamingSectionProps {
  settings: PluginSettings | undefined;
  live: LiveStatus | undefined;
  disabled: boolean;
  // The panel's errors and notices, by key: "streaming" for the mode itself,
  // "streaming_hold", "streaming_apps" and "streaming_patterns" for the
  // controls below it, each shown under its own control.
  errors: Record<string, string>;
  notices: Record<string, string>;
  // The panel's own toggle handler, so busy state, errors and the status
  // refresh behave as they do for every other toggle.
  onToggle: (key: string, fn: () => Promise<MethodResult>) => Promise<MethodResult | undefined>;
}

// Auto mode never keeps the driver options file (main.py set_power_save,
// persist=not auto, and _drop_persistent_power_save). These are the
// DRIVER_PROFILES keys with modprobe_options (regress.py ties the two):
// rtw88 and rtw89 (Realtek), mt7921e (MediaTek), iwlwifi (Intel). On rtw88
// the parameters are read at runtime and come off between streams (except
// the CLKREQ part of disable_aspm, set only when the driver loads), and on
// the Steam Deck LCD they are the ones that help WiFi come back after
// sleep. On the others they only take effect when the driver loads (C19).
export const LOAD_TIME_DRIVERS = ["rtw88", "rtw89", "mt7921e", "iwlwifi"];
const SLEEP_HELP_DRIVER = "rtw88";
const SLEEP_HELP_AUTO_TEXT =
  " In auto mode, the driver settings that help WiFi come back after sleep are off between streams.";
const LOAD_TIME_AUTO_TEXT =
  " In auto mode, driver settings that only apply when the driver loads are not in effect.";

// What auto mode costs on this card's driver, if anything.
function autoModeDriverText(driver: string): string {
  if (driver === SLEEP_HELP_DRIVER) return SLEEP_HELP_AUTO_TEXT;
  return LOAD_TIME_DRIVERS.includes(driver) ? LOAD_TIME_AUTO_TEXT : "";
}

// Streaming auto mode: when the fixes apply, rather than another fix. Placed
// after Advanced for that reason. Most of what is here is about making it
// obvious which state you are in, because fixes that quietly stopped
// applying are worse than fixes left on.
export function StreamingSection({
  settings,
  live,
  disabled,
  errors,
  notices,
  onToggle,
}: StreamingSectionProps) {
  const enabled = settings?.streaming_mode_enabled ?? false;
  const [apps, setApps] = useState<StreamingApp[]>([]);
  const [patterns, setPatterns] = useState(settings?.streaming_custom_patterns ?? "");
  const [patternsDirty, setPatternsDirty] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    backend
      .listStreamingApps()
      .then((r) => r.success && setApps(r.apps))
      .catch(() => {});
  }, [enabled]);

  // Follow the saved value unless something is being typed.
  useEffect(() => {
    if (!patternsDirty) setPatterns(settings?.streaming_custom_patterns ?? "");
  }, [settings?.streaming_custom_patterns, patternsDirty]);

  const toggleApp = async (id: string, watched: boolean) => {
    setApps((prev) => prev.map((a) => (a.id === id ? { ...a, watched } : a)));
    const result = await onToggle("streaming_apps", () => backend.setStreamingApp(id, watched));
    if (!result?.success) {
      backend.listStreamingApps().then((r) => r.success && setApps(r.apps)).catch(() => {});
    }
  };

  // Back to what is saved, and following it again.
  const cancelPatterns = () => {
    setPatternsDirty(false);
    setPatterns(settings?.streaming_custom_patterns ?? "");
  };

  const savePatterns = async () => {
    const result = await onToggle("streaming_patterns", () => backend.setStreamingPatterns(patterns));
    if (result?.success) {
      setPatternsDirty(false);
      setPatterns(String(result.patterns ?? ""));
    }
  };

  const active = live?.streaming_active ?? false;
  const held = live?.streaming_held ?? false;
  const app = live?.streaming_app ?? "";
  const process = live?.streaming_process ?? "";
  // The fixes this mode governs that are turned on, by their names above.
  const governed = [
    settings?.power_save_disabled ? "Prevent lag spikes" : "",
    settings?.buffer_tuning_enabled ? "Network buffer tuning" : "",
    settings?.cake_enabled ? "Traffic shaping (CAKE)" : "",
  ].filter(Boolean);
  const governedList =
    governed.length > 1
      ? `${governed.slice(0, -1).join(", ")} and ${governed[governed.length - 1]}`
      : governed[0] ?? "";
  // With none of them on, a stream has nothing to turn on either.
  const fixesText = governed.length ? " Fixes are on." : " No fixes are turned on.";
  let stateText: string;
  if (active && app) {
    stateText = `Streaming: ${app} (${process}).${fixesText}`;
  } else if (active && held) {
    const until = "until you turn off Keep fixes on or restart the device.";
    stateText = governed.length ? `Held on by you. Fixes are on ${until}` : `Held on by you ${until}${fixesText}`;
  } else if (active) {
    // Flagged as running before the app has been named, just after a reload.
    stateText = `A stream is running.${fixesText}`;
  } else if (governed.length) {
    stateText = `No streaming app running. ${governedList} ${
      governed.length > 1 ? "are" : "is"
    } paused until one starts.`;
  } else {
    stateText = "No streaming app running. No fixes are turned on.";
  }

  return (
    <PanelSection title="Streaming auto mode">
      <InfoRow
        label="Only fix while streaming"
        subtitle={
          enabled
            ? "Fixes apply while a streaming app runs"
            : "Off: enabled fixes apply all the time"
        }
        explanation={`Applies Prevent lag spikes, Network buffer tuning and Traffic shaping (CAKE) only while a streaming app is running, and takes them off afterwards to save battery. Background scanning, band, DNS and IPv6 stay as they are, because changing them reconnects WiFi and would drop the stream.${autoModeDriverText(
          settings?.driver ?? "",
        )} If a stream is not picked up, turn on Keep fixes on, or add the app's process name below. Idea and first version by bassobr.`}
        checked={enabled}
        disabled={disabled}
        error={errors.streaming}
        notice={notices.streaming}
        onChange={(val: boolean) => onToggle("streaming", () => backend.setStreamingMode(val))}
      />
      {enabled && (
        <>
          <PanelSectionRow>
            <div
              style={{
                fontSize: theme.fontSize.body,
                color: active ? theme.success.text : theme.info.text,
              }}
            >
              {stateText}
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="Keep fixes on"
              description="For a stream that is not detected. Lasts until you restart the device."
              checked={held}
              disabled={disabled}
              onChange={(val: boolean) =>
                onToggle("streaming_hold", () => backend.setStreamingHold(val))
              }
            />
          </PanelSectionRow>
          <MessageRow error={errors.streaming_hold} notice={notices.streaming_hold} />
          {apps.map((a) => (
            <PanelSectionRow key={a.id}>
              <ToggleField
                label={a.label}
                checked={a.watched}
                disabled={disabled}
                onChange={(val: boolean) => toggleApp(a.id, val)}
              />
            </PanelSectionRow>
          ))}
          <MessageRow error={errors.streaming_apps} notice={notices.streaming_apps} />
          <PanelSectionRow>
            <TextField
              label="Other process names (space-separated)"
              value={patterns}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
                setPatternsDirty(true);
                setPatterns(e.target.value);
              }}
            />
          </PanelSectionRow>
          {patternsDirty && (
            <>
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={disabled} onClick={savePatterns}>
                  Save process names
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={disabled} onClick={cancelPatterns}>
                  Cancel
                </ButtonItem>
              </PanelSectionRow>
            </>
          )}
          {/* Under the Save button, and still there once it has gone: a
              name that was refused is said beside the field it is in. */}
          <MessageRow error={errors.streaming_patterns} notice={notices.streaming_patterns} />
        </>
      )}
    </PanelSection>
  );
}
