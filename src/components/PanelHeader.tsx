import { PanelSection, PanelSectionRow } from "@decky/ui";
import { theme } from "../theme";
import { timeAgo } from "../utils";

interface PanelHeaderProps {
  model: string;
  driver: string;
  version: string;
  lastApplied: number;
  lastEnforced?: number;
}

// Top-of-panel identification: device chip, plugin version, hint, and the
// two timestamps that tell the user when their settings were last touched
// (either by them via a toggle or by the dispatcher on a WiFi reconnect).
export function PanelHeader({
  model,
  driver,
  version,
  lastApplied,
  lastEnforced,
}: PanelHeaderProps) {
  const modelLabel = `${(model || "unknown").toUpperCase()} - ${driver || "?"}`;
  const rowStyle: React.CSSProperties = {
    fontSize: theme.fontSize.tiny,
    color: theme.text.muted,
  };
  return (
    <PanelSection>
      <PanelSectionRow>
        <span
          style={{
            fontSize: theme.fontSize.tiny,
            background: theme.surface.md,
            padding: "2px 8px",
            borderRadius: theme.radius.pill,
            color: theme.text.tertiary,
          }}
        >
          Device: {modelLabel}
        </span>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={rowStyle}>Version: {version}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={rowStyle}>Tap (i) on any toggle for details</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={rowStyle}>
          Last changed: {timeAgo(lastApplied)}
          {lastEnforced ? (
            <>
              <br />
              Auto-applied: {timeAgo(lastEnforced)}
            </>
          ) : (
            ""
          )}
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
}
