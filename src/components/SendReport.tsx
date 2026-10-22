import { ButtonItem, ConfirmModal, Focusable, PanelSection, PanelSectionRow, showModal } from "@decky/ui";
import { callable } from "@decky/api";
import { useEffect, useState } from "react";
import { previewBlocks, previewLines } from "../reportPreview";
import { theme } from "../theme";
import { CALL_TIMEOUT_MS, NoAnswer, withTimeout } from "../utils";
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
  // A waiting report was dropped unsent (refused, too large, invalid or
  // two weeks old), until dismissed. Older backends leave it out.
  dropped?: boolean;
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
const cancelPendingReport = callable<[], { success: boolean }>("cancel_pending_report");

const SEND_FAIL_TEXT =
  "Couldn't send the report. Try again later, or use Copy diagnostics and post it on GitHub.";
const PREVIEW_FAIL_TEXT = "Couldn't put the report together. Try again in a moment.";
// The report is saved before it is sent, so a send with no answer yet can
// still go through.
const SEND_SLOW_TEXT = "The report may still be sending. Check back here in a minute.";
const CANCEL_FAIL_TEXT = "Couldn't delete the saved report. Try again in a moment.";
const READY_TEXT = "Report ready. Open it here to review.";
// The preview collects the same diagnostics as Copy diagnostics, and a send
// can take as long again, so both get the limit Copy diagnostics has
// (CALL_TIMEOUT_MS). The state read and the notices answer from a few small
// files and one bounded log read.
const STATE_LIMIT_MS = 60000;
// How often the state is read again while a report is waiting, or while no
// read has come through yet, so the waiting line and the reference follow a
// send made in the background without the panel being reopened.
const STATE_POLL_MS = 20000;

type Outcome = { text: string; tone: "success" | "info" | "error"; from: "wake" | "footer" };
// A preview that came back while the panel was closed, kept to open here.
type Ready = { id: string; text: string; from: Outcome["from"] };

// Shared by the footer button and the wake notice, and kept for as long as
// the plugin is loaded, so a result stays on screen until it is dismissed
// even if the panel is closed and opened again.
// inFlight counts the preview, send and cancel calls under way, so the
// buttons stay disabled until the last of them has ended, not the first.
let shared: {
  state: ReportState | null;
  inFlight: number;
  outcome: Outcome | null;
  ready: Ready | null;
  cancelFailed: boolean;
} = {
  state: null,
  inFlight: 0,
  outcome: null,
  ready: null,
  cancelFailed: false,
};
const listeners = new Set<() => void>();
let loading: Promise<void> | null = null;
// The one read chained after a read under way (refreshReportState).
let again: Promise<void> | null = null;
// Counts the actions that have ended (a send, a cancel, a dismiss), and the
// count a read under way started at.
let actionSeq = 0;
let loadingSeq = 0;
let pollTimer: ReturnType<typeof setTimeout> | null = null;

// Notices dismissed during this load. They stay hidden whatever the backend
// answered, so Dismiss never looks as if it did nothing. One the backend
// could not record comes back after a reload. The wake notice also goes once
// a report has been sent or saved, which answers it, and comes back if that
// saved report is withdrawn, unless Dismiss hid it (wakeDismissed).
const hidden = { wake: false, wakeDismissed: false, dropped: false, sentRef: "" };

function update(patch: Partial<typeof shared>) {
  shared = { ...shared, ...patch };
  listeners.forEach((listener) => listener());
}

function begin(patch: Partial<typeof shared> = {}) {
  update({ ...patch, inFlight: shared.inFlight + 1 });
}

function end(patch: Partial<typeof shared> = {}) {
  actionSeq += 1;
  update({ ...patch, inFlight: Math.max(0, shared.inFlight - 1) });
}

// One timer at a time, only while something here is on screen.
function schedulePoll() {
  if (pollTimer || !listeners.size) return;
  if (shared.state && !shared.state.pending) return;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    // While hidden, the visibility handler reads it when the panel is back.
    if (document.visibilityState === "visible") void refreshReportState();
  }, STATE_POLL_MS);
}

function stopPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function onVisibility() {
  if (document.visibilityState === "visible") void refreshReportState();
}

