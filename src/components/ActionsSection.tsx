import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";

interface ActionsSectionProps {
  connected: boolean;
  supported: boolean;
  isBusy: boolean;
  onForceReapply: () => void;
  onReset: () => void;
}

// Bottom-panel action buttons. Force Reapply re-runs every enabled
// optimization; Reset Settings reverts runtime state and the plugin's
// config files (not NM per-connection settings - those persist on the
// saved network).
export function ActionsSection({
  connected,
  supported,
  isBusy,
  onForceReapply,
  onReset,
}: ActionsSectionProps) {
  return (
    <PanelSection title="Actions">
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!connected || !supported || isBusy}
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
