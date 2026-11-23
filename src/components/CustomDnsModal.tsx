import { useState } from "react";
import { ConfirmModal, TextField } from "@decky/ui";

// Custom DNS servers are typed in a dialog of their own. The field used to
// sit in the panel under the provider menu, shown by state the panel held
// while the menu closed, and on the Steam Deck choosing Custom could leave
// the menu back on the saved provider with no field to type in (GitHub #18).
// A dialog keeps what is being typed out of the panel, so nothing the panel
// does while the on-screen keyboard is up can replace it.
export function CustomDnsModal({
  initial,
  onApply,
  onCancel,
  closeModal,
}: {
  initial: string;
  onApply: (servers: string) => void;
  onCancel: () => void;
  closeModal?: () => void;
}) {
  const [servers, setServers] = useState(initial);
  const trimmed = servers.trim();
  return (
    <ConfirmModal
      strTitle="Custom DNS"
      strDescription="Up to six IPv4 addresses, with spaces between them. For example 1.1.1.1 9.9.9.9."
      strOKButtonText="Apply"
      strCancelButtonText="Cancel"
      bOKDisabled={!trimmed}
      closeModal={closeModal}
      onOK={() => onApply(trimmed)}
      onCancel={onCancel}
    >
      <TextField
        label="DNS servers"
        value={servers}
        onChange={(e: React.ChangeEvent<HTMLInputElement>) => setServers(e.target.value)}
      />
    </ConfirmModal>
  );
}
