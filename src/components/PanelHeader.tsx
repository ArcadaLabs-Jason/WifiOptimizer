import { PanelSection, PanelSectionRow } from "@decky/ui";
import { theme } from "../theme";
import { timeAgo } from "../utils";

interface PanelHeaderProps {
  driver: string;
  version: string;
  lastApplied: number;
  lastEnforced?: number;
  deviceLabel?: string;
}

export function PanelHeader({
  driver,
  version,
  lastApplied,
  lastEnforced,
  deviceLabel,
}: PanelHeaderProps) {
  const modelLabel = deviceLabel && deviceLabel !== "unknown"
    ? deviceLabel
    : driver && driver !== "unknown"
      ? driver
      : "Unrecognized hardware";
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
