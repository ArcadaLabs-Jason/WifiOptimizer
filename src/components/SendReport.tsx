import { ButtonItem, ConfirmModal, Focusable, PanelSection, PanelSectionRow, showModal } from "@decky/ui";
import { callable } from "@decky/api";
import { useEffect, useState } from "react";
import { theme } from "../theme";
import { Banner } from "./Banner";

// "Send report to the developer": the same report as Copy diagnostics, sent
// only after the user has read it and said yes. The backend decides whether
// the feature is on at all (get_report_state's enabled); while it is off
// nothing here renders.

export interface ReportState {
  success: boolean;
  enabled: boolean;
  // A report the user sent while offline, waiting for WiFi.
  pending: boolean;
  // The reference of a waiting report that has since gone, until dismissed.
  sent_ref: string;
  // The previous boot's last wake left WiFi down. Only this and the time.
  wake_failure: boolean;
  wake_failure_at: number;
}

interface ReportPreview {
  success: boolean;
  id?: string;
  payload?: Record<string, unknown>;
  message?: string;
}

interface ReportResult {
  success: boolean;
  status?: "sent" | "queued" | "failed";
  ref?: string;
  message?: string;
}

const getReportState = callable<[], ReportState>("get_report_state");
const previewReport = callable<[], ReportPreview>("preview_report");
const sendReport = callable<[id: string], ReportResult>("send_report");
const dismissReportNotice = callable<[kind: string], { success: boolean }>("dismiss_report_notice");

const SEND_FAIL_TEXT =
  "Couldn't send the report. Try again later, or use Copy diagnostics and post it on GitHub.";
const PREVIEW_FAIL_TEXT = "Couldn't put the report together. Try again in a moment.";
// Longer than the backend's own limits (diagnostics, then a 25 second send).
const CALL_LIMIT_MS = 60000;

type Outcome = { text: string; tone: "success" | "info" | "error"; from: "wake" | "footer" };

// Shared by the footer button and the wake notice, and kept for as long as
// the plugin is loaded, so a result stays on screen until it is dismissed
// even if the panel is closed and opened again.
let shared: { state: ReportState | null; working: boolean; outcome: Outcome | null } = {
  state: null,
  working: false,
  outcome: null,
};
const listeners = new Set<() => void>();
let loading: Promise<void> | null = null;

function update(patch: Partial<typeof shared>) {
  shared = { ...shared, ...patch };
  listeners.forEach((listener) => listener());
}

function withLimit<T>(call: Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("no answer")), CALL_LIMIT_MS);
    call.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function refreshReportState(): Promise<void> {
  if (!loading) {
    loading = withLimit(getReportState())
      .then((state) => {
        if (state && typeof state === "object") update({ state });
      })
      .catch((e) => console.error("report state error", e))
      .finally(() => {
        loading = null;
      });
  }
  return loading;
}

function useReportState() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const listener = () => setTick((n) => n + 1);
    listeners.add(listener);
    void refreshReportState();
    return () => {
      listeners.delete(listener);
    };
  }, []);
  return shared;
}

// Split into blocks the controller can step through: one long block of text
// cannot be scrolled with the D-pad, but focus moving from block to block
// scrolls the box along with it.
const LINES_PER_BLOCK = 12;

function ReportPreviewModal({
  text,
  onSend,
  closeModal,
}: {
  text: string;
  onSend: () => void;
  closeModal?: () => void;
}) {
  const lines = text.split("\n");
  const blocks: string[] = [];
  for (let i = 0; i < lines.length; i += LINES_PER_BLOCK) {
    blocks.push(lines.slice(i, i + LINES_PER_BLOCK).join("\n"));
  }
  return (
    <ConfirmModal
      strTitle="Send this report to the developer?"
      strDescription="This is everything that will be sent. Network names and addresses are already taken out. Scroll through it, then choose Send."
      strOKButtonText="Send"
      strCancelButtonText="Cancel"
      onOK={onSend}
      closeModal={closeModal}
    >
      <Focusable
        style={{
          maxHeight: "50vh",
          overflowY: "auto",
          marginTop: "8px",
          padding: "8px",
          background: theme.surface.md,
          borderRadius: theme.radius.md,
        }}
      >
        {blocks.map((block, i) => (
          <Focusable
            key={i}
            onActivate={() => {}}
            style={{
              fontFamily: "monospace",
              fontSize: theme.fontSize.body,
              color: theme.text.primary,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
            }}
          >
            {block}
          </Focusable>
        ))}
      </Focusable>
    </ConfirmModal>
  );
}

