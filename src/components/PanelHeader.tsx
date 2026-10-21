import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { theme } from "../theme";
import { timeAgo } from "../utils";
import type { LiveStatus } from "../types";

interface PanelHeaderProps {
  driver: string;
  lastApplied: number;
  lastEnforced?: number;
  deviceLabel?: string;
  streaming?: LiveStatus;
  // Every setting's explanation open at once, for reading with a controller.
  showDetails: boolean;
  onShowDetails: (on: boolean) => void;
}

export function PanelHeader({
  driver,
  lastApplied,
  lastEnforced,
  deviceLabel,
  streaming,
  showDetails,
  onShowDetails,
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
      {streaming?.streaming_mode && (
        // Always in view, so nobody has to find the section to know whether
        // the fixes are on right now.
        <PanelSectionRow>
          <span
            style={{
              fontSize: theme.fontSize.tiny,
              background: streaming.streaming_active ? theme.success.badgeBg : theme.info.accentBg,
              color: streaming.streaming_active ? theme.success.text : theme.info.text,
              padding: "2px 8px",
              borderRadius: theme.radius.pill,
            }}
          >
            {streaming.streaming_active
              ? streaming.streaming_app
                ? `Auto mode: streaming ${streaming.streaming_app}, fixes on`
                : "Auto mode: held on, fixes on"
              : "Auto mode: no stream, fixes paused"}
          </span>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ToggleField
          label="Show details"
          description="Explains every setting below. You can also tap (i) next to one."
          checked={showDetails}
          onChange={onShowDetails}
        />
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
