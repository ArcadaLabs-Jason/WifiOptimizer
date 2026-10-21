import { PanelSection, PanelSectionRow, ButtonItem, Navigation } from "@decky/ui";
import { theme } from "../theme";
import * as backend from "../backend";
import { useState } from "react";
import { withTimeout } from "../utils";

const KOFI_URL = "https://ko-fi.com/fanciestpeanut";

interface PanelFooterProps {
  version: string;
}

export function PanelFooter({ version }: PanelFooterProps) {
  const [diagState, setDiagState] = useState<
    "idle" | "copying" | "done" | "saved" | "error" | "save_failed"
  >("idle");
  // Where the file went when the clipboard was not available. Kept until
  // the button is pressed again: it is the only place the path is said, and
  // five seconds is not long enough to note it down.
  const [savedPath, setSavedPath] = useState("");
  const rowStyle: React.CSSProperties = {
    fontSize: theme.fontSize.small,
    color: theme.text.tertiary,
  };

  const handleCopyDiagnostics = async () => {
    setDiagState("copying");
    setSavedPath("");
    try {
      // Bounded, or the button could stay on "Collecting..." for good.
      const info = await withTimeout(backend.getDiagnosticInfo());
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
        let saved: Record<string, unknown> | null = null;
        try {
          saved = await withTimeout(backend.saveDiagnosticInfo());
        } catch {
          saved = null;
        }
        if (saved?.success === true) {
          setDiagState("saved");
          setSavedPath(typeof saved.path === "string" ? saved.path : "");
        } else {
          setDiagState("save_failed");
        }
      }
      setTimeout(() => setDiagState("idle"), 5000);
    } catch {
      setDiagState("error");
      setTimeout(() => setDiagState("idle"), 5000);
    }
  };

  const label =
    diagState === "done"
      ? "Copied to clipboard"
      : diagState === "saved"
        ? "Saved to a file"
        : diagState === "save_failed"
          ? "Couldn't save diagnostics"
          : diagState === "error"
            ? "Couldn't collect diagnostics"
            : diagState === "copying"
              ? "Collecting..."
              : "Copy diagnostics";

  return (
    <PanelSection>
      <PanelSectionRow>
        <div style={rowStyle}>v{version} by jasonridesabike</div>
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
      {(diagState === "save_failed" || diagState === "error") && (
        <PanelSectionRow>
          <div style={{ ...rowStyle, color: theme.error.text }}>
            Try again in a moment. If it keeps failing, say so in your report.
          </div>
        </PanelSectionRow>
      )}
      {savedPath && (
        <PanelSectionRow>
          <div style={{ ...rowStyle, wordBreak: "break-all" }}>
            The clipboard wasn't available, so the diagnostics were saved to
            this file: {savedPath}
          </div>
        </PanelSectionRow>
      )}
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