async function send(id: string, from: Outcome["from"]) {
  update({ working: true, outcome: null });
  let result: ReportResult | null = null;
  try {
    result = await withLimit(sendReport(id));
  } catch (e) {
    console.error("send report error", e);
  }
  const status = result?.status;
  update({
    working: false,
    outcome: {
      text: typeof result?.message === "string" ? result.message : SEND_FAIL_TEXT,
      tone: status === "sent" ? "success" : status === "queued" ? "info" : "error",
      from,
    },
  });
  await refreshReportState();
}

async function startReport(from: Outcome["from"]) {
  if (shared.working) return;
  update({ working: true, outcome: null });
  let preview: ReportPreview | null = null;
  try {
    preview = await withLimit(previewReport());
  } catch (e) {
    console.error("preview report error", e);
  }
  update({ working: false });
  if (!preview?.success || typeof preview.id !== "string" || !preview.payload) {
    update({
      outcome: {
        text: typeof preview?.message === "string" ? preview.message : PREVIEW_FAIL_TEXT,
        tone: "error",
        from,
      },
    });
    return;
  }
  const id = preview.id;
  showModal(
    <ReportPreviewModal
      text={JSON.stringify(preview.payload, null, 2)}
      onSend={() => void send(id, from)}
    />,
  );
}

const rowStyle = { fontSize: theme.fontSize.small, color: theme.text.tertiary };

function OutcomeRows({ outcome }: { outcome: Outcome }) {
  const color =
    outcome.tone === "success"
      ? theme.success.text
      : outcome.tone === "error"
        ? theme.error.text
        : theme.info.text;
  return (
    <>
      <PanelSectionRow>
        <div style={{ ...rowStyle, color }}>{outcome.text}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={() => update({ outcome: null })}>
          Dismiss
        </ButtonItem>
      </PanelSectionRow>
    </>
  );
}

// Beside Copy diagnostics, inside the footer's section.
export function SendReportRow() {
  const { state, working, outcome } = useReportState();
  if (!state?.enabled) return null;
  return (
    <>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={working} onClick={() => void startReport("footer")}>
          {working ? "Working..." : "Send report to the developer"}
        </ButtonItem>
      </PanelSectionRow>
      {outcome?.from === "footer" && <OutcomeRows outcome={outcome} />}
      {state.pending && !outcome && (
        <PanelSectionRow>
          <div style={rowStyle}>A report is waiting. It will be sent when WiFi is back.</div>
        </PanelSectionRow>
      )}
      {state.sent_ref && (
        <>
          <PanelSectionRow>
            <div style={{ ...rowStyle, color: theme.success.text }}>
              Your saved report was sent. Reference {state.sent_ref}. Quote it if you post about
              this.
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                try {
                  await dismissReportNotice("sent");
                } catch (e) {
                  console.error("dismiss report notice error", e);
                }
                await refreshReportState();
              }}
            >
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
    </>
  );
}

// Near the top of the panel, once per wake that left WiFi down.
export function WakeFailureNotice() {
  const { state, working, outcome } = useReportState();
  const fromHere = outcome?.from === "wake" ? outcome : null;
  if (!state?.enabled || (!state.wake_failure && !fromHere)) return null;
  const dismiss = async () => {
    update({ outcome: null });
    try {
      await dismissReportNotice("wake");
    } catch (e) {
      console.error("dismiss wake notice error", e);
    }
    await refreshReportState();
  };
  if (fromHere) {
    // After a failure the notice comes back with its button, to try again.
    // Once the report is sent or saved, the notice has done its job.
    const done = fromHere.tone !== "error";
    return (
      <>
        <Banner variant={fromHere.tone}>{fromHere.text}</Banner>
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => (done ? void dismiss() : update({ outcome: null }))}
            >
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      </>
    );
  }
  return (
    <>
      <Banner variant="warning">
        Last time your device woke from sleep, WiFi didn't come back. Sending a report helps
        find out why.
      </Banner>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={working} onClick={() => void startReport("wake")}>
            {working ? "Working..." : "Send report to the developer"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void dismiss()}>
            Dismiss
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}
