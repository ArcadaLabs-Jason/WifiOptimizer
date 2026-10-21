import {
  useState,
  useEffect,
  useCallback,
  useRef,
  Component,
  type ErrorInfo,
  type ReactNode,
} from "react";
import {
  PanelSection,
  PanelSectionRow,
  ButtonItem,
  DropdownItem,
  TextField,
  ConfirmModal,
  showModal,
  staticClasses,
} from "@decky/ui";
import { definePlugin } from "@decky/api";
import { FaWifi } from "react-icons/fa";

import * as backend from "./backend";
import type {
  PluginStatus,
  MethodResult,
  OptimizeSafeResult,
  UpdateCheckResult,
  UpdateProgress,
  BadgeStatus,
  BackendSwitchStatus,
  RegdomainInfo,
} from "./types";
import { ERROR_MESSAGES } from "./types";
import { InfoRow, DetailsContext, WORKING_TEXT } from "./components/InfoRow";
import { StatsGrid } from "./components/StatsGrid";
import { Banner } from "./components/Banner";
import { BackendToggleRow, backendResultText } from "./components/BackendToggleRow";
import { UpdatesSection } from "./components/UpdatesSection";
import { PanelHeader } from "./components/PanelHeader";
import { PanelFooter } from "./components/PanelFooter";
import { WakeFailureNotice } from "./components/SendReport";
import { ActionsSection } from "./components/ActionsSection";
import { StreamingSection } from "./components/StreamingSection";
import {
  watchUpdate,
  subscribeUpdate,
  checkUpdateOnLoad,
  disposeUpdateWatch,
  startUpdateNotices,
} from "./updateWatch";
import { theme } from "./theme";
import { NoAnswer, withTimeout } from "./utils";

const REFRESH_INTERVAL = 3000;
const RECONNECT_DELAY = 4000;
const BACKEND_POLL_INTERVAL = 750;
const UPDATE_CHECK_INTERVAL = 5 * 60 * 1000;
const UPDATE_CHECK_DEDUPE_MS = 60 * 1000;
// How long the panel waits for an update with no new progress before it
// says the update is taking too long. The same as the watch's limit in
// updateWatch.ts. The script itself may run for 14 minutes: the download,
// then, once the files are in place, as long as a game or stream is still
// running before it restarts Decky. A shorter limit told people to restart
// while the script was waiting for their game to end. Counted again from
// each new phase, since the signature check before the script starts takes
// up to two minutes of its own.
const UPDATE_TIMEOUT_MS = 15 * 60 * 1000;
const BACKEND_SWITCH_MAX_S = 240;
// One switch status read. It answers from memory, so this is only for a
// backend that has stopped answering altogether.
const BACKEND_POLL_TIMEOUT_MS = 20 * 1000;
// How long a finished backend switch's result stays up.
const BACKEND_RESULT_MS = 60 * 1000;
// How long an error or notice under a control stays up. They used to last
// until that control was used again, so a failure from an hour ago still
// read as the current state.
const MESSAGE_TTL_MS = 2 * 60 * 1000;
// Shown on the lock and band toggles while the iwd backend is in use.
const IWD_PIN_NOTICE =
  "Paused while the WiFi backend is iwd, where NetworkManager can't reconnect a network pinned this way. Switch to wpa_supplicant to use it.";
const BACKEND_POLL_MAX_FAILURES = 5;
// What Reset undoes, said before it happens. It reaches every network the
// plugin touched, so one stray press of the button should not be enough.
// Prevent lag spikes is saved as off, so it does not come back by itself.
// The network settings are written to the saved networks without a
// reconnect, so the connection in use keeps them until WiFi next connects.
// The update channel and notices are kept (main.py _reset_locked).
const RESET_CONFIRM_TEXT =
  "This turns off what the plugin applied and puts its WiFi settings back to their defaults, except that Prevent lag spikes stays off until you turn it on again. Buffer tuning and CAKE go back to the values your system had, and a WiFi region chosen here goes back to the one from before. The access point lock, the band preference, the IPv6 setting, custom DNS and Allow WPA2 are removed from the networks they were set on, from the next time WiFi connects. Update settings are kept.";
// Reset does not reconnect, so it does not say WiFi will.
const RESET_WORKING_TEXT = "Working...";
const RESET_DONE_TEXT = "Done. Settings are reset.";
// Only where the plugin can choose the backend itself. On SteamOS that is
// Valve's setting, and Reset leaves it alone.
const RESET_BACKEND_TEXT =
  " A WiFi backend chosen here goes back to the system default from the next boot.";
// The refresh after a change is bounded too, for the same reason.
const REFRESH_WAIT_MS = 15 * 1000;
const NO_ANSWER_TEXT =
  "No answer after 2 minutes. The change may still finish, so check this setting again in a moment. If WiFi doesn't come back, restart the device.";
// The device, not Steam: the backend runs in Decky's own service, which a
// Steam restart is not known to restart.
const UNREACHABLE_TEXT =
  "Couldn't reach the plugin's backend. Wait a moment and try again. If it keeps happening, restart the device.";
// Optimize Safe, Fix now and Force Reapply reconnect only when the lock or a
// network setting is among what they apply.
const BATCH_WORKING_TEXT = "Working. WiFi may reconnect, and this can take up to a minute.";
// For a failure that came with no message of its own.
const GENERIC_FAIL_TEXT =
  "That didn't work. Try again. If it keeps failing, use Copy diagnostics at the bottom of this panel and report it.";
// A setting that is on, but for another network than this one.
const OTHER_NETWORK_TEXT =
  "On for another network, so it does nothing here. To use it here, turn it off, then on again.";
// Drift keys, by the name the panel shows for the setting.
const DRIFT_LABELS: Record<string, string> = {
  power_save: "Prevent lag spikes",
  bssid_lock: "Stop background scanning",
  buffer_tuning: "Network buffer tuning",
  band_preference: "Band preference",
  dns: "Custom DNS",
  ipv6: "Disable IPv6",
  cake: "Traffic shaping (CAKE)",
};

