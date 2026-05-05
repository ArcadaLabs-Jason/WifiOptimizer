import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";

interface ActionsSectionProps {
  connected: boolean;
  isBusy: boolean;
  onForceReapply: () => void;
  onReset: () => void;
}

export function ActionsSection({
  connected,
  isBusy,
  onForceReapply,
  onReset,
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
    </PanelSection>
  );
}
