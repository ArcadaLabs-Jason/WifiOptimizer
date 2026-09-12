import { PanelSection, PanelSectionRow, ButtonItem, DropdownItem } from "@decky/ui";
import type { UpdateCheckResult } from "../types";
import { theme } from "../theme";

interface UpdatesSectionProps {
  channel: string;
  updating: boolean;
  checkingUpdate: boolean;
  updateInfo: UpdateCheckResult | null;
  updateError: string | null;
  onChannelChange: (channel: string) => void;
  onApply: () => void;
  onCheck: () => void;
}

const CHANNEL_OPTIONS = [
  { data: "stable", label: "Stable" },
  { data: "beta", label: "Beta" },
];

// The Updates panel section: channel selector, optional warning text for a
// stuck / failed update, and a button that context-switches between
// "Updating...", "Update Now", "Up to date", and "Check for Updates"
// depending on state.
export function UpdatesSection({
  channel,
  updating,
  checkingUpdate,
  updateInfo,
  updateError,
  onChannelChange,
  onApply,
  onCheck,
}: UpdatesSectionProps) {
  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <DropdownItem
          label="Update channel"
          rgOptions={CHANNEL_OPTIONS}
          selectedOption={channel}
          onChange={(option: { data: string }) => onChannelChange(option.data)}
        />
      </PanelSectionRow>
      {updateError && !updating && (
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.body, color: theme.warning.text }}>
            &#9888; {updateError}
          </div>
        </PanelSectionRow>
      )}
      {updating ? (
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.body, color: theme.info.text }}>
            Updating... plugin will restart momentarily.
          </div>
        </PanelSectionRow>
      ) : updateInfo?.update_available ? (
        <>
          <PanelSectionRow>
            <div
              style={{
                fontSize: theme.fontSize.body,
                color: updateInfo.is_downgrade ? theme.info.text : theme.success.text,
              }}
            >
              {updateInfo.is_downgrade
                ? `v${updateInfo.latest_version} is what the beta channel currently offers, which is older than your v${updateInfo.current_version}`
                : `v${updateInfo.latest_version} available (you have v${updateInfo.current_version})`}
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={onApply}>
              {updateInfo.is_downgrade
                ? `Go back to v${updateInfo.latest_version}`
                : "Update Now"}
            </ButtonItem>
          </PanelSectionRow>
        </>
      ) : (
        <>
          {updateInfo && updateInfo.success === false && updateInfo.message && (
            <PanelSectionRow>
              <div style={{ fontSize: theme.fontSize.tiny, color: theme.error.text }}>
                {updateInfo.message}
              </div>
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={checkingUpdate} onClick={onCheck}>
              {checkingUpdate
                ? "Checking..."
                : updateInfo?.success && !updateInfo?.update_available
                  ? "Up to date"
                  : "Check for Updates"}
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
}
