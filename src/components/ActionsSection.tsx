import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";
import { theme } from "../theme";

interface ActionsSectionProps {
  connected: boolean;
  isBusy: boolean;
  onForceReapply: () => void;
  onReset: () => void;
  // Reset's outcome when it failed. Force Reapply reports through the
  // result banner at the top of the panel, like Optimize Safe.
  resetError?: string | null;
  // Reset's outcome when it worked.
  resetNotice?: string | null;
  // The notice is not plain "Done": Reset worked, with networks still to
  // change, which the plugin retries.
  resetNoticeInfo?: boolean;
  // Said here while Force Reapply or Reset runs, where the button was pressed.
  workingText?: string | null;
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
}: ActionsSectionProps) {
  return (
    <PanelSection title="Actions">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || isBusy}
          onClick={onForceReapply}
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
