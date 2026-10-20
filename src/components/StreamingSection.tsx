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
import { InfoRow } from "./InfoRow";
import { theme } from "../theme";

interface StreamingSectionProps {
  settings: PluginSettings | undefined;
  live: LiveStatus | undefined;
  disabled: boolean;
  error?: string;
  notice?: string;
  // The panel's own toggle handler, so busy state, errors and the status
  // refresh behave as they do for every other toggle.
  onToggle: (key: string, fn: () => Promise<MethodResult>) => Promise<MethodResult | undefined>;
}

// Streaming auto mode: when the fixes apply, rather than another fix. Placed
// after Advanced for that reason. Most of what is here is about making it
// obvious which state you are in, because fixes that quietly stopped
// applying are worse than fixes left on.
export function StreamingSection({
  settings,
  live,
  disabled,
  error,
  notice,
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
    const result = await onToggle("streaming", () => backend.setStreamingApp(id, watched));
    if (!result?.success) {
      backend.listStreamingApps().then((r) => r.success && setApps(r.apps)).catch(() => {});
    }
  };

  const savePatterns = async () => {
    const result = await onToggle("streaming", () => backend.setStreamingPatterns(patterns));
    if (result?.success) {
      setPatternsDirty(false);
      setPatterns(String(result.patterns ?? ""));
    }
  };

  const active = live?.streaming_active ?? false;
  const held = live?.streaming_held ?? false;
  const app = live?.streaming_app ?? "";
  const process = live?.streaming_process ?? "";
  let stateText: string;
  if (active && app) {
    stateText = `Streaming: ${app} (${process}). Fixes are on.`;
  } else if (active && held) {
    stateText = "Held on by you. Fixes are on until you turn this off or restart.";
  } else {
    stateText =
      "No streaming app running. Power save, buffer tuning and CAKE are paused until one starts.";
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
        explanation="Applies Prevent lag spikes, Network buffer tuning and Traffic shaping (CAKE) only while a streaming app is running, and takes them off afterwards to save battery. Background scanning, band, DNS and IPv6 stay as they are, because changing them reconnects WiFi and would drop the stream. If a stream is not picked up, turn on Keep fixes on, or add the app's process name below. Idea and first version by bassobr."
        checked={enabled}
        disabled={disabled}
        error={error}
        notice={notice}
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
              {active ? "● " : "○ "}
              {stateText}
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="Keep fixes on"
              description="For a stream that is not detected. Lasts until you restart."
              checked={held}
              disabled={disabled}
              onChange={(val: boolean) =>
                onToggle("streaming", () => backend.setStreamingHold(val))
              }
            />
          </PanelSectionRow>
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
            <PanelSectionRow>
              <ButtonItem layout="below" disabled={disabled} onClick={savePatterns}>
                Save process names
              </ButtonItem>
            </PanelSectionRow>
          )}
        </>
      )}
    </PanelSection>
  );
}
