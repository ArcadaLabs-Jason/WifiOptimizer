import { PanelSection, PanelSectionRow, ButtonItem } from "@decky/ui";
import { theme } from "../theme";
import * as backend from "../backend";
import { useState } from "react";

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
      const text = JSON.stringify(info, null, 2);
      try {
        await navigator.clipboard.writeText(text);
        setDiagState("done");
      } catch {
        await backend.saveDiagnosticInfo();
        setDiagState("saved");
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
    </PanelSection>
  );
}
