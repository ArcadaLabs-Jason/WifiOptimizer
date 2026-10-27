import { PanelSection, PanelSectionRow, ButtonItem, Navigation, ToggleField } from "@decky/ui";
import { theme } from "../theme";
import * as backend from "../backend";
import { useEffect, useRef, useState } from "react";
import { withTimeout } from "../utils";
import { SendReportRow } from "./SendReport";

const KOFI_URL = "https://ko-fi.com/fanciestpeanut";

interface PanelFooterProps {
  version: string;
  // Every setting's explanation open at once, for reading with a controller.
  // Down here with the other secondary controls: the top of the panel is for
  // status and the first useful action, and the (i) next to each setting
  // already explains that one.
  showDetails: boolean;
  onShowDetails: (on: boolean) => void;
}

export function PanelFooter({ version, showDetails, onShowDetails }: PanelFooterProps) {
  const [diagState, setDiagState] = useState<
    "idle" | "copying" | "done" | "saved" | "error" | "save_failed"
  >("idle");
  // Where the file went when the clipboard was not available. Kept until
  // the button is pressed again: it is the only place the path is said, and
  // five seconds is not long enough to note it down.
  const [savedPath, setSavedPath] = useState("");
  // The timer that puts the button back to idle after a result. Cleared on
  // each press: one left from an earlier press set the button back to idle
  // in the middle of the next collection, which let a second one start.
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (idleTimer.current) clearTimeout(idleTimer.current);
    },
    [],
  );
  const backToIdle = () => {
    idleTimer.current = setTimeout(() => {
      idleTimer.current = null;
      setDiagState("idle");
    }, 5000);
  };
  const rowStyle: React.CSSProperties = {
    fontSize: theme.fontSize.small,
    color: theme.text.tertiary,
  };

  const handleCopyDiagnostics = async () => {
    if (idleTimer.current) {
      clearTimeout(idleTimer.current);
      idleTimer.current = null;
    }
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
      backToIdle();
    } catch {
      setDiagState("error");
      backToIdle();
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
        <ToggleField
          label="Show details"
          description="Opens every (i) explanation."
          checked={showDetails}
          onChange={onShowDetails}
        />
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
      <SendReportRow />
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
