import { useContext } from "react";
import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";
import { theme } from "../theme";
import { DetailsContext } from "./InfoRow";

// What Force reapply all does, under its button. Only with Show details on:
// explanation never stands in front of an action.
const FORCE_REAPPLY_DETAIL = "Applies every setting that is on again. Can reconnect WiFi.";

interface ActionsSectionProps {
  connected: boolean;
  isBusy: boolean;
  onForceReapply: () => void;
  onReset: () => void;
  // Reset's outcome when it failed.
  resetError?: string | null;
  // Reset's outcome when it worked.
  resetNotice?: string | null;
  // The notice is not plain "Done": Reset worked, with networks still to
  // change, which the plugin retries.
  resetNoticeInfo?: boolean;
  // Said here while Force Reapply or Reset runs, where the button was pressed.
  workingText?: string | null;
  // Force reapply's outcome, refusals included: the result banner at the top
  // of the panel says it too, but is off screen from here. Same text, tone
  // and expiry as that banner.
  reapplyResult?: { variant: "success" | "warning" | "error" | "info"; text: string } | null;
}

export function ActionsSection({
  connected,
  isBusy,
  onForceReapply,
  onReset,
  resetError,
  resetNotice,
  resetNoticeInfo,
  workingText,
  reapplyResult,
}: ActionsSectionProps) {
  const showDetails = useContext(DetailsContext);
  return (
    <PanelSection title="Actions">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || isBusy}
          onClick={onForceReapply}
          description={showDetails ? FORCE_REAPPLY_DETAIL : undefined}
        >
          Force reapply all
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={isBusy} onClick={onReset}>
          Reset settings
        </ButtonItem>
      </PanelSectionRow>
      {workingText && (
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.small, color: theme.info.text }}>
            {workingText}
          </div>
        </PanelSectionRow>
      )}
      {!workingText && reapplyResult && (
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.small, color: theme[reapplyResult.variant].text }}>
            {reapplyResult.text}
          </div>
        </PanelSectionRow>
      )}
      {!workingText && (resetError || resetNotice) && (
        <PanelSectionRow>
          <div
            style={{
              fontSize: theme.fontSize.small,
              color: resetError
                ? theme.error.text
                : resetNoticeInfo
                  ? theme.info.text
                  : theme.success.text,
            }}
          >
            {resetError ?? resetNotice}
          </div>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}
