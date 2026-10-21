import { PanelSection, PanelSectionRow, ButtonItem, DropdownItem, ToggleField } from "@decky/ui";
import type { UpdateCheckResult } from "../types";
import { theme } from "../theme";

interface UpdatesSectionProps {
  channel: string;
  updating: boolean;
  // What the update is doing right now, while updating.
  updatingText: string;
  checkingUpdate: boolean;
  updateInfo: UpdateCheckResult | null;
  updateError: string | null;
  onChannelChange: (channel: string) => void;
  // The toast when an update comes out, from the check that runs with the
  // panel closed.
  notify: boolean;
  onNotifyChange: (enabled: boolean) => void;
  onApply: () => void;
  onCheck: () => void;
  // Another operation is running. Starting an update then would restart
  // Decky in the middle of it.
  busy?: boolean;
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
  updatingText,
  checkingUpdate,
  updateInfo,
  updateError,
  onChannelChange,
  notify,
  onNotifyChange,
  onApply,
  onCheck,
  busy = false,
}: UpdatesSectionProps) {
  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <DropdownItem
          label="Update channel"
          rgOptions={CHANNEL_OPTIONS}
          selectedOption={channel}
          disabled={updating}
          onChange={(option: { data: string }) => onChannelChange(option.data)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="Tell me about updates"
          description="Shows one notification when a new version is out, never during a game or a stream. Nothing installs until you choose to."
          checked={notify}
          disabled={updating}
          onChange={onNotifyChange}
        />
      </PanelSectionRow>
      {updateError && !updating && (
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.body, color: theme.warning.text }}>
            {updateError}
          </div>
        </PanelSectionRow>
      )}
      {updating ? (
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.body, color: theme.info.text }}>
            {updatingText}
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
            <ButtonItem layout="below" disabled={busy} onClick={onApply}>
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
            <ButtonItem layout="below" disabled={checkingUpdate || updating} onClick={onCheck}>
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
