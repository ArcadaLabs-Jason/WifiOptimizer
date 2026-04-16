const manifest = {"name":"WiFi Optimizer"};
const API_VERSION = 2;
const internalAPIConnection = window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;
if (!internalAPIConnection) {
    throw new Error('[@decky/api]: Failed to connect to the loader as as the loader API was not initialized. This is likely a bug in Decky Loader.');
}
let api;
try {
    api = internalAPIConnection.connect(API_VERSION, manifest.name);
}
catch {
    api = internalAPIConnection.connect(1, manifest.name);
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version 1. Some features may not work.`);
}
if (api._version != API_VERSION) {
    console.warn(`[@decky/api] Requested API version ${API_VERSION} but the running loader only supports version ${api._version}. Some features may not work.`);
}
const callable = api.callable;
const definePlugin = (fn) => {
    return (...args) => {
        return fn(...args);
    };
};

var DefaultContext = {
  color: undefined,
  size: undefined,
  className: undefined,
  style: undefined,
  attr: undefined
};
var IconContext = SP_REACT.createContext && /*#__PURE__*/SP_REACT.createContext(DefaultContext);

var _excluded = ["attr", "size", "title"];
function _objectWithoutProperties(e, t) { if (null == e) return {}; var o, r, i = _objectWithoutPropertiesLoose(e, t); if (Object.getOwnPropertySymbols) { var n = Object.getOwnPropertySymbols(e); for (r = 0; r < n.length; r++) o = n[r], -1 === t.indexOf(o) && {}.propertyIsEnumerable.call(e, o) && (i[o] = e[o]); } return i; }
function _objectWithoutPropertiesLoose(r, e) { if (null == r) return {}; var t = {}; for (var n in r) if ({}.hasOwnProperty.call(r, n)) { if (-1 !== e.indexOf(n)) continue; t[n] = r[n]; } return t; }
function _extends() { return _extends = Object.assign ? Object.assign.bind() : function (n) { for (var e = 1; e < arguments.length; e++) { var t = arguments[e]; for (var r in t) ({}).hasOwnProperty.call(t, r) && (n[r] = t[r]); } return n; }, _extends.apply(null, arguments); }
function ownKeys(e, r) { var t = Object.keys(e); if (Object.getOwnPropertySymbols) { var o = Object.getOwnPropertySymbols(e); r && (o = o.filter(function (r) { return Object.getOwnPropertyDescriptor(e, r).enumerable; })), t.push.apply(t, o); } return t; }
function _objectSpread(e) { for (var r = 1; r < arguments.length; r++) { var t = null != arguments[r] ? arguments[r] : {}; r % 2 ? ownKeys(Object(t), true).forEach(function (r) { _defineProperty(e, r, t[r]); }) : Object.getOwnPropertyDescriptors ? Object.defineProperties(e, Object.getOwnPropertyDescriptors(t)) : ownKeys(Object(t)).forEach(function (r) { Object.defineProperty(e, r, Object.getOwnPropertyDescriptor(t, r)); }); } return e; }
function _defineProperty(e, r, t) { return (r = _toPropertyKey(r)) in e ? Object.defineProperty(e, r, { value: t, enumerable: true, configurable: true, writable: true }) : e[r] = t, e; }
function _toPropertyKey(t) { var i = _toPrimitive(t, "string"); return "symbol" == typeof i ? i : i + ""; }
function _toPrimitive(t, r) { if ("object" != typeof t || !t) return t; var e = t[Symbol.toPrimitive]; if (void 0 !== e) { var i = e.call(t, r); if ("object" != typeof i) return i; throw new TypeError("@@toPrimitive must return a primitive value."); } return ("string" === r ? String : Number)(t); }
function Tree2Element(tree) {
  return tree && tree.map((node, i) => /*#__PURE__*/SP_REACT.createElement(node.tag, _objectSpread({
    key: i
  }, node.attr), Tree2Element(node.child)));
}
function GenIcon(data) {
  return props => /*#__PURE__*/SP_REACT.createElement(IconBase, _extends({
    attr: _objectSpread({}, data.attr)
  }, props), Tree2Element(data.child));
}
function IconBase(props) {
  var elem = conf => {
    var {
        attr,
        size,
        title
      } = props,
      svgProps = _objectWithoutProperties(props, _excluded);
    var computedSize = size || conf.size || "1em";
    var className;
    if (conf.className) className = conf.className;
    if (props.className) className = (className ? className + " " : "") + props.className;
    return /*#__PURE__*/SP_REACT.createElement("svg", _extends({
      stroke: "currentColor",
      fill: "currentColor",
      strokeWidth: "0"
    }, conf.attr, attr, svgProps, {
      className: className,
      style: _objectSpread(_objectSpread({
        color: props.color || conf.color
      }, conf.style), props.style),
      height: computedSize,
      width: computedSize,
      xmlns: "http://www.w3.org/2000/svg"
    }), title && /*#__PURE__*/SP_REACT.createElement("title", null, title), props.children);
  };
  return IconContext !== undefined ? /*#__PURE__*/SP_REACT.createElement(IconContext.Consumer, null, conf => elem(conf)) : elem(DefaultContext);
}

// THIS FILE IS AUTO GENERATED
function FaWifi (props) {
  return GenIcon({"attr":{"viewBox":"0 0 640 512"},"child":[{"tag":"path","attr":{"d":"M634.91 154.88C457.74-8.99 182.19-8.93 5.09 154.88c-6.66 6.16-6.79 16.59-.35 22.98l34.24 33.97c6.14 6.1 16.02 6.23 22.4.38 145.92-133.68 371.3-133.71 517.25 0 6.38 5.85 16.26 5.71 22.4-.38l34.24-33.97c6.43-6.39 6.3-16.82-.36-22.98zM320 352c-35.35 0-64 28.65-64 64s28.65 64 64 64 64-28.65 64-64-28.65-64-64-64zm202.67-83.59c-115.26-101.93-290.21-101.82-405.34 0-6.9 6.1-7.12 16.69-.57 23.15l34.44 33.99c6 5.92 15.66 6.32 22.05.8 83.95-72.57 209.74-72.41 293.49 0 6.39 5.52 16.05 5.13 22.05-.8l34.44-33.99c6.56-6.46 6.33-17.06-.56-23.15z"},"child":[]}]})(props);
}

const getStatus = callable("get_status");
const setPowerSave = callable("set_power_save");
const setAutoFix = callable("set_auto_fix");
const setBssidLock = callable("set_bssid_lock");
const setBandPreference = callable("set_band_preference");
const setDns = callable("set_dns");
const setIpv6 = callable("set_ipv6");
const setBufferTuning = callable("set_buffer_tuning");
const optimizeSafe = callable("optimize_safe");
const reapplyAll = callable("reapply_all");
const resetSettings = callable("reset_settings");
const setUpdateChannel = callable("set_update_channel");
const checkForUpdate = callable("check_for_update");
const applyUpdate = callable("apply_update");
const startBackendSwitch = callable("start_backend_switch");
const getBackendSwitchStatus = callable("get_backend_switch_status");

const ERROR_MESSAGES = {
    no_wifi: "Not connected to WiFi. Connect first, then optimize.",
    iw_failed: "Couldn't change WiFi setting. Try toggling WiFi off/on.",
    nmcli_failed: "Couldn't update connection. Forget and reconnect to this network.",
    timeout: "Command timed out. The system may be busy. Try again.",
    write_failed: "Couldn't install auto-fix script. The filesystem may be locked.",
    parse_error: "Settings were reset to defaults.",
    unexpected: "Something went wrong. Check the Decky log for details.",
};

const BADGE_STYLES = {
    active: { background: "rgba(29,158,117,0.15)", color: "#3fc56e" },
    locked: { background: "rgba(29,158,117,0.15)", color: "#3fc56e" },
    set: { background: "rgba(29,158,117,0.15)", color: "#3fc56e" },
    drifted: { background: "rgba(223,138,0,0.15)", color: "#ffc669" },
    off: { background: "rgba(255,255,255,0.06)", color: "#6a6a7a" },
    error: { background: "rgba(211,36,43,0.15)", color: "#ff878c" },
    unknown: { background: "rgba(255,255,255,0.06)", color: "#8a8a9a" },
};
function StatusBadge({ badge, text }) {
    const style = BADGE_STYLES[badge];
    return (SP_JSX.jsx("span", { style: {
            fontSize: "11px",
            padding: "2px 6px",
            borderRadius: "4px",
            whiteSpace: "nowrap",
            background: style.background,
            color: style.color,
        }, children: text }));
}

function InfoRow({ label, subtitle, explanation, badge, text, checked, disabled = false, error, onChange, children, }) {
    const showBadge = badge !== undefined && text !== undefined;
    const [expanded, setExpanded] = SP_REACT.useState(false);
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: SP_JSX.jsxs("span", { style: { display: "flex", alignItems: "center", gap: "4px" }, children: [SP_JSX.jsx("span", { onClick: (e) => {
                                    e.stopPropagation();
                                    setExpanded(!expanded);
                                }, style: {
                                    display: "inline-flex",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    width: "16px",
                                    height: "16px",
                                    borderRadius: "50%",
                                    background: expanded
                                        ? "rgba(55,138,221,0.2)"
                                        : "rgba(255,255,255,0.08)",
                                    color: expanded ? "#60baff" : "#8a8a9a",
                                    fontSize: "10px",
                                    fontWeight: 700,
                                    cursor: "pointer",
                                    flexShrink: 0,
                                }, children: "i" }), SP_JSX.jsx("span", { children: label })] }), description: SP_JSX.jsxs("span", { style: { display: "block" }, children: [showBadge && (SP_JSX.jsx("span", { style: {
                                    display: "flex",
                                    justifyContent: "flex-end",
                                    marginBottom: "4px",
                                }, children: SP_JSX.jsx(StatusBadge, { badge: badge, text: text }) })), error ? (SP_JSX.jsx("span", { style: { color: "#ff878c" }, children: error })) : (SP_JSX.jsx("span", { style: { color: "#7a7a8a", fontSize: "11px" }, children: subtitle }))] }), checked: checked, disabled: disabled, onChange: onChange }) }), expanded && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                        padding: "8px 12px",
                        background: "rgba(255,255,255,0.02)",
                        borderRadius: "6px",
                        fontSize: "11px",
                        lineHeight: "1.5",
                        color: "#9a9aaa",
                    }, children: explanation }) })), children] }));
}

function signalColor(dbm) {
    if (!dbm)
        return "#6a6a7a";
    const val = parseInt(dbm);
    if (isNaN(val))
        return "#6a6a7a";
    if (val > -50)
        return "#3fc56e";
    if (val > -70)
        return "#e0e0e0";
    if (val > -80)
        return "#ffc669";
    return "#ff878c";
}
function bandLabel(freqStr) {
    if (!freqStr)
        return "--";
    const mhz = parseInt(freqStr);
    if (isNaN(mhz))
        return freqStr;
    if (mhz < 3000)
        return "2.4 GHz";
    if (mhz < 5925)
        return "5 GHz";
    return "6 GHz";
}
function bandColor(freqStr) {
    if (!freqStr)
        return "#6a6a7a";
    const mhz = parseInt(freqStr);
    if (isNaN(mhz))
        return "#e0e0e0";
    if (mhz < 3000)
        return "#ffc669"; // yellow - 2.4 GHz (suboptimal)
    return "#3fc56e"; // green - 5/6 GHz (good)
}
function StatsGrid({ live, connected }) {
    const na = "--";
    const signal = connected ? live.signal_dbm ?? na : na;
    const speed = connected ? live.tx_bitrate ?? na : na;
    const band = connected ? bandLabel(live.frequency) : na;
    const freq = connected ? live.frequency ?? "" : "";
    const channel = connected ? live.channel ?? na : na;
    const cell = {
        background: "rgba(255,255,255,0.04)",
        borderRadius: "4px",
        padding: "4px 8px",
    };
    const lbl = {
        fontSize: "9px",
        color: "#6a6a7a",
        textTransform: "uppercase",
        letterSpacing: "0.5px",
    };
    const val = (color) => ({
        fontSize: "13px",
        fontWeight: 500,
        color: connected ? color : "#6a6a7a",
    });
    return (SP_JSX.jsxs(DFL.Focusable, { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px" }, children: [SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Signal" }), SP_JSX.jsx("div", { style: val(signalColor(live.signal_dbm)), children: signal })] }), SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Speed" }), SP_JSX.jsx("div", { style: val("#e0e0e0"), children: speed })] }), SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Band" }), SP_JSX.jsx("div", { style: val(bandColor(live.frequency)), children: band }), freq && SP_JSX.jsxs("div", { style: { fontSize: "9px", color: "#6a6a7a" }, children: [freq, " MHz"] })] }), SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Channel" }), SP_JSX.jsx("div", { style: val("#e0e0e0"), children: channel })] })] }));
}

const REFRESH_INTERVAL = 3000;
const RECONNECT_DELAY = 4000;
const BACKEND_POLL_INTERVAL = 750;
const UPDATE_CHECK_INTERVAL = 5 * 60 * 1000;
const UPDATE_CHECK_DEDUPE_MS = 60 * 1000;
const BACKEND_PHASE_TEXT = {
    idle: "",
    switching: "Switching backend…",
    recovering_interface: "Recreating wlan0 interface…",
    reconnecting: "Reconnecting…",
    done: "",
    failed: "",
};
function timeAgo(ts) {
    if (!ts)
        return "never";
    const diff = Math.floor(Date.now() / 1000) - ts;
    if (diff < 60)
        return "just now";
    if (diff < 3600)
        return `${Math.floor(diff / 60)} min ago`;
    if (diff < 86400)
        return `${Math.floor(diff / 3600)} hr ago`;
    return `${Math.floor(diff / 86400)}d ago`;
}
function getBadge(_enabled, driftKey, status, errorKey, _activeText = "active") {
    // Only surface a badge when it tells the user something the toggle position
    // doesn't — failure or drift between our setting and the system state.
    // Binary on/off badges are redundant with the toggle itself and are hidden.
    if (errorKey)
        return { badge: "error", text: "failed" };
    if (driftKey && status?.drift?.[driftKey])
        return { badge: "drifted", text: "drifted" };
    return null;
}
const DNS_OPTIONS = [
    { data: "cloudflare", label: "Cloudflare (1.1.1.1)" },
    { data: "google", label: "Google (8.8.8.8)" },
    { data: "quad9", label: "Quad9 (9.9.9.9)" },
    { data: "custom", label: "Custom" },
];
function Content() {
    const [status, setStatus] = SP_REACT.useState(null);
    const [errors, setErrors] = SP_REACT.useState({});
    const [applyingAll, setApplyingAll] = SP_REACT.useState(false);
    const [optimizeResult, setOptimizeResult] = SP_REACT.useState(null);
    const [customDnsInput, setCustomDnsInput] = SP_REACT.useState("");
    const [updateInfo, setUpdateInfo] = SP_REACT.useState(null);
    const [checkingUpdate, setCheckingUpdate] = SP_REACT.useState(false);
    const [updating, setUpdating] = SP_REACT.useState(false);
    const [backendSwitch, setBackendSwitch] = SP_REACT.useState(null);
    const intervalRef = SP_REACT.useRef(null);
    const backendPollRef = SP_REACT.useRef(null);
    const busyRef = SP_REACT.useRef(false);
    const prevConnectedRef = SP_REACT.useRef(null);
    const lastUpdateCheckAtRef = SP_REACT.useRef(0);
    // Runs checkForUpdate with dedupe — skips if a check was issued within the
    // dedupe window. Lowers GitHub API pressure in CGNAT/dorm scenarios where
    // many Decks share an IP. Manual button bypasses this (force=true).
    const runUpdateCheck = SP_REACT.useCallback((force = false) => {
        const now = Date.now();
        if (!force && now - lastUpdateCheckAtRef.current < UPDATE_CHECK_DEDUPE_MS) {
            return;
        }
        lastUpdateCheckAtRef.current = now;
        checkForUpdate().then(setUpdateInfo).catch(() => { });
    }, []);
    const refreshStatus = SP_REACT.useCallback(async () => {
        if (busyRef.current)
            return;
        try {
            const s = await getStatus();
            setStatus(s);
            if (s.settings) {
                if (s.settings.dns_provider === "custom") {
                    setCustomDnsInput(s.settings.dns_servers || "");
                }
            }
        }
        catch (e) {
            console.error("WiFi Optimizer: failed to get status", e);
        }
    }, []);
    const stopBackendPoll = () => {
        if (backendPollRef.current) {
            clearInterval(backendPollRef.current);
            backendPollRef.current = null;
        }
    };
    const beginBackendPoll = SP_REACT.useCallback(() => {
        stopBackendPoll();
        backendPollRef.current = setInterval(async () => {
            try {
                const s = await getBackendSwitchStatus();
                if (s.in_progress) {
                    // Mid-switch: update so the subtitle can show the current phase.
                    setBackendSwitch(s);
                    return;
                }
                // Terminal. Stop polling, then refresh status BEFORE clearing the
                // in_progress flag — this keeps the optimistic toggle position steady
                // until live.wifi_backend has caught up, avoiding a toggle/untoggle
                // flicker at the end of the switch.
                stopBackendPoll();
                busyRef.current = false;
                if (s.result && !s.result.success && s.result.message) {
                    const detail = s.result.detail ? ` (${s.result.detail})` : "";
                    setErrors((prev) => ({
                        ...prev,
                        wifi_backend: s.result.message + detail,
                    }));
                }
                await refreshStatus();
                setBackendSwitch(s);
            }
            catch (e) {
                stopBackendPoll();
                busyRef.current = false;
                console.error("backend switch poll error", e);
            }
        }, BACKEND_POLL_INTERVAL);
    }, [refreshStatus]);
    SP_REACT.useEffect(() => {
        refreshStatus();
        intervalRef.current = setInterval(refreshStatus, REFRESH_INTERVAL);
        // Initial update check. If it fails (e.g., no network yet), the effect
        // below retries on connectivity recovery. QAM tends to cache the panel
        // across close/open, so we can't rely on remount to retry.
        runUpdateCheck();
        // Resume backend-switch polling if one is in flight (panel was reopened mid-switch)
        getBackendSwitchStatus()
            .then((s) => {
            if (s.in_progress) {
                setBackendSwitch(s);
                busyRef.current = true;
                beginBackendPoll();
            }
        })
            .catch(() => { });
        return () => {
            if (intervalRef.current)
                clearInterval(intervalRef.current);
            if (backendPollRef.current)
                clearInterval(backendPollRef.current);
        };
    }, [refreshStatus, beginBackendPoll, runUpdateCheck]);
    // Retry update check when connectivity recovers — the initial one-shot check
    // in the mount effect misses the case where the panel was already open when
    // the network came back. Skip until status has loaded to avoid a spurious
    // null→true "transition" firing an extra check on every mount.
    SP_REACT.useEffect(() => {
        if (!status)
            return;
        const connected = status.connected;
        const prev = prevConnectedRef.current;
        prevConnectedRef.current = connected;
        if (prev === false && connected === true) {
            runUpdateCheck();
        }
    }, [status?.connected, runUpdateCheck]);
    // Periodic update re-check — QAM often caches the panel across close/reopen,
    // so the mount-effect check doesn't re-fire. This heartbeat catches new
    // releases when the panel has been left open for a while.
    SP_REACT.useEffect(() => {
        const id = setInterval(() => runUpdateCheck(), UPDATE_CHECK_INTERVAL);
        return () => clearInterval(id);
    }, [runUpdateCheck]);
    const handleBackendToggle = async (on) => {
        const target = on ? "wpa_supplicant" : "iwd";
        busyRef.current = true;
        setErrors((prev) => {
            const next = { ...prev };
            delete next.wifi_backend;
            return next;
        });
        setOptimizeResult(null);
        try {
            const res = await startBackendSwitch(target);
            if (!res.accepted) {
                setErrors((prev) => ({
                    ...prev,
                    wifi_backend: res.message ?? "Could not start backend switch",
                }));
                busyRef.current = false;
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
        }
        catch (e) {
            busyRef.current = false;
            setErrors((prev) => ({
                ...prev,
                wifi_backend: "Failed to start backend switch",
            }));
            console.error("startBackendSwitch error", e);
        }
    };
    const handleToggle = async (key, fn) => {
        busyRef.current = true;
        setErrors((prev) => {
            const next = { ...prev };
            delete next[key];
            return next;
        });
        setOptimizeResult(null);
        try {
            const result = await fn();
            if (!result.success) {
                const detail = result.detail ? ` (${result.detail})` : "";
                const msg = (result.message ?? ERROR_MESSAGES[result.error ?? ""] ?? "Unknown error") + detail;
                setErrors((prev) => ({ ...prev, [key]: msg }));
            }
            // Wait for WiFi to reconnect before refreshing status
            if (result.reconnected) {
                await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
            }
        }
        finally {
            busyRef.current = false;
        }
        await refreshStatus();
    };
    const handleOptimize = async () => {
        busyRef.current = true;
        setApplyingAll(true);
        setErrors({});
        setOptimizeResult(null);
        try {
            const result = await optimizeSafe();
            setOptimizeResult(result);
            if (result.results) {
                const newErrors = {};
                for (const [k, v] of Object.entries(result.results)) {
                    if (!v.success) {
                        const det = v.detail ? ` (${v.detail})` : "";
                        newErrors[k] = (v.message ?? ERROR_MESSAGES[v.error ?? ""] ?? "Failed") + det;
                    }
                }
                setErrors(newErrors);
            }
            // optimize_safe contains BSSID lock which reconnects
            if (result.reconnected) {
                await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
            }
        }
        catch (e) {
            console.error("optimize error", e);
        }
        finally {
            busyRef.current = false;
            setApplyingAll(false);
        }
        await refreshStatus();
    };
    // Don't render content until first status arrives (prevents disconnect flash)
    if (!status) {
        return SP_JSX.jsx(DFL.PanelSection, { title: "WiFi Optimizer" });
    }
    const s = status?.settings;
    const connected = status?.connected ?? false;
    const supported = status?.supported ?? true;
    const driftCount = status?.drift ? Object.keys(status.drift).length : 0;
    const isOled = s?.model === "oled";
    const modelLabel = s
        ? `${(s.model || "unknown").toUpperCase()} - ${s.driver || "?"}`
        : "";
    // Check if all safe optimizations are already active
    const allSafeActive = connected &&
        status?.live?.power_save_off &&
        !status?.drift?.power_save &&
        s?.bssid_lock_enabled &&
        !status?.drift?.bssid_lock &&
        status?.live?.dispatcher_installed &&
        status?.live?.buffer_tuning_applied &&
        !status?.drift?.buffer_tuning;
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("span", { style: {
                                fontSize: "10px",
                                background: "rgba(255,255,255,0.06)",
                                padding: "2px 8px",
                                borderRadius: "10px",
                                color: "#8a8a9a",
                            }, children: ["Device: ", modelLabel] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: "10px", color: "#6a6a7a" }, children: ["Version: ", status?.version ?? "?"] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: "10px", color: "#6a6a7a" }, children: "Tap (i) on any toggle for details" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: "10px", color: "#6a6a7a" }, children: ["Last changed: ", timeAgo(s?.last_applied ?? 0), status?.live?.last_enforced ? (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx("br", {}), "Auto-applied: ", timeAgo(status.live.last_enforced)] })) : ""] }) })] }), !supported && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                            padding: "8px 12px",
                            background: "rgba(211,36,43,0.08)",
                            border: "0.5px solid rgba(211,36,43,0.2)",
                            borderRadius: "8px",
                            fontSize: "12px",
                            color: "#ff878c",
                            width: "100%",
                            boxSizing: "border-box",
                        }, children: SP_JSX.jsx("span", { children: "This plugin is designed for Steam Deck only. Unsupported device detected." }) }) }) })), updateInfo?.update_available && !updating && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs(DFL.ButtonItem, { layout: "below", onClick: async () => {
                            setUpdating(true);
                            try {
                                await applyUpdate();
                            }
                            catch { /* restart killed connection */ }
                        }, children: ["Update to v", updateInfo.latest_version] }) }) })), updating && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: "12px", color: "#60baff" }, children: "Updating... plugin will restart momentarily." }) }) })), connected && !s?.last_applied && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                            padding: "8px 12px",
                            background: "rgba(55,138,221,0.08)",
                            border: "0.5px solid rgba(55,138,221,0.2)",
                            borderRadius: "8px",
                            fontSize: "12px",
                            color: "#60baff",
                            width: "100%",
                            boxSizing: "border-box",
                        }, children: SP_JSX.jsxs("span", { children: ["Tap ", SP_JSX.jsx("strong", { children: "Optimize Safe" }), " to get started."] }) }) }) })), connected && !!s?.last_applied && driftCount > 0 && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: {
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                            padding: "8px 12px",
                            background: "rgba(223,138,0,0.08)",
                            border: "0.5px solid rgba(223,138,0,0.2)",
                            borderRadius: "8px",
                            fontSize: "12px",
                            color: "#ffc669",
                            width: "100%",
                            boxSizing: "border-box",
                        }, children: [SP_JSX.jsx("span", { style: { fontSize: "14px" }, children: "\u26A0" }), SP_JSX.jsxs("span", { children: [driftCount, " setting", driftCount > 1 ? "s" : "", " drifted after wake.", " ", SP_JSX.jsx("span", { style: { textDecoration: "underline", cursor: "pointer" }, onClick: handleOptimize, children: "Fix now" })] })] }) }) })), !connected && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: {
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                            padding: "8px 12px",
                            background: "rgba(211,36,43,0.08)",
                            border: "0.5px solid rgba(211,36,43,0.2)",
                            borderRadius: "8px",
                            fontSize: "12px",
                            color: "#ff878c",
                            width: "100%",
                            boxSizing: "border-box",
                        }, children: [SP_JSX.jsx("span", { style: { fontSize: "14px" }, children: "\u2715" }), SP_JSX.jsx("span", { children: "Not connected to WiFi. Connect first, then optimize." })] }) }) })), backendSwitch && !backendSwitch.in_progress && backendSwitch.result && (() => {
                const r = backendSwitch.result;
                // Treat reconnect timeout as a warning even when the backend-level
                // switch succeeded — the system is switched but WiFi didn't come back.
                const isWarning = !r.success || r.needs_reboot || r.reconnect_timed_out;
                let text;
                if (r.needs_reboot) {
                    text = `Backend switched to ${r.target}, but wlan0 didn't come back — reboot required`;
                }
                else if (!r.success) {
                    text = r.message ?? "Backend switch failed";
                }
                else {
                    const parts = [`Switched to ${r.backend}`];
                    if (r.recovery_performed)
                        parts.push("recreated wlan0 interface");
                    if (r.reconnect_timed_out)
                        parts.push("WiFi didn't reconnect");
                    text = parts.join(" · ");
                }
                return (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: {
                                display: "flex",
                                alignItems: "center",
                                gap: "6px",
                                padding: "8px 12px",
                                background: isWarning
                                    ? "rgba(223,138,0,0.08)"
                                    : "rgba(29,158,117,0.08)",
                                border: `0.5px solid ${isWarning
                                    ? "rgba(223,138,0,0.2)"
                                    : "rgba(29,158,117,0.2)"}`,
                                borderRadius: "8px",
                                fontSize: "12px",
                                color: isWarning ? "#ffc669" : "#3fc56e",
                                width: "100%",
                                boxSizing: "border-box",
                            }, children: [SP_JSX.jsx("span", { style: { fontSize: "14px" }, children: isWarning ? "⚠" : "✓" }), SP_JSX.jsx("span", { children: text })] }) }) }));
            })(), optimizeResult && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                            display: "flex",
                            alignItems: "center",
                            gap: "6px",
                            padding: "8px 12px",
                            background: optimizeResult.applied === optimizeResult.total
                                ? "rgba(29,158,117,0.08)"
                                : "rgba(223,138,0,0.08)",
                            border: `0.5px solid ${optimizeResult.applied === optimizeResult.total
                                ? "rgba(29,158,117,0.2)"
                                : "rgba(223,138,0,0.2)"}`,
                            borderRadius: "8px",
                            fontSize: "12px",
                            color: optimizeResult.applied === optimizeResult.total
                                ? "#3fc56e"
                                : "#ffc669",
                            width: "100%",
                            boxSizing: "border-box",
                        }, children: SP_JSX.jsx("span", { children: optimizeResult.applied === optimizeResult.total
                                ? "All applied"
                                : `${optimizeResult.applied}/${optimizeResult.total} applied` }) }) }) })), SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: !connected || !supported || applyingAll, onClick: handleOptimize, children: applyingAll
                            ? "Applying..."
                            : allSafeActive
                                ? "All good"
                                : "Optimize Safe" }) }) }), SP_JSX.jsxs(DFL.PanelSection, { title: "Power & stability", children: [SP_JSX.jsx(InfoRow, { label: "Prevent lag spikes", subtitle: "Disables WiFi power save and PCIe power states", explanation: "SteamOS enables WiFi power saving at multiple levels - the wireless chip, the PCIe bus connecting it to the CPU, and driver-level low power modes. These cause latency spikes, packet batching, and throughput degradation during sustained streaming. This toggle disables all of them, keeping the WiFi hardware fully awake. Battery impact is minimal.", ...getBadge(s?.power_save_disabled ?? false, "power_save", status, errors.power_save ?? null), checked: s?.power_save_disabled ?? false, error: errors.power_save, onChange: (val) => handleToggle("power_save", () => setPowerSave(val)) }), SP_JSX.jsx(InfoRow, { label: "Stop background scanning", subtitle: "Locks to current AP - disable to switch networks or roam", explanation: "Your Steam Deck scans for other WiFi networks every 2 minutes even while connected. Each scan causes a brief interruption that can drop packets and stutter game streaming. Locking to your current access point stops these scans entirely. You'll need to disable this before switching to a different network or access point.", ...getBadge(s?.bssid_lock_enabled ?? false, "bssid_lock", status, errors.bssid_lock ?? null, "locked"), checked: s?.bssid_lock_enabled ?? false, disabled: !connected && !s?.bssid_lock_enabled, error: errors.bssid_lock, onChange: (val) => handleToggle("bssid_lock", () => setBssidLock(val)) }), SP_JSX.jsx(InfoRow, { label: "Auto-fix on wake", subtitle: "Reapplies settings after sleep (NM dispatcher)", explanation: "SteamOS often resets WiFi settings when the Deck wakes from sleep. This installs a small script that automatically re-applies your optimizations every time the WiFi reconnects. It runs outside of Decky, so it works even if Decky has issues. Removing the plugin will also remove this script.", ...getBadge(s?.auto_fix_on_wake ?? false, undefined, status, errors.auto_fix ?? null), checked: s?.auto_fix_on_wake ?? false, error: errors.auto_fix, onChange: (val) => handleToggle("auto_fix", () => setAutoFix(val)) }), SP_JSX.jsx(InfoRow, { label: "Network buffer tuning", subtitle: "Optimize UDP buffers and TX queue for streaming", explanation: "Increases kernel network buffer sizes and transmit queue length to handle the bursty UDP traffic that game streaming produces. Without this, packets can be dropped during high-bitrate moments, causing frame drops or brief quality dips. These settings benefit all network interfaces, including ethernet. They reset on every reboot.", ...getBadge(s?.buffer_tuning_enabled ?? false, "buffer_tuning", status, errors.buffer_tuning ?? null), checked: s?.buffer_tuning_enabled ?? false, error: errors.buffer_tuning, onChange: (val) => handleToggle("buffer_tuning", () => setBufferTuning(val)) })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "Advanced", children: [SP_JSX.jsx(InfoRow, { label: isOled ? "Prefer 5 GHz / 6 GHz" : "Prefer 5 GHz band", subtitle: "Avoid 2.4 GHz Bluetooth interference", explanation: `Bluetooth operates on the 2.4 GHz band${!isOled ? ", and on the LCD model the antennas are shared" : ""}. Using 5 GHz${isOled ? " or 6 GHz" : ""} for WiFi avoids this interference entirely, giving you a cleaner, faster connection. Only enable this if your router supports 5 GHz. If your network is 2.4 GHz only, this will prevent you from connecting.`, ...getBadge(s?.band_preference_enabled ?? false, undefined, status, errors.band_preference ?? null, "5 GHz"), checked: s?.band_preference_enabled ?? false, disabled: !connected && !s?.band_preference_enabled, error: errors.band_preference, onChange: (val) => handleToggle("band_preference", () => setBandPreference(val, s?.band_preference ?? "a")) }), SP_JSX.jsx(InfoRow, { label: "Custom DNS", subtitle: "Override DNS servers for this network", explanation: "Your internet provider's DNS servers translate domain names (like store.steampowered.com) into IP addresses. They can be slow or unreliable. Switching to a public DNS like Cloudflare (1.1.1.1) or Google (8.8.8.8) can speed up initial connections and improve reliability. This only affects the current WiFi network.", ...getBadge(s?.dns_enabled ?? false, undefined, status, errors.dns ?? null, "set"), checked: s?.dns_enabled ?? false, disabled: !connected && !s?.dns_enabled, error: errors.dns, onChange: (val) => handleToggle("dns", () => setDns(val, s?.dns_provider ?? "cloudflare", customDnsInput)), children: s?.dns_enabled && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { label: "DNS Provider", rgOptions: DNS_OPTIONS, selectedOption: s?.dns_provider ?? "cloudflare", onChange: (option) => {
                                            const custom = option.data === "custom" ? customDnsInput : "";
                                            handleToggle("dns", () => setDns(true, option.data, custom));
                                        } }) }), s?.dns_provider === "custom" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.TextField, { label: "DNS servers (space-separated)", value: customDnsInput, onChange: (e) => setCustomDnsInput(e.target.value), onBlur: () => {
                                            if (customDnsInput) {
                                                handleToggle("dns", () => setDns(true, "custom", customDnsInput));
                                            }
                                        } }) }))] })) }), SP_JSX.jsx(InfoRow, { label: "Disable IPv6", subtitle: "Use IPv4 only on this network", explanation: "Some networks have poor or misconfigured IPv6 support, which can cause slow DNS resolution, connection timeouts, or routing issues. Disabling IPv6 forces all traffic through IPv4. Only enable this if you're experiencing issues - most modern networks handle IPv6 fine.", ...getBadge(s?.ipv6_disabled ?? false, undefined, status, errors.ipv6 ?? null), checked: s?.ipv6_disabled ?? false, disabled: !connected && !s?.ipv6_disabled, error: errors.ipv6, onChange: (val) => handleToggle("ipv6", () => setIpv6(val)) }), status?.live?.backend_tool_available && (() => {
                        const currentBackend = status?.live?.wifi_backend || "iwd";
                        const isWpa = currentBackend === "wpa_supplicant";
                        const switching = backendSwitch?.in_progress ?? false;
                        // Optimistic: during a switch, reflect the target so the toggle matches
                        // the user's click until the operation completes. On failure, it snaps
                        // back to the actual backend.
                        const checkedVal = switching && backendSwitch?.target
                            ? backendSwitch.target === "wpa_supplicant"
                            : isWpa;
                        const phaseText = switching
                            ? BACKEND_PHASE_TEXT[backendSwitch.phase] || "Working…"
                            : null;
                        const backendBadge = errors.wifi_backend
                            ? { badge: "error", text: "failed" }
                            : switching
                                ? { badge: "unknown", text: "…" }
                                : isWpa
                                    ? { badge: "active", text: "wpa_supplicant" }
                                    : { badge: "off", text: "iwd" };
                        // Inline result shown right under the toggle so it's visible where the
                        // user clicked — the top-of-panel banner is often off-screen when the
                        // user is scrolled down to the Advanced section.
                        const lastResult = !switching &&
                            !errors.wifi_backend &&
                            backendSwitch?.result &&
                            !backendSwitch.result.needs_reboot
                            ? backendSwitch.result
                            : null;
                        return (SP_JSX.jsx(InfoRow, { label: "Use wpa_supplicant backend", subtitle: phaseText
                                ? phaseText
                                : "Alternate WiFi backend — can fix OLED sleep/wake issues", explanation: "SteamOS 3.6+ defaults to iwd for WiFi. Some OLED owners see disconnects after sleep, 5 GHz dropouts, or 'invalid password' errors with iwd. Switching to wpa_supplicant trades slightly slower reconnect (about 5s vs 1-2s) for broader compatibility and better stability on certain routers. The setting survives reboots and SteamOS updates. On OLED, switching to wpa_supplicant may briefly destroy the wlan0 interface \u2014 the plugin automatically recreates it, but a reboot is needed as a last resort.", badge: backendBadge.badge, text: backendBadge.text, checked: checkedVal, disabled: switching, error: errors.wifi_backend, onChange: handleBackendToggle, children: lastResult?.success && (() => {
                                const timedOut = lastResult.reconnect_timed_out;
                                const parts = [`Switched to ${lastResult.backend}`];
                                if (lastResult.recovery_performed)
                                    parts.push("wlan0 interface recreated");
                                if (timedOut)
                                    parts.push("WiFi didn't reconnect");
                                return (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: {
                                            fontSize: "11px",
                                            color: timedOut ? "#ffc669" : "#3fc56e",
                                            padding: "2px 0",
                                        }, children: [timedOut ? "⚠" : "✓", " ", parts.join(" · ")] }) }));
                            })() }));
                    })()] }), SP_JSX.jsxs(DFL.PanelSection, { title: "Live status", children: [connected && status?.live?.ip_address && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: "10px", color: "#8a8a9a" }, children: ["IP: ", status.live.ip_address] }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(StatsGrid, { live: status?.live ?? {}, connected: connected }) })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "Actions", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: !connected || !supported, onClick: () => handleToggle("reapply", () => reapplyAll()), children: "Force Reapply All" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: async () => {
                                await resetSettings();
                                await refreshStatus();
                            }, children: "Reset Settings" }) })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "Updates", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { label: "Update channel", rgOptions: [
                                { data: "stable", label: "Stable" },
                                { data: "beta", label: "Beta" },
                            ], selectedOption: s?.update_channel ?? "stable", onChange: async (option) => {
                                await setUpdateChannel(option.data);
                                setUpdateInfo(null);
                                await refreshStatus();
                            } }) }), updating ? (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: "12px", color: "#60baff" }, children: "Updating... plugin will restart momentarily." }) })) : updateInfo?.update_available ? (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: "12px", color: "#3fc56e" }, children: ["v", updateInfo.latest_version, " available (you have v", updateInfo.current_version, ")"] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: async () => {
                                        setUpdating(true);
                                        try {
                                            await applyUpdate();
                                        }
                                        catch { /* restart killed connection */ }
                                    }, children: "Update Now" }) })] })) : (SP_JSX.jsxs(SP_JSX.Fragment, { children: [updateInfo && updateInfo.success === false && updateInfo.message && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: "10px", color: "#ff878c" }, children: updateInfo.message }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: checkingUpdate, onClick: async () => {
                                        setCheckingUpdate(true);
                                        lastUpdateCheckAtRef.current = Date.now();
                                        try {
                                            const result = await checkForUpdate();
                                            setUpdateInfo(result);
                                        }
                                        catch { /* ignore */ }
                                        setCheckingUpdate(false);
                                    }, children: checkingUpdate ? "Checking..." : (updateInfo?.success && !updateInfo?.update_available) ? "Up to date" : "Check for Updates" }) })] }))] }), SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: "10px", color: "#4a4a5a" }, children: ["v", status?.version ?? "?", " - by jasonridesabike"] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: "10px", color: "#4a4a5a" }, children: ["If WiFi won't reconnect, a reboot usually fixes it.", SP_JSX.jsx("br", {}), "Bugs? Report at github.com/ArcadaLabs-Jason/WifiOptimizer"] }) })] })] }));
}
var index = definePlugin(() => {
    return {
        name: "WiFi Optimizer",
        titleView: SP_JSX.jsx("div", { className: DFL.staticClasses.Title, children: "WiFi Optimizer" }),
        content: SP_JSX.jsx(Content, {}),
        icon: SP_JSX.jsx(FaWifi, {}),
        onDismount() { },
    };
});

export { index as default };
//# sourceMappingURL=index.js.map
