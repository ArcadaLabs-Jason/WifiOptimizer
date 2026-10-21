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
import { InfoRow } from "./components/InfoRow";
import { StatsGrid } from "./components/StatsGrid";
import { Banner } from "./components/Banner";
import { BackendToggleRow } from "./components/BackendToggleRow";
import { UpdatesSection } from "./components/UpdatesSection";
import { PanelHeader } from "./components/PanelHeader";
import { PanelFooter } from "./components/PanelFooter";
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

const REFRESH_INTERVAL = 3000;
const RECONNECT_DELAY = 4000;
const BACKEND_POLL_INTERVAL = 750;
const UPDATE_CHECK_INTERVAL = 5 * 60 * 1000;
const UPDATE_CHECK_DEDUPE_MS = 60 * 1000;
// The update script allows the download up to five minutes on a slow link,
// so the panel waits a little longer than that before calling it failed.
// Longer than an update can take on a slow link: up to about 2 minutes for
// the check and the signature, 5 for the download, and the reload. A limit
// inside that was reached while the update was still going, and the next
// progress report took the "didn't complete" message straight back off.
const UPDATE_TIMEOUT_MS = 10 * 60 * 1000;
const BACKEND_SWITCH_MAX_S = 240;
// Shown on the lock and band toggles while the iwd backend is in use.
const IWD_PIN_NOTICE =
  "Paused while the WiFi backend is iwd, where NetworkManager can't reconnect a network pinned this way. Switch to wpa_supplicant to use it.";
