import { PanelSection, PanelSectionRow, ButtonItem, Navigation } from "@decky/ui";
import { theme } from "../theme";
import * as backend from "../backend";
import { useState } from "react";

const KOFI_URL = "https://ko-fi.com/fanciestpeanut";

interface PanelFooterProps {
  version: string;
}

export function PanelFooter({ version }: PanelFooterProps) {
  const [diagState, setDiagState] = useState<
    "idle" | "copying" | "done" | "saved" | "error"
  >("idle");
  const rowStyle: React.CSSProperties = {
    fontSize: theme.fontSize.tiny,
    color: theme.text.dim,
  };

  const handleCopyDiagnostics = async () => {
    setDiagState("copying");
    try {
      const info = await backend.getDiagnosticInfo();
      // A collection that failed comes back as a result, not a throw, and
      // copying it said "Copied to clipboard" over an error message.
      if (info?.success === false) throw new Error(String(info.error ?? "diagnostics failed"));
      const text = JSON.stringify(info, null, 2);
      try {
        await navigator.clipboard.writeText(text);
        setDiagState("done");
      } catch {
        // A resolved failure is still a failure: saying "Saved" here sent
        // people looking for a file that was never written.
        const saved = await backend.saveDiagnosticInfo();
        setDiagState(saved?.success === false ? "error" : "saved");
      }
      setTimeout(() => setDiagState("idle"), 5000);
    } catch {
      setDiagState("error");
      setTimeout(() => setDiagState("idle"), 3000);
    }
  };

  const label =
    diagState === "done"
      ? "Copied to clipboard"
      : diagState === "saved"
        ? "Saved to plugin settings folder"
        : diagState === "error"
          ? "Failed to collect diagnostics"
          : diagState === "copying"
            ? "Collecting..."
            : "Copy diagnostics";

  return (
    <PanelSection>
      <PanelSectionRow>
        <div style={rowStyle}>v{version} - by jasonridesabike</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={rowStyle}>
          If WiFi won't reconnect, a reboot usually fixes it.
          <br />
          Bugs? Report at github.com/ArcadaLabs-Jason/WifiOptimizer
        </div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={diagState === "copying"}
          onClick={handleCopyDiagnostics}
        >
          {label}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          onClick={() => {
            Navigation.CloseSideMenus();
            Navigation.NavigateToExternalWeb(KOFI_URL);
          }}
        >
          Support on Ko-fi
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}
