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
}

export function ActionsSection({
  connected,
  isBusy,
  onForceReapply,
  onReset,
  resetError,
}: ActionsSectionProps) {
  return (
    <PanelSection title="Actions">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || isBusy}
          onClick={onForceReapply}
        >
          Force Reapply All
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={isBusy} onClick={onReset}>
          Reset Settings
        </ButtonItem>
      </PanelSectionRow>
      {resetError && (
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.tiny, color: theme.error.text }}>
            {resetError}
          </div>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}