// Read the state again. A read already under way is shared by whoever asks
// while it runs, unless an action has ended since it started: it may then
// answer with the state from before the action, so one more read is chained
// after it (only one, however many ask).
function refreshReportState(): Promise<void> {
  if (loading) {
    if (loadingSeq === actionSeq) return loading;
    if (!again) {
      again = loading.then(() => {
        again = null;
        return refreshReportState();
      });
    }
    return again;
  }
  loadingSeq = actionSeq;
  loading = withTimeout(getReportState(), STATE_LIMIT_MS)
    .then((state) => {
      if (!state || typeof state !== "object") return;
      // A dropped report the backend no longer reports was dismissed for
      // good, so the next one is shown again.
      if (!state.dropped) hidden.dropped = false;
      update({ state });
    })
    .catch((e) => console.error("report state error", e))
    .finally(() => {
      loading = null;
      schedulePoll();
    });
  return loading;
}

function useReportState() {
  const [, setTick] = useState(0);
  useEffect(() => {
    const listener = () => setTick((n) => n + 1);
    if (!listeners.size) document.addEventListener("visibilitychange", onVisibility);
    listeners.add(listener);
    void refreshReportState();
    return () => {
      listeners.delete(listener);
      if (!listeners.size) {
        document.removeEventListener("visibilitychange", onVisibility);
        stopPoll();
      }
    };
  }, []);
  return shared;
}

