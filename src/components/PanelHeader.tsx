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
  // Whether any fix auto mode governs (Prevent lag spikes, buffer tuning,
  // CAKE) is turned on. With none on there is nothing to pause or hold.
  fixesOn: boolean;
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
  fixesOn,
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
            {/* The same three cases the streaming section tells apart: an
                app named, Keep fixes on, or a stream flagged before the app
                has been named, just after a reload. The fixes are only
                mentioned when one of them is turned on. */}
            {streaming.streaming_active
              ? streaming.streaming_app
                ? `Auto mode: streaming ${streaming.streaming_app}${fixesOn ? ", fixes on" : ""}`
                : streaming.streaming_held
                  ? fixesOn ? "Auto mode: held on, fixes on" : "Auto mode: held on"
                  : fixesOn ? "Auto mode: stream running, fixes on" : "Auto mode: stream running"
              : fixesOn ? "Auto mode: no stream, fixes paused" : "Auto mode: no stream"}
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