const BACKEND_POLL_MAX_FAILURES = 5;
// What Reset undoes, said before it happens. It reaches every network the
// plugin touched, so one stray press of the button should not be enough.
const RESET_CONFIRM_TEXT =
  "This puts every setting back to its default and undoes what the plugin changed. Buffer tuning and CAKE go back to the values your system had, and the access point lock, the band preference, the IPv6 setting and custom DNS are removed from every network they were applied to.";
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
    case "restarting":
      return "Restarting Decky to finish. Steam will restart.";
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
// transmit power and which channels may be used, so this is a deliberate
// short list of ISO country codes rather than free text.
const REGION_OPTIONS = [
  { data: "00", label: "World (00) - most restrictive" },
  { data: "US", label: "United States (US)" },
  { data: "CA", label: "Canada (CA)" },
  { data: "GB", label: "United Kingdom (GB)" },
  { data: "IE", label: "Ireland (IE)" },
  { data: "DE", label: "Germany (DE)" },
  { data: "FR", label: "France (FR)" },
  { data: "ES", label: "Spain (ES)" },
  { data: "IT", label: "Italy (IT)" },
  { data: "NL", label: "Netherlands (NL)" },
  { data: "SE", label: "Sweden (SE)" },
  { data: "NO", label: "Norway (NO)" },
  { data: "DK", label: "Denmark (DK)" },
  { data: "FI", label: "Finland (FI)" },
  { data: "PL", label: "Poland (PL)" },
  { data: "PT", label: "Portugal (PT)" },
  { data: "AU", label: "Australia (AU)" },
  { data: "NZ", label: "New Zealand (NZ)" },
  { data: "JP", label: "Japan (JP)" },
  { data: "KR", label: "South Korea (KR)" },
  { data: "BR", label: "Brazil (BR)" },
  { data: "MX", label: "Mexico (MX)" },
  { data: "IN", label: "India (IN)" },
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
  const [isBusy, setIsBusy] = useState(false);
  const [applyingAll, setApplyingAll] = useState(false);
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
  const [updateError, setUpdateError] = useState<string | null>(null);
  const [updateProgress, setUpdateProgress] = useState<UpdateProgress | null>(null);

  // --- Backend switch state ---
  const [backendSwitch, setBackendSwitch] = useState<BackendSwitchStatus | null>(null);

  // --- Refs ---
  // busyRef is the synchronous re-entrancy guard; isBusy (above) is the React
  // state mirror so UI can respond. setBusy below writes both together.
  const busyRef = useRef(false);
  const backendPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevConnectedRef = useRef<boolean | null>(null);
  const lastUpdateCheckAtRef = useRef<number>(0);
  const statusSeqRef = useRef(0);

  const setBusy = useCallback((val: boolean) => {
    busyRef.current = val;
    setIsBusy(val);
  }, []);

  // Runs checkForUpdate with dedupe - skips if a check was issued within the
  // dedupe window. Lowers GitHub API pressure in CGNAT/dorm scenarios where
  // many devices share an IP. Manual button bypasses this (force=true).
  const runUpdateCheck = useCallback((force: boolean = false) => {
    const now = Date.now();
    if (!force && now - lastUpdateCheckAtRef.current < UPDATE_CHECK_DEDUPE_MS) {
      return;
    }
    lastUpdateCheckAtRef.current = now;
    backend.checkForUpdate().then(setUpdateInfo).catch(() => {});
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
      setStatus(s);
      if (s.settings) {
        // Only adopt a stored value that exists. Turning DNS off clears
        // dns_servers while leaving the provider on "custom", and copying
        // that empty string back over the field every few seconds made the
        // setting impossible to re-enable: the servers field is only
        // rendered while DNS is on, and turning it on with an empty custom
        // list is rejected. The backend also resets the provider when it
        // clears the servers, so the pair cannot get stuck together.
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
    }
  }, []);

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
    const giveUp = async (message: string) => {
      stopBackendPoll();
      setBackendSwitch(null);
      setErrors((prev) => ({ ...prev, wifi_backend: message }));
      await refreshStatus(true);
      setBusy(false);
    };
    backendPollRef.current = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const s = await backend.getBackendSwitchStatus();
        failures = 0;
        if (s.in_progress) {
          // A worker stuck on a wedged driver never reports back. Release
          // the panel rather than keep every control disabled forever.
          if (s.started_at && Date.now() / 1000 - s.started_at > BACKEND_SWITCH_MAX_S) {
            await giveUp("The backend switch is taking too long. Restart the Deck if WiFi does not come back.");
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
        stopBackendPoll();
        if (s.result && !s.result.success && s.result.message) {
          const detail = s.result.detail ? ` (${s.result.detail})` : "";
          setErrors((prev) => ({
            ...prev,
            wifi_backend: s.result!.message! + detail,
          }));
        }
        await refreshStatus(true);
        setBackendSwitch(s);
        setBusy(false);
      } catch (e) {
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
  }, [refreshStatus, setBusy]);

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
          setUpdateError("The update didn't finish. Try again, or check the plugin log.");
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

  // Safety timeout: if an update was initiated but the plugin hasn't been
  // reloaded within the time the script allows itself, it probably failed (network
  // drop, tarball corruption, etc.). Revert the UI so the user isn't stuck staring
  // at "Updating..." forever, and surface a message they can act on.
  useEffect(() => {
    if (!updating) return;
    const id = setTimeout(() => {
      console.error("WiFi Optimizer: update didn't complete in time");
      setUpdating(false);
      setUpdateError(
        "Update didn't complete. Try again, or reinstall manually from Konsole."
      );
    }, UPDATE_TIMEOUT_MS);
    return () => clearTimeout(id);
  }, [updating]);

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
      const result = await fn();
      outcome = result;
      // A setter can succeed and still have done something other than what
      // was asked - locking to the access point you were already on because
      // the stronger one would not take, say. Saying nothing there leaves the
      // user with a toggle that flipped and no idea why the outcome differs.
      if (result.success && result.message) {
        setNotices((prev) => ({ ...prev, [key]: result.message as string }));
      }
      if (!result.success) {
        const detail = result.detail ? ` (${result.detail})` : "";
        const msg = (result.message ?? ERROR_MESSAGES[result.error ?? ""] ?? "Unknown error") + detail;
        setErrors((prev) => ({ ...prev, [key]: msg }));
      }
      // Wait for WiFi to reconnect before refreshing status
      if (result.reconnected) {
        await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
      }
    } catch (e) {
      // A call that throws (Decky restarting, the backend gone) used to
      // surface as an unhandled rejection with the toggle silently unchanged.
      setErrors((prev) => ({
        ...prev,
        [key]: `Couldn't reach the plugin's backend (${String(e)})`,
      }));
    } finally {
      // Refresh before clearing busy so toggles/badges don't briefly flip to
      // stale pre-operation values in the interim render. Refresh runs even on
      // unexpected errors to keep UI consistent with backend state.
      await refreshStatus(true);
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
      const result = await fn();
      setOptimizeResult(result);
      if (result.results) {
        // Successes can carry a message too, as they do for a single toggle:
        // a lock that stayed on the access point you were already on, or a
        // fix kept for the next stream. Dropping those here hid the reason.
        const newErrors: Record<string, string> = {};
        const newNotices: Record<string, string> = {};
        for (const [k, v] of Object.entries(result.results)) {
          if (!v.success) {
            const det = v.detail ? ` (${v.detail})` : "";
            newErrors[k] = (v.message ?? ERROR_MESSAGES[v.error ?? ""] ?? "Failed") + det;
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
        message: "Something went wrong. Check the Decky log for details.",
      });
    } finally {
      // Refresh before clearing applyingAll so button doesn't briefly flip to
      // "All good" from stale status. Refresh runs on errors too.
      await refreshStatus(true);
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

  // Reset asks first: see RESET_CONFIRM_TEXT.
  const handleResetSettings = () => {
    if (busyRef.current) return;
    showModal(
      <ConfirmModal
        strTitle="Reset WiFi Optimizer?"
        strDescription={RESET_CONFIRM_TEXT}
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
    setOptimizeResult(null);
    setErrors({});
    setNotices({});
    resetDnsDraft();
    try {
      const result = await backend.resetSettings();
      if (!result.success) {
        setErrors({ reset: result.message ?? "Couldn't reset settings." });
      }
    } catch (e) {
      console.error("reset error", e);
      setErrors({ reset: "Couldn't reset settings." });
    } finally {
      await refreshStatus(true);
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

  const handleBackendToggle = async (on: boolean) => {
    // Re-entrancy guard: drop rapid clicks or clicks that land while another
    // operation is already running. Protects against duplicate backend calls
    // and the Force-Reapply/backend-switch overlap case.
    if (busyRef.current) return;
    const target = on ? "wpa_supplicant" : "iwd";
    setBusy(true);
    setErrors((prev) => {
      const next = { ...prev };
      delete next.wifi_backend;
      return next;
    });
    setOptimizeResult(null);

    try {
      const res = await backend.startBackendSwitch(target);
      if (!res.accepted) {
        setErrors((prev) => ({
          ...prev,
          wifi_backend: res.message ?? "Could not start backend switch",
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
      setBusy(false);
      setErrors((prev) => ({
        ...prev,
        wifi_backend: "Failed to start backend switch",
      }));
      console.error("startBackendSwitch error", e);
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
      // say why now instead of showing "Updating..." for a minute.
      if (!result.success) {
        setUpdating(false);
        setUpdateError(result.message ?? "Couldn't start the update.");
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
    try {
      const result = await backend.checkForUpdate();
      setUpdateInfo(result);
    } catch {
      // refreshStatus has its own catch; no need to log here
    }
    setCheckingUpdate(false);
  };

  const handleNotifyChange = async (enabled: boolean) => {
    try {
      const result = await backend.setUpdateNotify(enabled);
      if (!result.success) {
        setUpdateError(result.message ?? "Couldn't change the update notice setting.");
        return;
      }
      setUpdateError(null);
    } catch (e) {
      console.error("set update notify error", e);
      setUpdateError("Couldn't change the update notice setting.");
    }
    await refreshStatus(true);
  };

  const handleChannelChange = async (nextChannel: string) => {
    try {
      const result = await backend.setUpdateChannel(nextChannel);
      if (!result.success) {
        setUpdateError(result.message ?? "Couldn't change the update channel.");
        return;
      }
      setUpdateError(null);
      setUpdateInfo(null);
    } catch (e) {
      console.error("set channel error", e);
      setUpdateError("Couldn't change the update channel.");
      return;
    }
    await refreshStatus();
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
    const result = await handleToggle("dns", () =>
      backend.setDns(true, "custom", customDnsInput.trim())
    );
    if (result?.success) {
      dnsDirtyRef.current = false;
      setDnsCustomOpen(false);
    }
  };

  // Don't render content until first status arrives (prevents disconnect flash).
  // A status marked initializing is the backend saying a collection is still
  // running and it has nothing to report yet, which is the same situation.
  if (!status || status.initializing) {
    return <PanelSection title="WiFi Optimizer" />;
  }

  const s = status?.settings;
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

  // Streaming auto mode with no stream running: power save, buffer tuning
  // and CAKE are off on purpose. They read as paused, not as off or drifted.
  const streamingIdle = !!status?.live?.streaming_mode && !status?.live?.streaming_active;
  const gatedBadge = (key: string, on: boolean | undefined) =>
    streamingIdle && on && !errors[key]
      ? { badge: "paused" as const, text: "paused" }
      : getBadge(key, status, errors[key] ?? null);

  // Under iwd Optimize Safe leaves the lock out, so it is not one of the
  // things to wait for there, or "All good" never came.
  const lockSafe =
    status?.live?.wifi_backend === "iwd" ||
    (s?.bssid_lock_enabled &&
      !status?.drift?.bssid_lock &&
      // The lock can be enabled but belong to a different network, in which
      // case nothing has drifted and it is also doing nothing here. Without
      // this, the panel reports all clear while one of the four is inert.
      !status?.live?.bssid_lock_other_network);

  // Check if all safe optimizations are already active
  const allSafeActive =
    connected &&
    (status?.live?.power_save_off || (streamingIdle && s?.power_save_disabled)) &&
    !status?.drift?.power_save &&
    lockSafe &&
    status?.live?.dispatcher_installed &&
    (status?.live?.buffer_tuning_applied || (streamingIdle && s?.buffer_tuning_enabled)) &&
    !status?.drift?.buffer_tuning;

  return (
    <>
      <PanelHeader
        driver={s?.driver ?? ""}
        version={status?.version ?? "?"}
        lastApplied={s?.last_applied ?? 0}
        lastEnforced={status?.live?.last_enforced}
        streaming={status?.live}
        deviceLabel={s?.device_label !== "Unknown Device"
          ? `${s?.device_label} - ${s?.chip_label}`
          : s?.chip_label && s.chip_label !== "unknown"
            ? s.chip_label
            : undefined}
      />

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

      {/* Update available */}
      {updateInfo?.update_available && !updating && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleApplyUpdate}>
              {updateInfo.is_downgrade
                ? `Go back to v${updateInfo.latest_version}`
                : `Update to v${updateInfo.latest_version}`}
            </ButtonItem>
          </PanelSectionRow>
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

      {/* First-run prompt */}
      {connected && !s?.last_applied && (
        <Banner variant="info">
          Tap <strong>Optimize Safe</strong> to get started.
        </Banner>
      )}

      {/* Several saved copies of this network, disagreeing about security.
          The old wording here told people to forget the network and rejoin.
          That throws away their settings and gambles on which security the
          new copy gets, when the actual problem is only that the copies rank
          equally. Say what was done instead, and how to choose the other. */}
      {status?.external?.duplicate_profiles && (
        <Banner variant="warning" icon="⚠">
          This network is saved more than once with different security
          settings. Which copy is used decides which access points can be
          reached, so the connection can look inconsistent. The copy in use
          has been given priority, so the choice stays the same from now on.
          To use the other one instead, connect with it once and it takes
          priority.
        </Banner>
      )}

      {/* The router handed out an address another device already holds. WiFi
          reads as connected while nothing works, and without this the plugin
          is the obvious suspect. */}
      {status?.address_conflict && (
        <Banner variant="warning" icon="⚠">
          Another device on your network is using the same address as the Deck
          ({status.address_conflict.ip}), so the connection may not work. That's
          the router, not a WiFi setting. Restarting the router usually fixes it.
        </Banner>
      )}

      {/* The saved security is shutting the device out of a stronger access
          point on the same network. Without this the only symptom is the
          Deck preferring a distant access point for no visible reason. */}
      {status?.wpa2_only_stronger && (
        <Banner variant="info">
          A stronger access point on this network (signal{" "}
          {status.wpa2_only_stronger.signal} vs{" "}
          {status.wpa2_only_stronger.current_signal}) only offers WPA2, and this
          network is saved as WPA3-only, so the Deck can't use it. Access points
          that offer WPA3 keep using it. Only allow this if that access point
          is yours: anyone nearby can broadcast your network's name.
          {errors.allow_wpa2 && (
            <div style={{ color: theme.error.text }}>{errors.allow_wpa2}</div>
          )}
        </Banner>
      )}
      {/* A real button, so it can be reached with the D-pad. */}
      {status?.wpa2_only_stronger && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleAllowWpa2}>
              Allow WPA2 here
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* The lock is holding the Deck on an access point it has been carried
          away from, while a much stronger one of the same network is in
          range. Moving reconnects, so it is offered rather than done. The
          banner also stays up for a failed move's message, because a failed
          move clears the finding and would otherwise take the reason away
          with it. */}
      {(status?.lock_weaker || errors.move_bssid_lock) && (
        <Banner variant="warning" icon="⚠">
          {status?.lock_weaker && (
            <>
              The access point lock is holding you on a weaker access point
              (signal {status.lock_weaker.current_signal}). A stronger one is in
              range (signal {status.lock_weaker.signal}).
            </>
          )}
          {errors.move_bssid_lock && (
            <div style={{ color: theme.error.text }}>{errors.move_bssid_lock}</div>
          )}
        </Banner>
      )}
      {/* A real button, so it can be reached with the D-pad. */}
      {status?.lock_weaker && (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={isBusy || updating} onClick={handleMoveLock}>
              Move the lock
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* A setting we wrote was the likely reason the device could not
          connect, so it was released. Worth stating plainly: nothing the user
          did caused it, and the preference is not lost. */}
      {status?.external?.pin_released && (
        <Banner variant="warning" icon="⚠">
          WiFi could not connect for a while, so the locked access point or
          band preference was released to let it reconnect. The setting is
          still on and applies again to whichever access point works.
        </Banner>
      )}

      {/* Drift alert */}
      {connected && !!s?.last_applied && driftCount > 0 && (
        <Banner variant="warning" icon="⚠">
          {driftCount > 1
            ? `${driftCount} settings no longer match what you set.`
            : "1 setting no longer matches what you set."}
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
        <Banner variant="error" icon="⚠">
          {status?.message ?? "Couldn't read WiFi status."}
        </Banner>
      )}

      {/* Disconnected banner */}
      {status?.success !== false && !connected && (
        <Banner variant="error" icon="✕">
          Not connected to WiFi. Connect first, then optimize.
        </Banner>
      )}

      {/* Backend switch result banner */}
      {backendSwitch && !backendSwitch.in_progress && backendSwitch.result && (() => {
        const r = backendSwitch.result;
        // Treat reconnect timeout as a warning even when the backend-level
        // switch succeeded - the system is switched but WiFi didn't come back.
        const isWarning = !r.success || r.needs_reboot || r.reconnect_timed_out;
        let text: string;
        if (r.needs_reboot) {
          text = `Backend switched to ${r.target}, but wlan0 didn't come back - reboot required`;
        } else if (!r.success) {
          text = r.message ?? "Backend switch failed";
        } else {
          const parts = [`Switched to ${r.backend}`];
          if (r.recovery_performed) parts.push("recreated wlan0 interface");
          if (r.reconnect_timed_out) parts.push("WiFi didn't reconnect");
          text = parts.join(" · ");
        }
        return (
          <Banner variant={isWarning ? "warning" : "success"} icon={isWarning ? "⚠" : "✓"}>
            {text}
          </Banner>
        );
      })()}

      {/* Optimize result banner */}
      {optimizeResult && (() => {
        // A failure has no counts at all, and undefined === undefined made
        // it read as "All applied".
        if (optimizeResult.success === false) {
          return (
            <Banner variant="error" icon="⚠">
              {optimizeResult.message ?? "Something went wrong."}
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
        return (
          <Banner variant={all ? "success" : "warning"}>
            {all
              ? "All applied"
              : `${optimizeResult.applied}/${optimizeResult.total} applied`}
          </Banner>
        );
      })()}

      {/* Optimize button */}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={!connected || applyingAll || isBusy}
            onClick={handleOptimize}
          >
            {applyingAll
              ? "Applying..."
              : allSafeActive
                ? "All good"
                : "Optimize Safe"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* Power & stability */}
      <PanelSection title="Power & stability">
        <InfoRow
          label="Prevent lag spikes"
          subtitle="Disables WiFi power save and PCIe power states"
          explanation="The OS enables WiFi power saving at multiple levels - the wireless chip, the PCIe bus connecting it to the CPU, and driver-level low power modes. These cause latency spikes, packet batching, and throughput degradation during sustained streaming. This toggle disables all of them, keeping the WiFi hardware fully awake. Battery impact is minimal."
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
          subtitle="Locks to current AP - disable to switch networks or roam"
          explanation="Your device scans for other WiFi networks every few minutes even while connected. Each scan causes a brief interruption that can drop packets and stutter game streaming. Locking to your current access point stops these scans entirely. You'll need to disable this before switching to a different network or access point."
          {...(status?.live?.bssid_lock_other_network && !errors.bssid_lock
            ? { badge: "unknown" as const, text: "other network" }
            : getBadge("bssid_lock", status, errors.bssid_lock ?? null))}
          checked={s?.bssid_lock_enabled ?? false}
          disabled={isBusy || updating || (!connected && !s?.bssid_lock_enabled)}
          error={errors.bssid_lock}
          notice={
            notices.bssid_lock ??
            (status?.live?.pins_inactive_iwd && s?.bssid_lock_enabled
              ? IWD_PIN_NOTICE
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
          subtitle="Reapplies settings after sleep (NM dispatcher)"
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
          explanation="Increases kernel network buffer sizes and transmit queue length to handle the bursty UDP traffic that game streaming produces. Without this, packets can be dropped during high-bitrate moments, causing frame drops or brief quality dips. These settings benefit all network interfaces, including ethernet. They reset on every reboot."
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
              rgOptions={REGION_OPTIONS}
              selectedOption={regdomain.setting || regdomain.governing || "00"}
              disabled={isBusy || updating}
              onChange={(option: { data: string }) => {
                void handleToggle("regdomain", () =>
                  backend.setRegdomain(option.data)
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
              is normal on the Steam Deck and does not mean a band is missing.
            </div>
          </PanelSectionRow>
        )}
        <InfoRow
          label="Force 5 GHz band"
          subtitle="Avoid 2.4 GHz Bluetooth interference"
          explanation={`Bluetooth operates on the 2.4 GHz band${
            isDeckLcd ? ", and on the Steam Deck LCD the antennas are shared" : ""
          }. Using 5 GHz for WiFi avoids this interference entirely, giving you a cleaner, faster connection. Only enable this if your router supports 5 GHz. If no 5 GHz access point for this network is in range when you turn it on, it is refused instead of disconnecting you.${
            supports6GHz
              ? " This keeps the device on 5 GHz specifically, so leave it off if you connect over 6 GHz."
              : ""
          }`}
          {...getBadge(on6GHz ? undefined : "band_preference", status, band5Error ?? null)}
          checked={(s?.band_preference_enabled ?? false) && !on6GHz}
          disabled={isBusy || updating || (!connected && !(s?.band_preference_enabled && !on6GHz))}
          error={band5Error}
          notice={
            notices.band_5ghz ??
            (on6GHz
              ? undefined
              : notices.band_preference ??
                (status?.live?.pins_inactive_iwd && s?.band_preference_enabled
                  ? IWD_PIN_NOTICE
                  : status?.external?.band
                  ? "This network is already limited to one band by a setting outside this plugin. Turn this on and off to clear it."
                  : undefined))
          }
          onChange={(val: boolean) => {
            clearBandMessages();
            handleToggle("band_5ghz", () =>
              // Never "6GHz": turning that off leaves it recorded as the band.
              backend.setBandPreference(val, s?.band_preference === "bg" ? "bg" : "a")
            );
          }}
        />
        {(status?.live?.six_ghz_offered || on6GHz) && (
          <InfoRow
            label="Force 6 GHz band"
            subtitle="Stay on 6 GHz where your router has it"
            explanation="6 GHz is the least crowded band, with the most room for wide channels, but it reaches less far than 5 GHz and only routers with WiFi 6E or newer offer it. This keeps the device on 6 GHz for this network. It needs WPA3. If this network can't use it, turning it on says why. If the device can't connect on 6 GHz when you turn this on, the band is put back the way it was. Like the 5 GHz setting, if you later take the device out of range it is released after about a minute so WiFi can reconnect."
            {...getBadge(on6GHz ? "band_preference" : undefined, status, band6Error ?? null)}
            checked={on6GHz}
            disabled={isBusy || updating || (!connected && !on6GHz)}
            error={band6Error}
            notice={
              notices.band_6ghz ??
              (on6GHz
                ? notices.band_preference ??
                  (status?.live?.pins_inactive_iwd ? IWD_PIN_NOTICE : undefined)
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
          explanation="Your internet provider's DNS servers translate domain names into IP addresses. They can be slow or unreliable. Switching to a public DNS like Cloudflare (1.1.1.1) or Google (8.8.8.8) can speed up initial connections and improve reliability. This only affects the current WiFi network."
          {...getBadge("dns", status, errors.dns ?? null)}
          checked={s?.dns_enabled ?? false}
          disabled={isBusy || updating || (!connected && !s?.dns_enabled)}
          error={errors.dns}
          notice={notices.dns}
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
                  label="DNS Provider"
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
          explanation="Some networks have poor or misconfigured IPv6 support, which can cause slow DNS resolution, connection timeouts, or routing issues. Disabling IPv6 forces all traffic through IPv4. Only enable this if you're experiencing issues - most modern networks handle IPv6 fine."
          {...getBadge("ipv6", status, errors.ipv6 ?? null)}
          notice={
            notices.ipv6 ??
            (status?.external?.ipv6
              ? "IPv6 is already off on this connection. SteamOS often saves networks that way."
              : status?.live?.ipv6_other_network
                ? "On for the network it was turned on for, not this one."
                : undefined)
          }
          checked={s?.ipv6_disabled ?? false}
          disabled={isBusy || updating || (!connected && !s?.ipv6_disabled)}
          error={errors.ipv6}
          onChange={(val: boolean) =>
            handleToggle("ipv6", () => backend.setIpv6(val))
          }
        />
        <InfoRow
          label="Traffic shaping (CAKE)"
          subtitle="Fair queuing and bufferbloat prevention"
          explanation="Replaces the default queue for traffic this device sends with CAKE, which keeps flows fair and filters redundant TCP acknowledgments. It only affects outgoing traffic from this device. It cannot control other devices, your router, or your internet connection. Does not limit your bandwidth. Resets on reboot and is reapplied automatically if auto-fix on wake is enabled."
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
      />

      <UpdatesSection
        channel={s?.update_channel ?? "stable"}
        updating={updating}
        updatingText={updatingText(updateProgress)}
        checkingUpdate={checkingUpdate}
        updateInfo={updateInfo}
        updateError={updateError}
        onChannelChange={handleChannelChange}
        notify={s?.update_notify ?? true}
        onNotifyChange={handleNotifyChange}
        onApply={handleApplyUpdate}
        onCheck={handleCheckForUpdate}
        busy={isBusy}
      />

      <PanelFooter version={status?.version ?? "?"} />
    </>
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