// Split into blocks the controller can step through: one long block of text
// cannot be scrolled with the D-pad, but focus moving from block to block
// scrolls the box along with it. previewBlocks keeps each block short enough
// to fit the box, and the block in focus is outlined.
function ReportPreviewModal({
  text,
  onSend,
  closeModal,
}: {
  text: string;
  onSend: () => void;
  closeModal?: () => void;
}) {
  const blocks = previewBlocks(text.split("\n"));
  const [focused, setFocused] = useState(-1);
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
            onGamepadFocus={() => setFocused(i)}
            onGamepadBlur={() => setFocused((f) => (f === i ? -1 : f))}
            onFocus={() => setFocused(i)}
            onBlur={() => setFocused((f) => (f === i ? -1 : f))}
            style={{
              fontFamily: "monospace",
              fontSize: theme.fontSize.body,
              color: theme.text.primary,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              padding: "2px 4px",
              borderRadius: theme.radius.sm,
              outline: focused === i ? `2px solid ${theme.info.text}` : "none",
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
  begin({ outcome: null, ready: null });
  let result: ReportResult | null = null;
  let noAnswer = false;
  try {
    result = await withTimeout(sendReport(id), CALL_TIMEOUT_MS);
  } catch (e) {
    noAnswer = e instanceof NoAnswer;
    console.error("send report error", e);
  }
  const status = result?.status;
  if (noAnswer || status === "sent" || status === "queued") hidden.wake = true;
  end({
    outcome: noAnswer
      ? { text: SEND_SLOW_TEXT, tone: "info", from }
      : {
          text: typeof result?.message === "string" ? result.message : SEND_FAIL_TEXT,
          tone: status === "sent" ? "success" : status === "queued" ? "info" : "error",
          from,
        },
  });
  await refreshReportState();
}

function openPreview(ready: Ready) {
  const { id, from } = ready;
  update({ ready: null });
  showModal(<ReportPreviewModal text={ready.text} onSend={() => void send(id, from)} />);
}

async function startReport(from: Outcome["from"]) {
  if (shared.inFlight) return;
  begin({ outcome: null, ready: null });
  let preview: ReportPreview | null = null;
  try {
    preview = await withTimeout(previewReport(), CALL_TIMEOUT_MS);
  } catch (e) {
    console.error("preview report error", e);
  }
  end();
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
  const ready = { id: preview.id, text: previewLines(preview.payload).join("\n"), from };
  // Collecting can take a minute or more. If the panel was closed in the
  // meantime, the preview would open over whatever is on screen, a game
  // included, so it waits here until the panel is opened again.
  if (document.visibilityState === "visible") openPreview(ready);
  else update({ ready });
}

async function cancelPending() {
  if (shared.inFlight) return;
  begin({ cancelFailed: false });
  let done = false;
  try {
    done = (await withTimeout(cancelPendingReport(), STATE_LIMIT_MS))?.success === true;
  } catch (e) {
    console.error("cancel report error", e);
  }
  // A result that said the report was saved for later is no longer true,
  // and the wake notice that report answered is open again (the backend
  // shows it again too, D17), unless it was dismissed.
  if (done && !hidden.wakeDismissed) hidden.wake = false;
  end({ cancelFailed: !done, outcome: done ? null : shared.outcome });
  await refreshReportState();
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

function ReadyRows({ ready, working }: { ready: Ready; working: boolean }) {
  return (
    <>
      <PanelSectionRow>
        <div style={{ ...rowStyle, color: theme.info.text }}>{READY_TEXT}</div>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={working} onClick={() => openPreview(ready)}>
          Open the report
        </ButtonItem>
      </PanelSectionRow>
    </>
  );
}

// Beside Copy diagnostics, inside the footer's section.
export function SendReportRow() {
  const { state, inFlight, outcome, ready, cancelFailed } = useReportState();
  const working = inFlight > 0;
  if (!state?.enabled) return null;
  const sentRef = state.sent_ref && state.sent_ref !== hidden.sentRef ? state.sent_ref : "";
  return (
    <>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={working} onClick={() => void startReport("footer")}>
          {working ? "Working..." : "Send report to the developer"}
        </ButtonItem>
      </PanelSectionRow>
      {/* A preview started from the wake notice is offered here once the
          notice is not on screen to offer it, or it would be lost. */}
      {ready && (ready.from === "footer" || !wakeNoticeOffersReady(state, outcome)) && (
        <ReadyRows ready={ready} working={working} />
      )}
      {outcome?.from === "footer" && <OutcomeRows outcome={outcome} />}
      {state.pending && (
        <>
          <PanelSectionRow>
            <div style={rowStyle}>A report is waiting to be sent.</div>
          </PanelSectionRow>
          {cancelFailed && (
            <PanelSectionRow>
              <div style={{ ...rowStyle, color: theme.error.text }}>{CANCEL_FAIL_TEXT}</div>
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={working} onClick={() => void cancelPending()}>
              Don't send it
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
      {state.dropped && !hidden.dropped && (
        <>
          <PanelSectionRow>
            <div style={{ ...rowStyle, color: theme.warning.text }}>
              Your saved report couldn't be sent. Use Copy diagnostics instead.
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                hidden.dropped = true;
                update({});
                try {
                  await withTimeout(dismissReportNotice("dropped"), STATE_LIMIT_MS);
                } catch (e) {
                  console.error("dismiss report notice error", e);
                }
                actionSeq += 1;
                await refreshReportState();
              }}
            >
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
      {sentRef && (
        <>
          <PanelSectionRow>
            <div style={{ ...rowStyle, color: theme.success.text }}>
              Your saved report was sent. Reference {sentRef}. Quote it if you post about this.
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={async () => {
                hidden.sentRef = sentRef;
                update({});
                try {
                  await withTimeout(dismissReportNotice("sent"), STATE_LIMIT_MS);
                } catch (e) {
                  console.error("dismiss report notice error", e);
                }
                actionSeq += 1;
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

// The wake's date and time, in the device's own format.
function wakeNoticeText(at: number): string {
  const when =
    at > 0
      ? ` (${new Date(at * 1000).toLocaleString(undefined, {
          year: "numeric",
          month: "short",
          day: "numeric",
          hour: "numeric",
          minute: "2-digit",
        })})`
      : "";
  return `Before this device last started up, WiFi didn't come back after it woke from sleep${when}. Sending a report helps find out why.`;
}

// Whether the wake notice is on screen with its own buttons, where a preview
// it started is offered. The same conditions WakeFailureNotice renders by.
function wakeNoticeOffersReady(state: ReportState | null, outcome: Outcome | null): boolean {
  if (!state?.enabled || outcome?.from === "wake") return false;
  return state.wake_failure && !hidden.wake && !state.pending;
}

// Near the top of the panel, for a wake in the previous boot that left WiFi
// down, until dismissed or answered with a report.
export function WakeFailureNotice() {
  const { state, inFlight, outcome, ready } = useReportState();
  const working = inFlight > 0;
  const fromHere = outcome?.from === "wake" ? outcome : null;
  if (!state?.enabled || (!state.wake_failure && !fromHere)) return null;
  // A report already sent, saved or waiting answers the notice.
  if (!fromHere && (hidden.wake || state.pending)) return null;
  const dismiss = async () => {
    hidden.wake = true;
    hidden.wakeDismissed = true;
    // Only the notice's own result goes with it, not one the footer shows.
    if (shared.outcome?.from === "wake") update({ outcome: null });
    else update({});
    try {
      await withTimeout(dismissReportNotice("wake"), STATE_LIMIT_MS);
    } catch (e) {
      console.error("dismiss wake notice error", e);
    }
    actionSeq += 1;
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
      <Banner variant="warning">{wakeNoticeText(state.wake_failure_at)}</Banner>
      <PanelSection>
        {ready?.from === "wake" ? (
          <ReadyRows ready={ready} working={working} />
        ) : (
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={working} onClick={() => void startReport("wake")}>
              {working ? "Working..." : "Send report to the developer"}
            </ButtonItem>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => void dismiss()}>
            Dismiss
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}