// "A", "A and B", "A, B and C".
function joinNames(names: string[]): string {
  return names.length > 1
    ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`
    : names[0] ?? "";
}

// What a failed result says, without the raw command output some carry in
// "detail". That text is for the log, and read as noise in the panel.
function failureText(result: MethodResult): string {
  if (result.detail) console.warn("WiFi Optimizer: failure detail", result.detail);
  return result.message ?? ERROR_MESSAGES[result.error ?? ""] ?? GENERIC_FAIL_TEXT;
}

function isResult(value: unknown): value is MethodResult {
  return !!value && typeof (value as MethodResult).success === "boolean";
}

// Kept across the panel closing and reopening, which can remount it.
let detailsShown = false;
let duplicatesDismissed = false;
// Findings dismissed by the user: hidden until the finding clears and comes
// back. Not keyed on the signals they carry, which change on every scan and
// would bring the notice straight back.
let findingsDismissed = { wpa2: false, lockWeaker: false };
// The script fails for many reasons (download, checksum, unpacking, the
// version, copying files, or a game still running at its deadline, with the
// new files in place). Trying again covers the first ones, and a restart of
// the device loads files that are already in place. Restarting Steam is not
// known to restart Decky's backend, so it is not offered.
const UPDATE_FAILED_TEXT =
  "The update didn't finish. Try the update again. If it still doesn't finish, restart the device.";
const UPDATE_SLOW_TEXT =
  "The update is taking too long. Restart the device, then check the version at the bottom of this panel. If it didn't change, try the update again.";
// Long enough for the reloaded backend to have answered the update script,
// so a finished update is announced rather than mistaken for a running one.
const ANNOUNCE_DELAY_MS = 4000;

// What the panel says while an update runs, from what the script reports.
function updatingText(progress: UpdateProgress | null): string {
  const v = progress?.version ? ` v${progress.version}` : "";
  switch (progress?.phase) {
    case "downloading":
      return `Downloading${v}...`;
    case "installing":
      return `Installing${v}...`;
    case "swapped":
    case "loaded":
      return "Reloading the plugin...";
    // The script reports this before it waits for a game or stream to end,
    // which can take many minutes, so it says when the restart comes.
    case "restarting":
      return "Finishing the update. Steam restarts once no game or stream is running.";
    default:
      return "Starting the update...";
  }
}

function getBadge(
  driftKey: string | undefined,
  status: PluginStatus | null,
  errorKey: string | null,
): { badge: BadgeStatus; text: string } | null {
  // Only surface a badge when it tells the user something the toggle position
  // doesn't - failure or drift between our setting and the system state.
  // Binary on/off badges are redundant with the toggle itself and are hidden.
  if (errorKey) return { badge: "error", text: "failed" };
  if (driftKey && status?.drift?.[driftKey]) return { badge: "drifted", text: "drifted" };
  return null;
}

// Regions the selector offers. A regulatory domain is a legal constraint on
// transmit power and which channels may be used, so this is a list of ISO
// country codes rather than free text. It is the common ones, not all of
// them: a region set to one that is not here is still shown, by its code.
const REGION_UNMANAGED = "none";
const REGION_OPTIONS = [
  { data: "00", label: "World (00), most restrictive" },
  { data: "AR", label: "Argentina (AR)" },
  { data: "AU", label: "Australia (AU)" },
  { data: "AT", label: "Austria (AT)" },
  { data: "BE", label: "Belgium (BE)" },
  { data: "BR", label: "Brazil (BR)" },
  { data: "CA", label: "Canada (CA)" },
  { data: "CL", label: "Chile (CL)" },
  { data: "CN", label: "China (CN)" },
  { data: "CO", label: "Colombia (CO)" },
  { data: "CZ", label: "Czechia (CZ)" },
  { data: "DK", label: "Denmark (DK)" },
  { data: "FI", label: "Finland (FI)" },
  { data: "FR", label: "France (FR)" },
  { data: "DE", label: "Germany (DE)" },
  { data: "GR", label: "Greece (GR)" },
  { data: "HK", label: "Hong Kong (HK)" },
  { data: "HU", label: "Hungary (HU)" },
  { data: "IN", label: "India (IN)" },
  { data: "ID", label: "Indonesia (ID)" },
  { data: "IE", label: "Ireland (IE)" },
  { data: "IL", label: "Israel (IL)" },
  { data: "IT", label: "Italy (IT)" },
  { data: "JP", label: "Japan (JP)" },
  { data: "MY", label: "Malaysia (MY)" },
  { data: "MX", label: "Mexico (MX)" },
  { data: "NL", label: "Netherlands (NL)" },
  { data: "NZ", label: "New Zealand (NZ)" },
  { data: "NO", label: "Norway (NO)" },
  { data: "PH", label: "Philippines (PH)" },
  { data: "PL", label: "Poland (PL)" },
  { data: "PT", label: "Portugal (PT)" },
  { data: "RO", label: "Romania (RO)" },
  { data: "SA", label: "Saudi Arabia (SA)" },
  { data: "SG", label: "Singapore (SG)" },
  { data: "ZA", label: "South Africa (ZA)" },
  { data: "KR", label: "South Korea (KR)" },
  { data: "ES", label: "Spain (ES)" },
  { data: "SE", label: "Sweden (SE)" },
  { data: "CH", label: "Switzerland (CH)" },
  { data: "TW", label: "Taiwan (TW)" },
  { data: "TH", label: "Thailand (TH)" },
  { data: "TR", label: "Turkey (TR)" },
  { data: "UA", label: "Ukraine (UA)" },
  { data: "AE", label: "United Arab Emirates (AE)" },
  { data: "GB", label: "United Kingdom (GB)" },
  { data: "US", label: "United States (US)" },
];

const DNS_OPTIONS = [
  { data: "cloudflare", label: "Cloudflare (1.1.1.1)" },
  { data: "google", label: "Google (8.8.8.8)" },
  { data: "quad9", label: "Quad9 (9.9.9.9)" },
  { data: "custom", label: "Custom" },
];

// Catches render-time exceptions in the panel tree so a bad status shape or
// other unexpected error surfaces a recoverable message instead of Decky
// blanking the panel. The child tree is re-entered on next mount (panel
// reopen), so users can recover without a plugin restart.
class ErrorBoundary extends Component<
  { children: ReactNode },
  { err: Error | null }
> {
  state = { err: null as Error | null };

  static getDerivedStateFromError(err: Error) {
    return { err };
  }

  componentDidCatch(err: Error, info: ErrorInfo) {
    console.error("WiFi Optimizer render error:", err, info);
  }

  render() {
    if (this.state.err) {
      // A retry rather than "close and reopen": the Quick Access Menu keeps
      // the panel mounted across close and open, so reopening alone would
      // show this same message again.
      return (
        <>
          <Banner variant="error">
            WiFi Optimizer hit an unexpected error. If it keeps happening,
            please report at github.com/ArcadaLabs-Jason/WifiOptimizer.
          </Banner>
          <PanelSection>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => this.setState({ err: null })}>
                Try again
              </ButtonItem>
            </PanelSectionRow>
          </PanelSection>
        </>
      );
    }
    return this.props.children;
  }
}

// The single component that owns all panel state. It's intentionally flat:
// every section reads from shared state and state setters are passed down to
// leaf components. Organized top-to-bottom as:
//   1. State and refs
//   2. Stable callbacks (setBusy, runUpdateCheck, refreshStatus, poll helpers)
//   3. Lifecycle effects (main refresh, init, connectivity retry, update timeout,
//      update heartbeat)
//   4. User action handlers (toggles, optimize, reset, backend switch, updates)
//   5. Derived render state (connected, supportTier, allSafeActive, etc.)
//   6. JSX for the panel sections in top-to-bottom screen order
function Content() {
  // --- General status and toggle state ---
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notices, setNotices] = useState<Record<string, string>>({});
  const [busyState, setIsBusy] = useState(false);
  // The key of the change running now, for the text on its row.
  const [working, setWorking] = useState<string | null>(null);
  const [applyingAll, setApplyingAll] = useState(false);
  // The last status read failed, and when the first one is slow to come.
  const [statusFailed, setStatusFailed] = useState(false);
  const [slowLoad, setSlowLoad] = useState(false);
  const [showDetails, setShowDetails] = useState(detailsShown);
  const [dupDismissed, setDupDismissed] = useState(duplicatesDismissed);
  const [dismissed, setDismissed] = useState(findingsDismissed);
  const [optimizeResult, setOptimizeResult] = useState<OptimizeSafeResult | null>(null);
  const [regdomain, setRegdomainInfo] = useState<RegdomainInfo | null>(null);

  // The wireless region is a device-level fact that does not change on its
  // own, so it is read once rather than added to the status poll.
  const refreshRegdomain = useCallback(() => {
    backend
      .getRegdomain()
      .then((info) => setRegdomainInfo(info))
      .catch(() => setRegdomainInfo(null));
  }, []);

  useEffect(() => {
    refreshRegdomain();
  }, [refreshRegdomain]);
  const [customDnsInput, setCustomDnsInput] = useState("");
  // Custom DNS is edited as a draft and applied with a button. Choosing
  // Custom used to send the empty field straight to the backend, which
  // refused it, so the field it gates never appeared and nobody starting
  // from the defaults could ever enter servers. The dirty flag stops the
  // status poll replacing what is being typed, which on the on-screen
  // keyboard takes longer than one poll.
  const [dnsCustomOpen, setDnsCustomOpen] = useState(false);
  const dnsDirtyRef = useRef(false);

  // --- Update flow state ---
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResult | null>(null);
  const [checkingUpdate, setCheckingUpdate] = useState(false);
  const [updating, setUpdating] = useState(false);
  // Why an update could not start or did not finish. Shown under the update
  // buttons, and expires like the other messages.
  const [updateError, setUpdateError] = useState<string | null>(null);
  // A failed change to the channel or the notice toggle. Kept apart, so it
  // is shown beside those controls rather than under the update button.
  const [updateSettingError, setUpdateSettingError] = useState<string | null>(null);
  const [updateProgress, setUpdateProgress] = useState<UpdateProgress | null>(null);

  // --- Backend switch state ---
  const [backendSwitch, setBackendSwitch] = useState<BackendSwitchStatus | null>(null);

  // --- Refs ---
  // busyRef is the synchronous re-entrancy guard; busyState (above) is the
  // React state mirror so UI can respond. setBusy below writes both together.
  const busyRef = useRef(false);
  const backendPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevConnectedRef = useRef<boolean | null>(null);
  const lastUpdateCheckAtRef = useRef<number>(0);
  // Bumped when the update channel changes, so a check that was already
  // running for the old channel cannot put its answer back up.
  const updateCheckSeqRef = useRef(0);
  const statusSeqRef = useRef(0);

  const setBusy = useCallback((val: boolean) => {
    busyRef.current = val;
    setIsBusy(val);
  }, []);

  // Runs checkForUpdate with dedupe - skips if a check was issued within the
  // dedupe window. Lowers GitHub API pressure in CGNAT/dorm scenarios where
  // many devices share an IP. Manual button bypasses this (force=true).
  // A successful answer replaces any update error, which described an older
  // state, except where the caller has just set the error it is about
  // (keepError): a refused update re-checks so its offer is current, and
  // the reason for the refusal has to stay readable.
  const runUpdateCheck = useCallback((force: boolean = false, keepError: boolean = false) => {
    const now = Date.now();
    if (!force && now - lastUpdateCheckAtRef.current < UPDATE_CHECK_DEDUPE_MS) {
      return;
    }
    lastUpdateCheckAtRef.current = now;
    const seq = updateCheckSeqRef.current;
    backend
      .checkForUpdate()
      .then((info) => {
        if (seq !== updateCheckSeqRef.current) return;
        setUpdateInfo(info);
        if (info?.success && !keepError) setUpdateError(null);
      })
      .catch(() => {});
  }, []);

  const refreshStatus = useCallback(async (force: boolean = false) => {
    // Background interval ticks defer while a user operation is in flight;
    // handler-driven refreshes (at the end of an op) force through so the UI
    // catches up immediately instead of waiting for the next interval tick.
    if (!force && busyRef.current) return;
    // Status collection runs off the event loop on the backend, so a slow
    // call can still be in flight when the next tick fires. Without a
    // sequence check the older reply lands last and overwrites the newer
    // one, which shows up as toggles and stats flicking back to stale
    // values a second after they settled.
    const seq = ++statusSeqRef.current;
    try {
      const s = await backend.getStatus();
      if (seq !== statusSeqRef.current) return;
      setStatusFailed(false);
      setStatus(s);
      if (s.settings) {
        // Only adopt a stored value that exists. Older backends cleared
        // dns_servers when DNS was turned off, and copying that empty string
        // back over the field every few seconds made the setting impossible
        // to re-enable: the servers field is only rendered while DNS is on,
        // and turning it on with an empty custom list is rejected.
        if (
          !dnsDirtyRef.current &&
          s.settings.dns_provider === "custom" &&
          s.settings.dns_servers
        ) {
          setCustomDnsInput(s.settings.dns_servers);
        }
      }
    } catch (e) {
      console.error("WiFi Optimizer: failed to get status", e);
      if (seq === statusSeqRef.current) setStatusFailed(true);
    }
  }, []);

  // The refresh at the end of a change, bounded so a backend that stopped
  // answering cannot keep the panel busy.
  const refreshAfterChange = useCallback(
    () => withTimeout(refreshStatus(true), REFRESH_WAIT_MS).catch(() => {}),
    [refreshStatus],
  );

  const stopBackendPoll = () => {
    if (backendPollRef.current) {
      clearInterval(backendPollRef.current);
      backendPollRef.current = null;
    }
  };

  const beginBackendPoll = useCallback(() => {
    stopBackendPoll();
    // One request at a time: an interval callback that outlasts the interval
    // would otherwise stack, and a late one could clear busy in the middle
    // of whatever the user started next.
    let inFlight = false;
    let failures = 0;
    // Once given up or finished, a reply still on its way is ignored, so it
    // cannot clear busy under whatever the user started next.
    let done = false;
    const pollStartedAt = Date.now();
    const giveUp = async (message: string) => {
      done = true;
      stopBackendPoll();
      setBackendSwitch(null);
      setErrors((prev) => ({ ...prev, wifi_backend: message }));
      await refreshAfterChange();
      setBusy(false);
    };
    const tooLong = "The backend switch is taking too long. Restart the device if WiFi does not come back.";
    backendPollRef.current = setInterval(async () => {
      if (done) return;
      // Checked before the in-flight guard: a status call that never
      // settles kept the guard set, and with it every control disabled.
      if (Date.now() - pollStartedAt > BACKEND_SWITCH_MAX_S * 1000) {
        await giveUp(tooLong);
        return;
      }
      if (inFlight) return;
      inFlight = true;
      try {
        const s = await withTimeout(backend.getBackendSwitchStatus(), BACKEND_POLL_TIMEOUT_MS);
        if (done) return;
        failures = 0;
        if (s.in_progress) {
          // A worker stuck on a wedged driver never reports back. Release
          // the panel rather than keep every control disabled forever. The
          // backend's own start time covers a panel opened mid-switch.
          if (s.started_at && Date.now() / 1000 - s.started_at > BACKEND_SWITCH_MAX_S) {
            await giveUp(tooLong);
            return;
          }
          // Mid-switch: update so the subtitle can show the current phase.
          setBackendSwitch(s);
          return;
        }
        // Terminal. Stop polling, then refresh status BEFORE flipping backend
        // state and clearing busy - this keeps the optimistic toggle position
        // steady until live.wifi_backend has caught up AND keeps other toggles
        // disabled until the whole UI has consistent state, avoiding any
        // toggle/untoggle flicker at the end of the switch.
        done = true;
        stopBackendPoll();
        // The same words as the banner at the top, and never the raw command
        // output some failures carry in "detail": that is for the log.
        if (s.result && !s.result.success) {
          if (s.result.detail) console.warn("WiFi Optimizer: backend switch detail", s.result.detail);
          setErrors((prev) => ({
            ...prev,
            wifi_backend: backendResultText(s.result!),
          }));
        }
        await refreshAfterChange();
        setBackendSwitch(s);
        setBusy(false);
      } catch (e) {
        if (done) return;
        console.error("backend switch poll error", e);
        // One failed call is not the switch failing; the worker carries on
        // regardless. Only after several in a row is it lost.
        failures += 1;
        if (failures >= BACKEND_POLL_MAX_FAILURES) {
          await giveUp("Lost track of the backend switch. Check WiFi, then try again.");
        }
      } finally {
        inFlight = false;
      }
    }, BACKEND_POLL_INTERVAL);
  }, [refreshAfterChange, setBusy]);

  // Main refresh interval, paused when the panel is hidden so we don't burn
  // CPU/battery on ~12 subprocess calls every 3s in the background. On return
  // to visible, run one refresh immediately to catch anything that changed
  // while hidden, then resume the interval.
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (id) return;
      refreshStatus();
      id = setInterval(refreshStatus, REFRESH_INTERVAL);
    };
    const stop = () => {
      if (id) {
        clearInterval(id);
        id = null;
      }
    };
    const onVis = () => {
      if (document.hidden) stop();
      else start();
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      stop();
    };
  }, [refreshStatus]);

  // Say something while the first status is slow to come, rather than show
  // an empty section with no sign that anything is happening.
  const loaded = !!status && !status.initializing;
  useEffect(() => {
    if (loaded) return;
    const id = setTimeout(() => setSlowLoad(true), 10 * 1000);
    return () => clearTimeout(id);
  }, [loaded]);

  // A dismissed duplicate notice comes back the next time duplicates are
  // found, not while the same ones are still reported.
  const hasDuplicates = !!status?.external?.duplicate_profiles;
  useEffect(() => {
    if (loaded && !hasDuplicates) {
      duplicatesDismissed = false;
      setDupDismissed(false);
    }
  }, [loaded, hasDuplicates]);

  // The same for the WPA2 and weaker-lock notices: once a dismissed finding
  // is no longer reported, the next one is shown again.
  const hasWpa2Finding = !!status?.wpa2_only_stronger;
  const hasLockWeaker = !!status?.lock_weaker;
  useEffect(() => {
    if (!loaded) return;
    const next = {
      wpa2: findingsDismissed.wpa2 && hasWpa2Finding,
      lockWeaker: findingsDismissed.lockWeaker && hasLockWeaker,
    };
    if (next.wpa2 !== findingsDismissed.wpa2 || next.lockWeaker !== findingsDismissed.lockWeaker) {
      findingsDismissed = next;
      setDismissed(next);
    }
  }, [loaded, hasWpa2Finding, hasLockWeaker]);

  // One-time init: initial update check and resume backend polling if a switch
  // was already in flight when the panel opened.
  useEffect(() => {
    // Initial update check. If it fails (e.g., no network yet), the effect
    // below retries on connectivity recovery. QAM tends to cache the panel
    // across close/open, so we can't rely on remount to retry.
    runUpdateCheck();
    // Resume backend-switch polling if one is in flight (panel was reopened mid-switch)
    backend
      .getBackendSwitchStatus()
      .then((s) => {
        if (s.in_progress) {
          setBackendSwitch(s);
          setBusy(true);
          beginBackendPoll();
        }
      })
      .catch(() => {});
    return () => {
      if (backendPollRef.current) clearInterval(backendPollRef.current);
    };
  }, [beginBackendPoll, runUpdateCheck, setBusy]);

  // Retry update check when connectivity recovers. The initial one-shot check
  // in the mount effect misses the case where the panel was already open when
  // the network came back. Skip until status has loaded to avoid a spurious
  // null-to-true transition firing an extra check on every mount.
  useEffect(() => {
    if (!status) return;
    const connected = status.connected;
    const prev = prevConnectedRef.current;
    prevConnectedRef.current = connected;
    if (prev === false && connected === true) {
      runUpdateCheck();
    }
  }, [status?.connected, runUpdateCheck]);

  // Follow the update script. The watch itself is module level, so a panel
  // closed and reopened mid-update picks the progress back up from here.
  useEffect(
    () =>
      subscribeUpdate((progress) => {
        setUpdateProgress(progress);
        if (progress?.phase === "failed") {
          setUpdating(false);
          setUpdateError(UPDATE_FAILED_TEXT);
        } else if (progress?.phase === "done") {
          // Seen by a panel left open across the reload; the new copy of the
          // plugin is the one running now. Its offer to update is stale too,
          // and kept showing "Update to" the version just installed.
          setUpdating(false);
          setUpdateInfo(null);
          runUpdateCheck(true);
        } else if (progress) {
          setUpdating(true);
        }
      }),
    [runUpdateCheck],
  );

  // Safety timeout: if an update was initiated but the script has reported
  // nothing new for as long as it allows itself, it probably failed (network
  // drop, tarball corruption, etc.). Revert the UI so the user isn't stuck staring
  // at "Updating..." forever, and surface a message they can act on. Started
  // again at each new phase, as the watch in updateWatch.ts is.
  const updatePhase = updateProgress?.phase ?? "";
  useEffect(() => {
    if (!updating) return;
    const id = setTimeout(() => {
      console.error("WiFi Optimizer: update didn't complete in time");
      setUpdating(false);
      setUpdateError(UPDATE_SLOW_TEXT);
    }, UPDATE_TIMEOUT_MS);
    return () => clearTimeout(id);
  }, [updating, updatePhase]);

  // The update messages and the Optimize Safe / Fix now / Force Reapply
  // result expire like the messages under each setting. The result pointed
  // at "failed" marks that had already gone, and a refusal such as "Close
  // your game" stayed after the game was closed.
  useEffect(() => {
    if (!updateError) return;
    const shown = updateError;
    const id = setTimeout(
      () => setUpdateError((prev) => (prev === shown ? null : prev)),
      MESSAGE_TTL_MS,
    );
    return () => clearTimeout(id);
  }, [updateError]);
  useEffect(() => {
    if (!updateSettingError) return;
    const shown = updateSettingError;
    const id = setTimeout(
      () => setUpdateSettingError((prev) => (prev === shown ? null : prev)),
      MESSAGE_TTL_MS,
    );
    return () => clearTimeout(id);
  }, [updateSettingError]);
  useEffect(() => {
    if (!optimizeResult) return;
    const shown = optimizeResult;
    const id = setTimeout(
      () => setOptimizeResult((prev) => (prev === shown ? null : prev)),
      MESSAGE_TTL_MS,
    );
    return () => clearTimeout(id);
  }, [optimizeResult]);

  // Periodic update re-check - QAM often caches the panel across close/reopen,
  // so the mount-effect check doesn't re-fire. This heartbeat catches new
  // releases when the panel has been left open for a while. Paused when the
  // panel/tab is hidden to avoid pointless GitHub calls accumulating while the
  // user isn't looking; re-fires one check immediately on visibility return.
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (id) return;
      id = setInterval(() => runUpdateCheck(), UPDATE_CHECK_INTERVAL);
    };
    const stop = () => {
      if (id) {
        clearInterval(id);
        id = null;
      }
    };
    const onVis = () => {
      if (document.hidden) {
        stop();
      } else {
        runUpdateCheck();
        start();
      }
    };
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      document.removeEventListener("visibilitychange", onVis);
      stop();
    };
  }, [runUpdateCheck]);

  // A switch's result describes that switch, and was left up for as long as
  // the panel stayed mounted, so "Switched to iwd" could still be showing
  // after the backend was changed again from SteamOS. A reboot request is
  // kept: it stays true until the reboot.
  useEffect(() => {
    const r = backendSwitch?.result;
    if (!backendSwitch || backendSwitch.in_progress || !r || r.needs_reboot) return;
    const shown = backendSwitch;
    const id = setTimeout(
      () => setBackendSwitch((prev) => (prev === shown ? null : prev)),
      BACKEND_RESULT_MS,
    );
    return () => clearTimeout(id);
  }, [backendSwitch]);

  // Expire each error and notice MESSAGE_TTL_MS after it first appeared. A
  // message set again with the same text keeps its time; one that changed,
  // or went and came back, starts over.
  const messageSeenRef = useRef<Record<string, { text: string; at: number }>>({});
  useEffect(() => {
    const now = Date.now();
    const seen: Record<string, { text: string; at: number }> = {};
    const shown = [
      ...Object.entries(errors).map(([k, v]) => [`e:${k}`, v] as const),
      ...Object.entries(notices).map(([k, v]) => [`n:${k}`, v] as const),
    ];
    for (const [key, text] of shown) {
      const before = messageSeenRef.current[key];
      seen[key] = before && before.text === text ? before : { text, at: now };
    }
    messageSeenRef.current = seen;
    const times = Object.values(seen).map((m) => m.at);
    if (!times.length) return;
    const id = setTimeout(() => {
      // A second of slack, so a timer that fires a little early still
      // clears the message it was set for.
      const due = Date.now() + 1000 - MESSAGE_TTL_MS;
      const expire = (prefix: string) => (prev: Record<string, string>) => {
        const stale = Object.keys(prev).filter((k) => {
          const m = messageSeenRef.current[prefix + k];
          return m && m.text === prev[k] && m.at <= due;
        });
        if (!stale.length) return prev;
        const next = { ...prev };
        stale.forEach((k) => delete next[k]);
        return next;
      };
      setErrors(expire("e:"));
      setNotices(expire("n:"));
    }, Math.max(0, Math.min(...times) + MESSAGE_TTL_MS - now));
    return () => clearTimeout(id);
  }, [errors, notices]);

  // ---- User action handlers ----
  //
  // Generic toggle handler used by every setter toggle (power_save, BSSID,
  // auto_fix, buffer_tuning, band_preference, DNS, IPv6). Takes a key for
  // error tracking and a backend fn, and hands back the result.
  const handleToggle = async (
    key: string,
    fn: () => Promise<MethodResult>,
  ): Promise<MethodResult | undefined> => {
    if (busyRef.current) return undefined;
    setBusy(true);
    setWorking(key);
    setErrors((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setNotices((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
    setOptimizeResult(null);

    let outcome: MethodResult | undefined;
    try {
      const answer = await withTimeout(fn());
      // A reply without a success flag is not a result the panel can read.
      const result: MethodResult = isResult(answer)
        ? answer
        : { success: false, message: GENERIC_FAIL_TEXT };
      outcome = result;
      // A setter can succeed and still have done something other than what
      // was asked - locking to the access point you were already on because
      // the stronger one would not take, say. Saying nothing there leaves the
      // user with a toggle that flipped and no idea why the outcome differs.
      if (result.success && result.message) {
        setNotices((prev) => ({ ...prev, [key]: result.message as string }));
      }
      if (!result.success) {
        const msg = failureText(result);
        setErrors((prev) => ({ ...prev, [key]: msg }));
      }
      // Wait for WiFi to reconnect before refreshing status
      if (result.reconnected) {
        await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
      }
    } catch (e) {
      // A call that throws (Decky restarting, the backend gone) used to
      // surface as an unhandled rejection with the toggle silently unchanged.
      console.error(`WiFi Optimizer: ${key} call failed`, e);
      setErrors((prev) => ({
        ...prev,
        [key]: e instanceof NoAnswer ? NO_ANSWER_TEXT : UNREACHABLE_TEXT,
      }));
    } finally {
      // Refresh before clearing busy so toggles/badges don't briefly flip to
      // stale pre-operation values in the interim render. Refresh runs even on
      // unexpected errors to keep UI consistent with backend state.
      await refreshAfterChange();
      setWorking(null);
      setBusy(false);
    }
    return outcome;
  };

  // Optimize Safe, the drift banner's Fix now and Force Reapply all run
  // several setters and report per setting. Force Reapply used to go through
  // the single-toggle handler, which dropped every per-setting failure and
  // put its own errors under a key nothing displayed.
  const runBatch = async (fn: () => Promise<OptimizeSafeResult>) => {
    if (busyRef.current) return;
    setBusy(true);
    setApplyingAll(true);
    setErrors({});
    setNotices({});
    setOptimizeResult(null);
    try {
      const answer = await withTimeout(fn());
      const result: OptimizeSafeResult = isResult(answer)
        ? answer
        : { success: false, total: 0, applied: 0, results: {}, message: GENERIC_FAIL_TEXT };
      setOptimizeResult(result);
      if (result.results) {
        // Successes can carry a message too, as they do for a single toggle:
        // a lock that stayed on the access point you were already on, or a
        // fix kept for the next stream. Dropping those here hid the reason.
        const newErrors: Record<string, string> = {};
        const newNotices: Record<string, string> = {};
        for (const [k, v] of Object.entries(result.results)) {
          if (!isResult(v)) {
            newErrors[k] = GENERIC_FAIL_TEXT;
          } else if (!v.success) {
            newErrors[k] = failureText(v);
          } else if (v.message) {
            newNotices[k] = v.message;
          }
        }
        setErrors(newErrors);
        setNotices(newNotices);
      }
      // optimize_safe contains BSSID lock which reconnects
      if (result.reconnected) {
        await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
      }
    } catch (e) {
      console.error("optimize error", e);
      setOptimizeResult({
        success: false, total: 0, applied: 0, results: {},
        message: e instanceof NoAnswer ? NO_ANSWER_TEXT : UNREACHABLE_TEXT,
      });
    } finally {
      // Refresh before clearing applyingAll so button doesn't briefly flip to
      // "All good" from stale status. Refresh runs on errors too.
      await refreshAfterChange();
      setBusy(false);
      setApplyingAll(false);
    }
  };

  const handleOptimize = () => runBatch(() => backend.optimizeSafe());
  const handleRepairDrift = () => runBatch(() => backend.repairDrifted());
  const handleForceReapply = () => runBatch(() => backend.reapplyAll());

  const handleAllowWpa2 = () =>
    handleToggle("allow_wpa2", () => backend.allowWpa2());

  const handleMoveLock = () =>
    handleToggle("move_bssid_lock", () => backend.moveBssidLock());

  const dismissError = (key: string) =>
    setErrors((prev) => {
      const next = { ...prev };
      delete next[key];
      return next;
    });
  // Dismiss hides the notice and any failed attempt's message with it. A
  // finding comes back only once it has cleared and is reported again.
  const dismissFinding = (key: "wpa2" | "lockWeaker", errorKey: string) => {
    dismissError(errorKey);
    findingsDismissed = { ...findingsDismissed, [key]: true };
    setDismissed(findingsDismissed);
  };
  const handleDismissLockWeaker = () => dismissFinding("lockWeaker", "move_bssid_lock");
  const handleDismissWpa2 = () => dismissFinding("wpa2", "allow_wpa2");

  const handleDismissDuplicates = () => {
    duplicatesDismissed = true;
    setDupDismissed(true);
  };

  const handleShowDetails = (on: boolean) => {
    detailsShown = on;
    setShowDetails(on);
  };

  // Reset asks first: see RESET_CONFIRM_TEXT.
  const handleResetSettings = () => {
    if (busyRef.current) return;
    const distro = status?.settings?.distro_id ?? "";
    const description =
      RESET_CONFIRM_TEXT + (distro && distro !== "steamos" ? RESET_BACKEND_TEXT : "");
    showModal(
      <ConfirmModal
        strTitle="Reset WiFi Optimizer?"
        strDescription={description}
        strOKButtonText="Reset"
        strCancelButtonText="Cancel"
        bDestructiveWarning={true}
        onOK={() => void runReset()}
      />,
    );
  };

  const runReset = async () => {
    if (busyRef.current) return;
    setBusy(true);
    setWorking("reset");
    setOptimizeResult(null);
    setErrors({});
    setNotices({});
    resetDnsDraft();
    try {
      const result = await withTimeout(backend.resetSettings());
      if (!isResult(result) || !result.success) {
        setErrors({ reset: isResult(result) ? failureText(result) : GENERIC_FAIL_TEXT });
      } else {
        setNotices({ reset: RESET_DONE_TEXT });
        // Reset keeps the update channel, but an older backend put it back
        // to Stable. Either way the offer on screen may be for a channel no
        // longer chosen, so it is dropped and asked for again.
        updateCheckSeqRef.current += 1;
        setUpdateInfo(null);
        runUpdateCheck(true);
      }
    } catch (e) {
      console.error("reset error", e);
      setErrors({ reset: e instanceof NoAnswer ? NO_ANSWER_TEXT : UNREACHABLE_TEXT });
    } finally {
      await refreshAfterChange();
      // The region is read once, not polled, and Reset can put it back.
      refreshRegdomain();
      setWorking(null);
      setBusy(false);
    }
  };

  // The backend keeps reporting a settings file it had to set aside until
  // told the user has seen it, so the notice survives the panel closing.
  const handleDismissRecovered = async () => {
    try {
      await backend.acknowledgeSettingsRecovered();
    } catch (e) {
      console.error("acknowledge settings recovered error", e);
    }
    await refreshStatus(true);
  };

  // The switch takes WiFi down for up to a minute, so it asks first, as
  // Reset does.
  const handleBackendToggle = (on: boolean) => {
    if (busyRef.current) return;
    const target = on ? "wpa_supplicant" : "iwd";
    const paused =
      !on && (status?.settings?.bssid_lock_enabled || status?.settings?.band_preference_enabled)
        ? " Under iwd, Stop background scanning and the band preference are paused."
        : "";
    showModal(
      <ConfirmModal
        strTitle="Switch the WiFi backend?"
        strDescription={`This switches to ${target}. WiFi disconnects while it switches, usually for less than a minute.${paused} If WiFi doesn't come back, switch back here or restart the device.`}
        strOKButtonText="Switch"
        strCancelButtonText="Cancel"
        onOK={() => void runBackendSwitch(target)}
      />,
    );
  };

  const runBackendSwitch = async (target: string) => {
    // Re-entrancy guard: drop rapid clicks or clicks that land while another
    // operation is already running. Protects against duplicate backend calls
    // and the Force-Reapply/backend-switch overlap case.
    if (busyRef.current) return;
    setBusy(true);
    setErrors((prev) => {
      const next = { ...prev };
      delete next.wifi_backend;
      return next;
    });
    setOptimizeResult(null);

    try {
      // Bounded: a call that never settled kept the panel busy for good.
      const res = await withTimeout(backend.startBackendSwitch(target));
      if (!res.accepted) {
        setErrors((prev) => ({
          ...prev,
          wifi_backend: res.message ?? "Couldn't start the backend switch. Try again in a moment.",
        }));
        // Clear any stale result banner/inline from a prior switch so we don't
        // render old success + new error side-by-side.
        setBackendSwitch(null);
        setBusy(false);
        return;
      }
      setBackendSwitch({
        success: true,
        in_progress: true,
        phase: "switching",
        target,
        started_at: Math.floor(Date.now() / 1000),
        result: null,
      });
      beginBackendPoll();
    } catch (e) {
      console.error("startBackendSwitch error", e);
      // The reply may be what was lost, with the switch running anyway.
      // Follow it then, rather than say it never started.
      try {
        const s = await withTimeout(backend.getBackendSwitchStatus(), BACKEND_POLL_TIMEOUT_MS);
        if (s.in_progress) {
          setBackendSwitch(s);
          beginBackendPoll();
          return;
        }
      } catch {
        // Nothing to follow.
      }
      setBusy(false);
      setErrors((prev) => ({
        ...prev,
        wifi_backend: "Couldn't start the backend switch. Try again in a moment.",
      }));
    }
  };

  // Update-flow handlers, used by the top update banner and the Updates
  // section. Share the updating/updateError state so either surface can
  // initiate a check or apply an update.
  const handleApplyUpdate = async () => {
    setUpdateError(null);
    setUpdating(true);
    try {
      // The version the user was shown. The backend checks GitHub again and
      // installs only if it still offers this one, so a "Go back to" is
      // never replaced by something they did not choose.
      const result = (await backend.applyUpdate(updateInfo?.latest_version ?? "")) as MethodResult & {
        version?: string;
      };
      // Refused outright (no update after all, a backend switch running):
      // say why now instead of showing "Updating..." for a minute. Then ask
      // GitHub again, so the offer on screen matches what it offers now: a
      // refusal for "no update" or "the update changed" left the old offer
      // up, and the Check button is hidden while an offer is shown.
      if (!result.success) {
        setUpdating(false);
        setUpdateError(result.message ?? "Couldn't start the update.");
        updateCheckSeqRef.current += 1;
        runUpdateCheck(true, true);
        return;
      }
      const version = result.version ?? updateInfo?.latest_version;
      if (version) watchUpdate(version, true);
    } catch {
      // The script may well be running anyway. Follow it, so the plugin is
      // still reloaded when the files are in place rather than Steam
      // restarted; if nothing started, the watch and the safety timeout end.
      if (updateInfo?.latest_version) watchUpdate(updateInfo.latest_version, true);
    }
  };

  const handleCheckForUpdate = async () => {
    setCheckingUpdate(true);
    setUpdateError(null);
    lastUpdateCheckAtRef.current = Date.now();
    const seq = updateCheckSeqRef.current;
    try {
      // Bounded, or "Checking..." could stay up with the button disabled.
      const result = await withTimeout(backend.checkForUpdate());
      if (seq === updateCheckSeqRef.current) setUpdateInfo(result);
    } catch {
      // refreshStatus has its own catch; no need to log here
    }
    setCheckingUpdate(false);
  };

  const handleNotifyChange = async (enabled: boolean) => {
    try {
      const result = await withTimeout(backend.setUpdateNotify(enabled), REFRESH_WAIT_MS);
      if (!result.success) {
        setUpdateSettingError(result.message ?? "Couldn't change the update notice setting. Try again.");
        return;
      }
      setUpdateSettingError(null);
    } catch (e) {
      console.error("set update notify error", e);
      setUpdateSettingError("Couldn't change the update notice setting. Try again.");
    }
    await refreshAfterChange();
  };

  const handleChannelChange = async (nextChannel: string) => {
    try {
      const result = await withTimeout(backend.setUpdateChannel(nextChannel), REFRESH_WAIT_MS);
      if (!result.success) {
        setUpdateSettingError(result.message ?? "Couldn't change the update channel. Try again.");
        return;
      }
      updateCheckSeqRef.current += 1;
      setUpdateSettingError(null);
      setUpdateError(null);
      setUpdateInfo(null);
    } catch (e) {
      console.error("set channel error", e);
      setUpdateSettingError("Couldn't change the update channel. Try again.");
      return;
    }
    // Forced like the notify toggle. Unforced, it is skipped while another
    // operation holds the panel busy, and the dropdown shows the old channel.
    await refreshStatus(true);
    // The answer for the old channel was dropped above. Ask again, or the
    // panel offers nothing for the new channel until the next timed check.
    await handleCheckForUpdate();
  };

  // Custom DNS draft handling
  const resetDnsDraft = () => {
    dnsDirtyRef.current = false;
    setDnsCustomOpen(false);
    setCustomDnsInput(
      status?.settings?.dns_provider === "custom" ? status.settings.dns_servers : ""
    );
  };

  const applyCustomDns = async () => {
    const result = await handleToggle("dns", async () => {
      const r = await backend.setDns(true, "custom", customDnsInput.trim());
      // Before the refresh that follows, so it adopts the servers as the
      // backend saved them. Cleared after it, Apply and Cancel stayed up
      // until the next poll whenever the saved form differed from the typed.
      if (r.success) dnsDirtyRef.current = false;
      return r;
    });
    if (result?.success) setDnsCustomOpen(false);
  };

  // Don't render content until first status arrives (prevents disconnect flash).
  // A status marked initializing is the backend saying a collection is still
  // running and it has nothing to report yet, which is the same situation.
  if (!status || status.initializing) {
    return (
      <PanelSection title="WiFi Optimizer">
        <PanelSectionRow>
          <div style={{ fontSize: theme.fontSize.body, color: theme.text.secondary }}>
            {statusFailed
              ? "Couldn't reach the plugin. It keeps trying. If this stays, restart the device."
              : slowLoad
                ? "Still reading WiFi status. This can take a while just after waking from sleep."
                : "Loading WiFi status..."}
          </div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const s = status?.settings;
  // A change the backend is still making, which may have outlasted the
  // panel's own wait for it. Every control waits for it too.
  const changeRunning = !!status?.profile_change_in_flight;
  const isBusy = busyState || changeRunning;
  const connected = status?.connected ?? false;
  const supportTier = status?.support_tier ?? 1;
  const driftCount = status?.drift ? Object.keys(status.drift).length : 0;
  const supports6GHz = s?.supports_6ghz ?? false;
  // One band preference, shown as two toggles: 5 GHz and, where it can work, 6 GHz.
  const on6GHz = !!s?.band_preference_enabled && s?.band_preference === "6GHz";
  // Each toggle reports under its own key; band_preference is what Force
  // Reapply and Fix now report under, shown on whichever band is set.
  const band5Error = errors.band_5ghz ?? (on6GHz ? undefined : errors.band_preference);
  const band6Error = errors.band_6ghz ?? (on6GHz ? errors.band_preference : undefined);
  // A result from one band toggle is stale once the other is used.
  const clearBandMessages = () => {
    const drop = (prev: Record<string, string>) => {
      const next = { ...prev };
      delete next.band_5ghz;
      delete next.band_6ghz;
      delete next.band_preference;
      return next;
    };
    setErrors(drop);
    setNotices(drop);
  };
  const isDeckLcd = s?.device_family === "deck_lcd";
  const isDeck = isDeckLcd || s?.device_family === "deck_oled";
  // The badge and line for a setting that is on for another network.
  const otherNetworkBadge = { badge: "unknown" as const, text: "other network" };
  const bandOther = !!status?.live?.band_other_network && !!s?.band_preference_enabled;
  // A band of 2.4 GHz is only reachable by editing the settings file, or
  // from an old version. Shown under the 5 GHz toggle, which is the one on.
  const onBg = !!s?.band_preference_enabled && s?.band_preference === "bg";
  // What has drifted, by name.
  const driftNames = Object.keys(status?.drift ?? {}).map((k) => DRIFT_LABELS[k] ?? k);
  // "Steam Deck OLED, WiFi 6E (QCA206X)", leaving out whichever part is unknown.
  const deviceParts = [
    s?.device_label && s.device_label !== "Unknown Device" ? s.device_label : "",
    s?.chip_label && s.chip_label !== "unknown" ? s.chip_label : "",
  ].filter(Boolean);

  // Streaming auto mode with no stream running: power save, buffer tuning
  // and CAKE are off on purpose. They read as paused, not as off or drifted.
  const streamingIdle = !!status?.live?.streaming_mode && !status?.live?.streaming_active;
  const gatedBadge = (key: string, on: boolean | undefined) =>
    streamingIdle && on && !errors[key]
      ? { badge: "paused" as const, text: "paused" }
      : getBadge(key, status, errors[key] ?? null);

  // Under iwd Optimize Safe leaves the lock out, so it is not one of the
  // things to wait for there, or "All good" never came.
  // The findings with a Dismiss, unless dismissed.
  const wpa2Shown = !!status?.wpa2_only_stronger && !dismissed.wpa2;
  const lockWeakerShown = !!status?.lock_weaker && !dismissed.lockWeaker;

  // What the first-run prompt says is on already.
  const alreadyOn = [
    s?.power_save_disabled ? "Prevent lag spikes" : "",
    s?.auto_fix_on_wake ? "Auto-fix on wake" : "",
  ].filter(Boolean);

  // Why there is no connection, where the backend can tell (older backends
  // send nothing, which reads as plainly disconnected). "Connect first,
  // then optimize" only before anything has been applied.
  const wifiState = status?.live?.wifi_state ?? "disconnected";
  const disconnectedText =
    wifiState === "no_device" || wifiState === "not_responding" || wifiState === "unavailable"
      ? "The WiFi card isn't responding. Restart the device."
      : wifiState === "radio_off"
        ? "WiFi is off. Turn it on in Steam's settings."
        : s?.last_applied
          ? "Not connected to WiFi."
          : "Not connected to WiFi. Connect first, then optimize.";

  // A lock that is on for another network counts as done too: Optimize Safe
  // leaves it where it was set (_lock_held_elsewhere in main.py), so waiting
  // for it here meant "All good" never came on any other network. The row
  // itself says the lock does nothing here.
  const lockSafe =
    status?.live?.wifi_backend === "iwd" ||
    (s?.bssid_lock_enabled &&
      (!!status?.live?.bssid_lock_other_network || !status?.drift?.bssid_lock));

  // Check if all safe optimizations are already active
  const allSafeActive =
    !!connected &&
    (status?.live?.power_save_off || (streamingIdle && s?.power_save_disabled)) &&
    !status?.drift?.power_save &&
    lockSafe &&
    status?.live?.dispatcher_installed &&
    (status?.live?.buffer_tuning_applied || (streamingIdle && s?.buffer_tuning_enabled)) &&
    !status?.drift?.buffer_tuning;

  return (
    <DetailsContext.Provider value={showDetails}>
      <PanelHeader
        driver={s?.driver ?? ""}
        lastApplied={s?.last_applied ?? 0}
        lastEnforced={status?.live?.last_enforced}
        streaming={status?.live}
        deviceLabel={deviceParts.length ? deviceParts.join(", ") : undefined}
        showDetails={showDetails}
        onShowDetails={handleShowDetails}
      />

      {/* A status read failed after the panel had loaded. What is shown is
          the last status that came through. */}
      {statusFailed && (
        <Banner variant="warning">
          Lost contact with the plugin, so this may be out of date. It keeps
          trying. If this stays, restart the device.
        </Banner>
      )}

      {/* The settings file could not be saved. Changes then hold until the
          plugin restarts, and the toggles still read as if they had stuck. */}
      {status?.settings_unwritable && (
        <Banner variant="warning">
          The plugin couldn't save its settings, so changes may not last past
          a restart. Check that the device has free storage space, then try
          the change again.
        </Banner>
      )}

      {changeRunning && !busyState && (
        <Banner variant="info">
          A WiFi change is still finishing. The controls come back when it is
          done.
        </Banner>
      )}

      {/* Unknown hardware info */}
      {supportTier === 3 && (
        <Banner variant="info">
          Unknown WiFi hardware detected. Some optimizations may not apply.
        </Banner>
      )}

      {/* The settings file could not be read and was set aside. Every toggle
          has just gone back to its default, and without this it looks as if
          the plugin forgot them for no reason. */}
      {status?.settings_recovered && (
        <Banner variant="warning">{ERROR_MESSAGES.parse_error}</Banner>
      )}
      {status?.settings_recovered && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleDismissRecovered}>
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
      <WakeFailureNotice />

      {/* Update available. A refusal is said here too: the Updates section
          is far down the panel, and pressing this showed nothing. */}
      {updateInfo?.update_available && !updating && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleApplyUpdate}>
              {updateInfo.is_downgrade
                ? `Go back to v${updateInfo.latest_version}`
                : `Update to v${updateInfo.latest_version}`}
            </ButtonItem>
          </PanelSectionRow>
          {updateError && (
            <PanelSectionRow>
              <div style={{ fontSize: theme.fontSize.body, color: theme.warning.text }}>
                {updateError}
              </div>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      {/* Updating */}
      {updating && (
        <PanelSection>
          <PanelSectionRow>
            <div style={{ fontSize: theme.fontSize.body, color: theme.info.text }}>
              {updatingText(updateProgress)}
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* First-run prompt. Prevent lag spikes and Auto-fix on wake are on
          from install (and Auto-fix after a Reset), so it says which are
          already on rather than reading as if nothing were. */}
      {connected && !s?.last_applied && (
        <Banner variant="info">
          {alreadyOn.length > 0 &&
            `${joinNames(alreadyOn)} ${alreadyOn.length > 1 ? "are" : "is"} already on. `}
          Tap <strong>Optimize Safe</strong> to turn on the rest of the recommended settings.
        </Banner>
      )}

      {/* Several saved copies of this network, disagreeing about security.
          The old wording here told people to forget the network and rejoin.
          That throws away their settings and gambles on which security the
          new copy gets, when the actual problem is only that the copies rank
          equally. Say what was done instead, and how to choose the other.
          Priority is only claimed when the backend says it was written. */}
      {hasDuplicates && !dupDismissed && (
        <Banner variant="warning">
          This network is saved more than once with different security
          settings. Which copy is used decides which access points can be
          reached, so the connection can look inconsistent.{" "}
          {status?.duplicates_prioritized
            ? "The copy in use has been given priority, so the choice stays the same from now on. To use the other one instead, connect with it once and it takes priority."
            : "Usually the copy used last is picked next time. If the connection keeps acting up, remove the extra copy in Desktop Mode's network settings."}
        </Banner>
      )}
      {hasDuplicates && !dupDismissed && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={handleDismissDuplicates}>
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* The router handed out an address another device already holds. WiFi
          reads as connected while nothing works, and without this the plugin
          is the obvious suspect. */}
      {status?.address_conflict && (
        <Banner variant="warning">
          Another device on your network is using the same address as this
          one ({status.address_conflict.ip}), so the connection may not work. That's
          the router, not a WiFi setting. Restarting the router usually fixes it.
        </Banner>
      )}

      {/* The saved security is shutting the device out of a stronger access
          point on the same network. Without this the only symptom is the
          Deck preferring a distant access point for no visible reason. */}
      {/* Kept up for a failed attempt's message as well, as the lock's
          banner below is: a finding that clears would take the reason away
          with it. */}
      {/* Access points that offer both keep using WPA3, but before
          NetworkManager 1.58.1 a profile allowed WPA2 does not join one that
          offers only WPA3 on its own. The backend only reports this where no
          such access point is in view (or NetworkManager is new enough), and
          Reset puts WPA3 only back. */}
      {(wpa2Shown || errors.allow_wpa2) && (
        <Banner variant="info">
          {status?.wpa2_only_stronger && wpa2Shown && (
            <>
              A stronger access point on this network (signal{" "}
              {status.wpa2_only_stronger.signal} of 100, against{" "}
              {status.wpa2_only_stronger.current_signal} for the one in use) only
              offers WPA2, and this network is saved as WPA3-only, so this device
              can't use it. Access points that offer both keep using WPA3, but
              one that offers only WPA3 may then not be joined automatically.
              Only allow this if that access point is yours:
              anyone nearby can broadcast your network's name. Allowing it
              reconnects WiFi. Reset settings puts it back to WPA3 only.
            </>
          )}
          {errors.allow_wpa2 && (
            <div style={{ color: theme.error.text }}>{errors.allow_wpa2}</div>
          )}
        </Banner>
      )}
      {/* A real button, so it can be reached with the D-pad. */}
      {wpa2Shown && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleAllowWpa2}>
              {working === "allow_wpa2" ? WORKING_TEXT : "Allow WPA2 here"}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
      {/* Declining is a choice too: the notice stays away until the finding
          clears and comes back. */}
      {(wpa2Shown || errors.allow_wpa2) && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleDismissWpa2}>
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* The lock is holding the Deck on an access point it has been carried
          away from, while a much stronger one of the same network is in
          range. Moving reconnects, so it is offered rather than done. The
          banner also stays up for a failed move's message, because a failed
          move clears the finding and would otherwise take the reason away
          with it. That message can be dismissed, and expires like the
          others. */}
      {(lockWeakerShown || errors.move_bssid_lock) && (
        <Banner variant="warning">
          {status?.lock_weaker && lockWeakerShown && (
            <>
              The access point lock is holding you on a weaker access point
              (signal {status.lock_weaker.current_signal} of 100). A stronger one is in
              range (signal {status.lock_weaker.signal} of 100). Moving the lock
              reconnects WiFi for a few seconds.
            </>
          )}
          {errors.move_bssid_lock && (
            <div style={{ color: theme.error.text }}>{errors.move_bssid_lock}</div>
          )}
        </Banner>
      )}
      {/* A real button, so it can be reached with the D-pad. */}
      {lockWeakerShown && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleMoveLock}>
              {working === "move_bssid_lock" ? WORKING_TEXT : "Move the lock"}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
      {(lockWeakerShown || errors.move_bssid_lock) && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleDismissLockWeaker}>
              Dismiss
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* A successful move can still say something, such as staying put. */}
      {notices.move_bssid_lock && (
        <Banner variant="info">{notices.move_bssid_lock}</Banner>
      )}

      {/* A setting we wrote was the likely reason the device could not
          connect, so it was released. Worth stating plainly: nothing the user
          did caused it, and the preference is not lost. */}
      {status?.external?.pin_released && (
        <Banner variant="warning">
          WiFi could not connect for a while, so the access point lock or the
          band preference was released to let it reconnect. Both stay on. The
          lock is set again on whichever access point works. The band
          preference comes back after about 10 minutes, once an access point
          on that band is in range.
        </Banner>
      )}

      {/* Drift alert */}
      {connected && !!s?.last_applied && driftCount > 0 && (
        <Banner variant="warning">
          {driftCount > 1
            ? `These settings no longer match what you set: ${driftNames.join(", ")}. Fix now puts them back.`
            : `${driftNames[0]} no longer matches what you set. Fix now puts it back.`}
        </Banner>
      )}
      {connected && !!s?.last_applied && driftCount > 0 && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleRepairDrift}>
              Fix now
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Status read failed - say so rather than claiming disconnection */}
      {status?.success === false && (
        <Banner variant="error">
          {status?.message ?? "Couldn't read WiFi status."} The panel keeps
          trying. If this stays, restart the device.
        </Banner>
      )}

      {/* Disconnected banner. A dead or missing card is not something
          connecting fixes, so it says what to do for each case. */}
      {status?.success !== false && !connected && (
        <Banner variant="error">{disconnectedText}</Banner>
      )}

      {/* Backend switch result banner */}
      {backendSwitch && !backendSwitch.in_progress && backendSwitch.result && (() => {
        const r = backendSwitch.result;
        // Treat reconnect timeout as a warning even when the backend-level
        // switch succeeded - the system is switched but WiFi didn't come back.
        const isWarning = !r.success || r.needs_reboot || r.reconnect_timed_out;
        return (
          <Banner variant={isWarning ? "warning" : "success"}>
            {backendResultText(r)}
          </Banner>
        );
      })()}

      {/* Optimize result banner */}
      {optimizeResult && (() => {
        // A failure has no counts at all, and undefined === undefined made
        // it read as "All applied".
        if (optimizeResult.success === false) {
          return (
            <Banner variant="error">
              {optimizeResult.message ?? GENERIC_FAIL_TEXT}
            </Banner>
          );
        }
        if (!optimizeResult.total) {
          return (
            <Banner variant="info">
              {optimizeResult.message ?? "Nothing to apply."}
            </Banner>
          );
        }
        const all = optimizeResult.applied === optimizeResult.total;
        // In streaming auto mode with no stream running, these were saved
        // but not applied, so "All applied" was not true.
        const paused = Object.entries(optimizeResult.results ?? {})
          .filter(([, v]) => isResult(v) && v.success && v.paused)
          .map(([k]) => DRIFT_LABELS[k] ?? k);
        const pausedText = paused.length
          ? `${joinNames(paused)} ${paused.length > 1 ? "apply" : "applies"} only while a streaming app is running.`
          : "";
        if (all && paused.length) {
          return <Banner variant="info">{`Saved. ${pausedText}`}</Banner>;
        }
        return (
          <Banner variant={all ? "success" : "warning"}>
            {all
              ? "All applied"
              : `${optimizeResult.applied} of ${optimizeResult.total} applied. The settings marked failed below say why.${pausedText ? " " + pausedText : ""}`}
          </Banner>
        );
      })()}

      {/* Optimize button */}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            // "All good" is a state, not an action: pressing it ran Optimize
            // Safe again, which can reconnect WiFi for nothing.
            disabled={!connected || applyingAll || isBusy || updating || allSafeActive}
            onClick={handleOptimize}
          >
            {applyingAll
              ? "Applying..."
              : allSafeActive
                ? "All good"
                : "Optimize Safe"}
          </ButtonItem>
        </PanelSectionRow>
        {applyingAll && (
          <PanelSectionRow>
            <div style={{ fontSize: theme.fontSize.small, color: theme.info.text }}>
              {BATCH_WORKING_TEXT}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* Power & stability */}
      <PanelSection title="Power & stability">
        <InfoRow
          label="Prevent lag spikes"
          subtitle="Disables WiFi power save and PCIe power states"
          explanation="The OS saves WiFi power in several places: the wireless chip, the PCIe link between the chip and the CPU, and low power modes in the driver. These can cause latency spikes, packets sent in batches, and lower throughput during a long stream. This turns them off, so the WiFi hardware stays fully awake. It uses a little more battery."
          {...gatedBadge("power_save", s?.power_save_disabled)}
          checked={s?.power_save_disabled ?? false}
          disabled={isBusy || updating}
          error={errors.power_save}
          notice={notices.power_save}
          onChange={(val: boolean) =>
            handleToggle("power_save", () => backend.setPowerSave(val))
          }
        />
        <InfoRow
          label="Stop background scanning"
          subtitle="Pins one access point. Turn off to roam."
          explanation="While connected, your device scans now and then for a better access point. Each scan takes the radio off your channel for a moment, which can drop packets and make a game stream stutter. Pinning the connection to one access point turns off these roaming scans. It picks the strongest access point of your network, unless the one you are on is already good. The lock only applies to the network you turn it on for, so other networks are not affected. On that network you stay on the pinned access point until you turn this off or move the lock. Turning it on or off reconnects WiFi for a few seconds."
          {...(status?.live?.bssid_lock_other_network && !errors.bssid_lock
            ? otherNetworkBadge
            : getBadge("bssid_lock", status, errors.bssid_lock ?? null))}
          checked={s?.bssid_lock_enabled ?? false}
          disabled={isBusy || updating || (!connected && !s?.bssid_lock_enabled)}
          working={working === "bssid_lock"}
          error={errors.bssid_lock}
          notice={
            notices.bssid_lock ??
            (status?.live?.pins_inactive_iwd && s?.bssid_lock_enabled
              ? IWD_PIN_NOTICE
              : status?.live?.bssid_lock_other_network && s?.bssid_lock_enabled
              ? OTHER_NETWORK_TEXT
              : status?.external?.bssid_lock
              ? "This network is already pinned to one access point by a setting outside this plugin. Turn this on and off to clear it."
              : undefined)
          }
          onChange={(val: boolean) =>
            handleToggle("bssid_lock", () => backend.setBssidLock(val))
          }
        />
        <InfoRow
          label="Auto-fix on wake"
          subtitle="Reapplies settings after sleep"
          explanation="The OS often resets WiFi settings after sleep or updates. This installs a small script that automatically re-applies your optimizations every time the WiFi reconnects. It runs outside of Decky, so it works even if Decky has issues. Removing the plugin will also remove this script."
          {...getBadge(undefined, status, errors.auto_fix ?? null)}
          checked={s?.auto_fix_on_wake ?? false}
          disabled={isBusy || updating}
          error={errors.auto_fix}
          notice={notices.auto_fix}
          onChange={(val: boolean) =>
            handleToggle("auto_fix", () => backend.setAutoFix(val))
          }
        />
        <InfoRow
          label="Network buffer tuning"
          subtitle="Optimize UDP buffers and TX queue for streaming"
          explanation="Increases the kernel's network buffer sizes and transmit queue length to handle the bursty UDP traffic that game streaming produces. Without this, packets can be dropped during high-bitrate moments, causing frame drops or brief quality dips. These settings apply to every network connection, wired ones too. They reset on every reboot, and are put back when WiFi connects if Auto-fix on wake is on."
          {...gatedBadge("buffer_tuning", s?.buffer_tuning_enabled)}
          checked={s?.buffer_tuning_enabled ?? false}
          disabled={isBusy || updating}
          error={errors.buffer_tuning}
          notice={notices.buffer_tuning}
          onChange={(val: boolean) =>
            handleToggle("buffer_tuning", () => backend.setBufferTuning(val))
          }
        />
      </PanelSection>

      {/* Advanced */}
      <PanelSection title="Advanced">
        {/* A radio that carries its own regulatory domain ignores this
            setting entirely. Offering a control that cannot do anything is
            worse than offering none, so the two cases render differently:
            a selector where it works, an explanation where it does not. */}
        {regdomain?.readable && regdomain.changeable && (
          <PanelSectionRow>
            <DropdownItem
              label="WiFi region"
              description="Which channels this device may use. Set it to where you actually are. The wrong region can hide 5 GHz and 6 GHz channels entirely."
              rgOptions={[
                // Not set by the plugin: labelled with the region in use.
                // Once the plugin has set one, choosing this puts back the
                // region from before, so it says that instead of naming the
                // plugin's own region.
                {
                  data: REGION_UNMANAGED,
                  label: regdomain.setting
                    ? "Not managed (put back the region from before)"
                    : `Not managed (${regdomain.governing || "unknown"})`,
                },
                ...REGION_OPTIONS,
                ...(regdomain.setting &&
                !REGION_OPTIONS.some((o) => o.data === regdomain.setting)
                  ? [{ data: regdomain.setting, label: regdomain.setting }]
                  : []),
              ]}
              selectedOption={regdomain.setting || REGION_UNMANAGED}
              disabled={isBusy || updating}
              onChange={(option: { data: string }) => {
                const unmanaged = option.data === REGION_UNMANAGED;
                // "" puts back the region from before the plugin set one,
                // or World when there was none, so it is only sent to stop
                // managing a region the plugin did set.
                if (unmanaged && !regdomain.setting) return;
                void handleToggle("regdomain", () =>
                  backend.setRegdomain(unmanaged ? "" : option.data)
                ).then(refreshRegdomain);
              }}
            />
          </PanelSectionRow>
        )}
        {(errors.regdomain || notices.regdomain) && (
          <PanelSectionRow>
            <div
              style={{
                fontSize: theme.fontSize.tiny,
                color: errors.regdomain ? theme.error.text : theme.text.tertiary,
              }}
            >
              {errors.regdomain ?? notices.regdomain}
            </div>
          </PanelSectionRow>
        )}
        {regdomain?.readable && !regdomain.changeable && (
          <PanelSectionRow>
            <div style={{ fontSize: "12px", opacity: 0.8, padding: "4px 0" }}>
              WiFi region: {regdomain.governing || "unknown"}. This device's
              WiFi card sets its own region, so it cannot be changed here. That
              is normal for many cards{isDeck ? ", the Steam Deck's included," : ""} and
              does not mean a band is missing.
            </div>
          </PanelSectionRow>
        )}
        <InfoRow
          label="Force 5 GHz band"
          subtitle="Avoid 2.4 GHz Bluetooth interference"
          explanation={`Bluetooth operates on the 2.4 GHz band${
            isDeckLcd ? ", and on the Steam Deck LCD the antennas are shared" : ""
          }. Using 5 GHz for WiFi avoids this interference entirely, giving you a cleaner, faster connection. Only turn this on if your router has 5 GHz. If no 5 GHz access point for this network is in range when you turn it on, it is refused instead of disconnecting you. Turning it on or off reconnects WiFi for a few seconds.${
            supports6GHz
              ? status?.live?.six_ghz_offered || on6GHz
                ? " This keeps the device on 5 GHz specifically, so leave it off if you connect over 6 GHz. To stay on 6 GHz, use Force 6 GHz band below instead."
                : " This keeps the device on 5 GHz specifically, so leave it off if you connect over 6 GHz."
              : ""
          }`}
          {...(bandOther && !on6GHz && !band5Error
            ? otherNetworkBadge
            : getBadge(on6GHz ? undefined : "band_preference", status, band5Error ?? null))}
          checked={(s?.band_preference_enabled ?? false) && !on6GHz}
          disabled={isBusy || updating || (!connected && !(s?.band_preference_enabled && !on6GHz))}
          working={working === "band_5ghz"}
          error={band5Error}
          notice={
            notices.band_5ghz ??
            (on6GHz
              ? undefined
              : notices.band_preference ??
                (status?.live?.pins_inactive_iwd && s?.band_preference_enabled
                  ? IWD_PIN_NOTICE
                  : bandOther
                  ? OTHER_NETWORK_TEXT
                  : onBg
                  ? "This network is held to 2.4 GHz by an older setting, not 5 GHz. Turn this off, then on again, to use 5 GHz."
                  : status?.external?.band
                  ? "This network is already limited to one band by a setting outside this plugin. Turn this on and off to clear it."
                  : undefined))
          }
          onChange={(val: boolean) => {
            clearBandMessages();
            handleToggle("band_5ghz", () =>
              // Turning it on always asks for 5 GHz, which is what the
              // toggle says, even over a 2.4 GHz setting from an old
              // version. Never "6GHz": turning that off leaves it recorded
              // as the band.
              val
                ? backend.setBandPreference(true, "a")
                : backend.setBandPreference(val, s?.band_preference === "bg" ? "bg" : "a")
            );
          }}
        />
        {(status?.live?.six_ghz_offered || on6GHz) && (
          <InfoRow
            label="Force 6 GHz band"
            subtitle="Stay on 6 GHz where your router has it"
            explanation="6 GHz is the least crowded band, with the most room for wide channels, but it reaches less far than 5 GHz and only routers with WiFi 6E or newer offer it. This keeps the device on 6 GHz for this network. It needs WPA3. If this network can't use it, turning it on says why. If the device can't connect on 6 GHz when you turn this on, the band is put back the way it was. Turning it on or off reconnects WiFi. Like the 5 GHz setting, if you later take the device out of range it is released after about a minute so WiFi can reconnect."
            {...(bandOther && on6GHz && !band6Error
              ? otherNetworkBadge
              : getBadge(on6GHz ? "band_preference" : undefined, status, band6Error ?? null))}
            checked={on6GHz}
            disabled={isBusy || updating || (!connected && !on6GHz)}
            working={working === "band_6ghz"}
            error={band6Error}
            notice={
              notices.band_6ghz ??
              (on6GHz
                ? notices.band_preference ??
                  (status?.live?.pins_inactive_iwd
                    ? IWD_PIN_NOTICE
                    : bandOther
                      ? OTHER_NETWORK_TEXT
                      : undefined)
                : undefined)
            }
            onChange={(val: boolean) => {
              clearBandMessages();
              handleToggle("band_6ghz", () => backend.setBandPreference(val, "6GHz"));
            }}
          />
        )}
        <InfoRow
          label="Custom DNS"
          subtitle="Override DNS servers for this network"
          explanation="Your internet provider's DNS servers translate domain names into IP addresses. They can be slow or unreliable. Switching to a public DNS like Cloudflare (1.1.1.1) or Google (8.8.8.8) can speed up initial connections and improve reliability. This only affects the WiFi network you set it on, and replaces that network's own DNS servers, including any it offers over IPv6. The servers used are IPv4 addresses, so on the rare network with no IPv4 at all, websites would stop loading; leave this off there. Each change reconnects WiFi for a few seconds."
          {...(status?.live?.dns_other_network && s?.dns_enabled && !errors.dns
            ? otherNetworkBadge
            : getBadge("dns", status, errors.dns ?? null))}
          checked={s?.dns_enabled ?? false}
          disabled={isBusy || updating || (!connected && !s?.dns_enabled)}
          working={working === "dns"}
          error={errors.dns}
          notice={
            notices.dns ??
            (status?.live?.dns_other_network && s?.dns_enabled ? OTHER_NETWORK_TEXT : undefined)
          }
          onChange={(val: boolean) => {
            if (!val) resetDnsDraft();
            handleToggle("dns", () =>
              backend.setDns(val, s?.dns_provider ?? "cloudflare", customDnsInput)
            );
          }}
        >
          {s?.dns_enabled && (
            <>
              <PanelSectionRow>
                <DropdownItem
                  label="DNS provider"
                  rgOptions={DNS_OPTIONS}
                  selectedOption={dnsCustomOpen ? "custom" : s?.dns_provider ?? "cloudflare"}
                  disabled={isBusy || updating}
                  onChange={(option: { data: string }) => {
                    if (option.data === "custom") {
                      // Open the editor; nothing is sent until Apply.
                      setDnsCustomOpen(true);
                      return;
                    }
                    resetDnsDraft();
                    handleToggle("dns", () =>
                      backend.setDns(true, option.data, "")
                    );
                  }}
                />
              </PanelSectionRow>
              {(dnsCustomOpen || s?.dns_provider === "custom") && (
                <>
                  {/* No submit on blur: moving focus with the D-pad blurs
                      the field, and each submit reconnects WiFi. */}
                  <PanelSectionRow>
                    <TextField
                      label="DNS servers (space-separated)"
                      value={customDnsInput}
                      onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
                        dnsDirtyRef.current = true;
                        setCustomDnsInput(e.target.value);
                      }}
                    />
                  </PanelSectionRow>
                  {(dnsCustomOpen ||
                    customDnsInput.trim() !== (s?.dns_servers ?? "")) && (
                    <>
                      <PanelSectionRow>
                        <ButtonItem
                          layout="below"
                          disabled={isBusy || updating || !customDnsInput.trim()}
                          onClick={applyCustomDns}
                        >
                          Apply DNS servers
                        </ButtonItem>
                      </PanelSectionRow>
                      <PanelSectionRow>
                        <ButtonItem
                          layout="below"
                          disabled={isBusy || updating}
                          onClick={resetDnsDraft}
                        >
                          Cancel
                        </ButtonItem>
                      </PanelSectionRow>
                    </>
                  )}
                </>
              )}
            </>
          )}
        </InfoRow>
        <InfoRow
          label="Disable IPv6"
          subtitle="Use IPv4 only on this network"
          explanation="Some networks have poor or misconfigured IPv6 support, which can cause slow DNS resolution, connection timeouts, or routing issues. Disabling IPv6 forces all traffic through IPv4. Only turn this on if you have one of those problems. Most networks handle IPv6 fine. Turning it on or off reconnects WiFi for a few seconds."
          {...(status?.live?.ipv6_other_network && s?.ipv6_disabled && !errors.ipv6
            ? otherNetworkBadge
            : getBadge("ipv6", status, errors.ipv6 ?? null))}
          notice={
            notices.ipv6 ??
            (status?.external?.ipv6
              ? "IPv6 is already off on this connection, which is common for saved networks. Nothing to do unless something here needs IPv6. To turn IPv6 back on, turn this on, then off."
              : status?.live?.ipv6_other_network
                ? OTHER_NETWORK_TEXT
                : undefined)
          }
          checked={s?.ipv6_disabled ?? false}
          disabled={isBusy || updating || (!connected && !s?.ipv6_disabled)}
          working={working === "ipv6"}
          error={errors.ipv6}
          onChange={(val: boolean) =>
            handleToggle("ipv6", () => backend.setIpv6(val))
          }
        />
        <InfoRow
          label="Traffic shaping (CAKE)"
          subtitle="Fair queuing for traffic this device sends"
          explanation="Replaces the default queue for traffic this device sends with CAKE, which keeps flows fair and filters redundant TCP acknowledgments. It only affects outgoing traffic from this device. It cannot control other devices, your router, or your internet connection, so it does not fix lag that starts there. It does not limit your bandwidth. It is lost on reboot, and put back when WiFi connects if Auto-fix on wake is on."
          {...gatedBadge("cake", s?.cake_enabled)}
          checked={s?.cake_enabled ?? false}
          disabled={isBusy || updating || (!connected && !s?.cake_enabled)}
          error={errors.cake}
          notice={notices.cake}
          onChange={(val: boolean) =>
            handleToggle("cake", () => backend.setCake(val))
          }
        />
        {status?.live?.backend_tool_available && (
          <BackendToggleRow
            status={status}
            backendSwitch={backendSwitch}
            error={errors.wifi_backend}
            isBusy={isBusy || updating}
            onToggle={handleBackendToggle}
          />
        )}
      </PanelSection>

      <StreamingSection
        settings={s}
        live={status?.live}
        disabled={isBusy || updating}
        error={errors.streaming}
        notice={notices.streaming}
        onToggle={handleToggle}
      />

      {/* Live status */}
      <PanelSection title="Live status">
        {connected && status?.live?.ip_address && (
          <PanelSectionRow>
            <div style={{ fontSize: theme.fontSize.tiny, color: theme.text.tertiary }}>
              IP: {status.live.ip_address}
            </div>
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <StatsGrid live={status?.live ?? {}} connected={connected} />
        </PanelSectionRow>
      </PanelSection>

      <ActionsSection
        connected={connected}
        isBusy={isBusy || updating}
        onForceReapply={handleForceReapply}
        onReset={handleResetSettings}
        resetError={errors.reset}
        resetNotice={notices.reset}
        workingText={working === "reset" ? RESET_WORKING_TEXT : applyingAll ? BATCH_WORKING_TEXT : null}
      />

      <UpdatesSection
        channel={s?.update_channel ?? "stable"}
        updating={updating}
        updatingText={updatingText(updateProgress)}
        checkingUpdate={checkingUpdate}
        updateInfo={updateInfo}
        updateError={updateError}
        settingError={updateSettingError}
        onChannelChange={handleChannelChange}
        notify={s?.update_notify ?? true}
        onNotifyChange={handleNotifyChange}
        onApply={handleApplyUpdate}
        onCheck={handleCheckForUpdate}
        busy={isBusy}
      />

      <PanelFooter version={status?.version ?? "?"} />
    </DetailsContext.Provider>
  );
}

export default definePlugin(() => {
  let gameLifetimeUnsub: { unregister(): void } | null = null;
  // A launch just before the plugin is unloaded still had its delayed
  // reapply pending, which then called into a backend being torn down.
  let disposed = false;
  const timers = new Set<ReturnType<typeof setTimeout>>();

  const announce = setTimeout(() => {
    timers.delete(announce);
    if (!disposed) checkUpdateOnLoad();
  }, ANNOUNCE_DELAY_MS);
  timers.add(announce);
  const stopUpdateNotices = startUpdateNotices();

  try {
    const later = (ms: number, fn: () => void) => {
      const id = setTimeout(() => {
        timers.delete(id);
        if (!disposed) fn();
      }, ms);
      timers.add(id);
    };
    gameLifetimeUnsub = SteamClient.GameSessions.RegisterForAppLifetimeNotifications(
      (notification: { bRunning: boolean }) => {
        if (disposed) return;
        // Streaming auto mode looks now and again once the app has had time
        // to start or exit, instead of waiting for its next pass. A no-op
        // with auto mode off.
        backend.pokeDetection().catch(() => {});
        later(3000, () => backend.pokeDetection().catch(() => {}));
        if (notification.bRunning) {
          later(3000, () => backend.reapplyVolatile().catch(() => {}));
        }
      },
    );
  } catch (e) {
    console.error("WiFi Optimizer: failed to register game launch listener", e);
  }

  return {
    name: "WiFi Optimizer",
    titleView: <div className={staticClasses.Title}>WiFi Optimizer</div>,
    content: (
      <ErrorBoundary>
        <Content />
      </ErrorBoundary>
    ),
    icon: <FaWifi />,
    onDismount() {
      disposed = true;
      timers.forEach((id) => clearTimeout(id));
      timers.clear();
      gameLifetimeUnsub?.unregister();
      stopUpdateNotices();
      disposeUpdateWatch();
    },
  };
});
