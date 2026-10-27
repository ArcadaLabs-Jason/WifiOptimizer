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
const toaster = api.toaster;
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
const setCake = callable("set_cake");
const optimizeSafe = callable("optimize_safe");
const reapplyAll = callable("reapply_all");
const repairDrifted = callable("repair_drifted");
const reapplyVolatile = callable("reapply_volatile");
const allowWpa2 = callable("allow_wpa2");
const moveBssidLock = callable("move_bssid_lock");
const resetSettings = callable("reset_settings");
const acknowledgeSettingsRecovered = callable("acknowledge_settings_recovered");
const setUpdateChannel = callable("set_update_channel");
const setUpdateNotify = callable("set_update_notify");
const updateNotice = callable("update_notice");
const markUpdateNotified = callable("mark_update_notified");
const checkForUpdate = callable("check_for_update");
const applyUpdate = callable("apply_update");
const getUpdateProgress = callable("get_update_progress");
const startBackendSwitch = callable("start_backend_switch");
const getBackendSwitchStatus = callable("get_backend_switch_status");
const setStreamingMode = callable("set_streaming_mode");
const setStreamingHold = callable("set_streaming_hold");
const setStreamingApp = callable("set_streaming_app");
const setStreamingPatterns = callable("set_streaming_patterns");
const listStreamingApps = callable("list_streaming_apps");
const pokeDetection = callable("poke_detection");
const getRegdomain = callable("get_regdomain");
const setRegdomain = callable("set_regdomain");
const getDiagnosticInfo = callable("get_diagnostic_info");
const saveDiagnosticInfo = callable("save_diagnostic_info");

const ERROR_MESSAGES = {
    no_wifi: "Not connected to WiFi. Connect first, then optimize.",
    iw_failed: "Couldn't change a WiFi setting. Turn WiFi off and on, then try again.",
    nmcli_failed: "Couldn't update this network's saved settings. Try again in a moment. If it keeps failing, restart the device.",
    write_failed: "Couldn't install the auto-fix script. Restart the device, then try again.",
    // An earlier iw is still stuck in the driver: only a restart clears it.
    not_responding: "The WiFi card isn't responding. Restart the device.",
    // Reset could neither read the settings file nor move it aside.
    settings_unreadable: "The settings file can't be read or replaced. Remove ~/homebrew/settings/WiFi Optimizer/settings.json, then try Reset again.",
    // Shown while the backend reports settings_recovered.
    parse_error: "Your settings file couldn't be read, so settings are back to their defaults. Turn them back on, or select Optimize Safe.",
    unexpected: "Something went wrong. Try again. If it keeps happening, use Copy diagnostics at the bottom of this panel and report it.",
};

// Design tokens for the WiFi Optimizer panel: the four semantic banner
// colors (success/warning/error/info) that read well against the dark
// background of Decky's Quick Access Menu, and the shared sizes.
const theme = {
    // Status colors - used for banners, badges, and text callouts.
    // Each family has text (solid), bg (panel background at ~8% alpha),
    // badgeBg (chip/pill background at ~15% alpha), and border (~20% alpha).
    success: {
        text: "#3fc56e",
        bg: "rgba(29,158,117,0.08)",
        badgeBg: "rgba(29,158,117,0.15)",
        border: "rgba(29,158,117,0.2)",
    },
    warning: {
        text: "#ffc669",
        bg: "rgba(223,138,0,0.08)",
        badgeBg: "rgba(223,138,0,0.15)",
        border: "rgba(223,138,0,0.2)",
    },
    error: {
        text: "#ff878c",
        bg: "rgba(211,36,43,0.08)",
        badgeBg: "rgba(211,36,43,0.15)",
        border: "rgba(211,36,43,0.2)",
    },
    info: {
        text: "#60baff",
        bg: "rgba(55,138,221,0.08)",
        border: "rgba(55,138,221,0.2)",
        accentBg: "rgba(55,138,221,0.2)",
    },
    // Neutral text scale, lightest to darkest.
    text: {
        primary: "#e0e0e0",
        secondary: "#9a9aaa",
        tertiary: "#8a8a9a",
        subtitle: "#7a7a8a",
        muted: "#6a6a7a",
        dim: "#4a4a5a",
    },
    // White-alpha surfaces used for layered backgrounds.
    surface: {
        xs: "rgba(255,255,255,0.02)",
        sm: "rgba(255,255,255,0.04)",
        md: "rgba(255,255,255,0.06)",
        lg: "rgba(255,255,255,0.08)",
    },
    // Consistent sizing tokens reused across the panel.
    radius: {
        sm: "4px",
        md: "6px",
        lg: "8px",
        pill: "10px",
    },
    fontSize: {
        label: "9px",
        tiny: "10px",
        small: "11px",
        body: "12px",
        heading: "13px",
        icon: "14px",
    },
};

const BADGE_STYLES = {
    active: { background: theme.success.badgeBg, color: theme.success.text },
    drifted: { background: theme.warning.badgeBg, color: theme.warning.text },
    off: { background: theme.surface.md, color: theme.text.muted },
    error: { background: theme.error.badgeBg, color: theme.error.text },
    unknown: { background: theme.surface.md, color: theme.text.tertiary },
    paused: { background: theme.info.accentBg, color: theme.info.text },
};
function StatusBadge({ badge, text }) {
    const style = BADGE_STYLES[badge];
    return (SP_JSX.jsx("span", { style: {
            fontSize: theme.fontSize.small,
            padding: "2px 6px",
            borderRadius: theme.radius.sm,
            whiteSpace: "nowrap",
            background: style.background,
            color: style.color,
        }, children: text }));
}

// The panel's "Show details" toggle. The (i) next to each label only answers
// a tap: it sits inside the toggle's label, where the D-pad cannot focus it
// apart from the toggle itself. The toggle opens every explanation at once,
// so they can be read with a controller too.
const DetailsContext = SP_REACT.createContext(false);
// Shown in place of the subtitle while a change that reconnects WiFi runs.
// Those take from a few seconds to about a minute.
const WORKING_TEXT = "Working, WiFi will reconnect...";
// An error or notice on a row of its own, under the control it is about,
// in the colors and size a toggle's own message uses.
function MessageRow({ error, notice }) {
    if (!error && !notice)
        return null;
    return (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                fontSize: theme.fontSize.small,
                color: error ? theme.error.text : theme.info.text,
            }, children: error || notice }) }));
}
function InfoRow({ label, subtitle, explanation, badge, text, checked, disabled = false, error, notice, working = false, onChange, children, }) {
    const showBadge = badge !== undefined && text !== undefined;
    const [tapped, setTapped] = SP_REACT.useState(false);
    const showAll = SP_REACT.useContext(DetailsContext);
    const expanded = tapped || showAll;
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: SP_JSX.jsxs("span", { style: { display: "flex", alignItems: "center", gap: "4px" }, children: [SP_JSX.jsx("span", { onClick: (e) => {
                                    e.stopPropagation();
                                    setTapped(!tapped);
                                }, style: {
                                    display: "inline-flex",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    width: "16px",
                                    height: "16px",
                                    borderRadius: "50%",
                                    background: expanded ? theme.info.accentBg : theme.surface.lg,
                                    color: expanded ? theme.info.text : theme.text.tertiary,
                                    fontSize: theme.fontSize.tiny,
                                    fontWeight: 700,
                                    cursor: "pointer",
                                    flexShrink: 0,
                                }, children: "i" }), SP_JSX.jsx("span", { children: label })] }), description: SP_JSX.jsxs("span", { style: { display: "block" }, children: [showBadge && (SP_JSX.jsx("span", { style: {
                                    display: "flex",
                                    justifyContent: "flex-end",
                                    marginBottom: "4px",
                                }, children: SP_JSX.jsx(StatusBadge, { badge: badge, text: text }) })), working ? (SP_JSX.jsx("span", { style: { color: theme.info.text, fontSize: theme.fontSize.small }, children: WORKING_TEXT })) : error ? (SP_JSX.jsx("span", { style: { color: theme.error.text }, children: error })) : notice ? (SP_JSX.jsx("span", { style: { color: theme.info.text, fontSize: theme.fontSize.small }, children: notice })) : (SP_JSX.jsx("span", { style: { color: theme.text.subtitle, fontSize: theme.fontSize.small }, children: subtitle }))] }), checked: checked, disabled: disabled, onChange: onChange }) }), expanded && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                        padding: "8px 12px",
                        background: theme.surface.xs,
                        borderRadius: theme.radius.md,
                        fontSize: theme.fontSize.small,
                        lineHeight: "1.5",
                        color: theme.text.secondary,
                    }, children: explanation }) })), children] }));
}

function signalColor(dbm) {
    if (!dbm)
        return theme.text.muted;
    const val = parseInt(dbm);
    if (isNaN(val))
        return theme.text.muted;
    if (val > -50)
        return theme.success.text;
    if (val > -70)
        return theme.text.primary;
    if (val > -80)
        return theme.warning.text;
    return theme.error.text;
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
        return theme.text.muted;
    const mhz = parseInt(freqStr);
    if (isNaN(mhz))
        return theme.text.primary;
    if (mhz < 3000)
        return theme.warning.text; // 2.4 GHz (suboptimal)
    return theme.success.text; // 5/6 GHz (good)
}
// iw reports the rate with its modulation details ("866.7 MBit/s VHT-MCS 9
// 80MHz short GI VHT-NSS 2"), which wrapped over four lines. The rate is the
// part worth showing; anything not in that form is shown as it came.
function speedLabel(raw) {
    const found = raw ? /^\s*([\d.]+)\s*MBit\/s/i.exec(raw) : null;
    return found ? `${Math.round(parseFloat(found[1]))} Mbit/s` : raw;
}
function StatsGrid({ live, connected }) {
    const na = "--";
    const signal = connected ? live.signal_dbm ?? na : na;
    const speed = connected ? speedLabel(live.tx_bitrate) ?? na : na;
    const band = connected ? bandLabel(live.frequency) : na;
    // The number only: iw has reported "5180", "5180 MHz" and "5180.0", which
    // showed as "5180 MHz MHz" or "5180.0 MHz". Read the way bandLabel does.
    const mhz = connected ? parseInt(live.frequency ?? "") : NaN;
    const freq = isNaN(mhz) ? "" : String(mhz);
    const channel = connected ? live.channel ?? na : na;
    const cell = {
        background: theme.surface.sm,
        borderRadius: theme.radius.sm,
        padding: "4px 8px",
    };
    const lbl = {
        fontSize: theme.fontSize.label,
        color: theme.text.muted,
        textTransform: "uppercase",
        letterSpacing: "0.5px",
    };
    const val = (color) => ({
        fontSize: theme.fontSize.heading,
        fontWeight: 500,
        color: connected ? color : theme.text.muted,
    });
    return (SP_JSX.jsxs(DFL.Focusable, { style: { display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px" }, children: [SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Signal" }), SP_JSX.jsx("div", { style: val(signalColor(live.signal_dbm)), children: signal })] }), SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Speed" }), SP_JSX.jsx("div", { style: val(theme.text.primary), children: speed })] }), SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Band" }), SP_JSX.jsx("div", { style: val(bandColor(live.frequency)), children: band }), freq && (SP_JSX.jsxs("div", { style: { fontSize: theme.fontSize.label, color: theme.text.muted }, children: [freq, " MHz"] }))] }), SP_JSX.jsxs("div", { style: cell, children: [SP_JSX.jsx("div", { style: lbl, children: "Channel" }), SP_JSX.jsx("div", { style: val(theme.text.primary), children: channel })] })] }));
}

// Shared banner styling for the panel's many inline notifications
// (unknown hardware, drift alert, optimize result, backend switch
// result, update errors, etc.). Each banner sits in its own
// PanelSection so Decky's QAM can focus it with gamepad navigation.
function Banner({ variant, children }) {
    const palette = theme[variant];
    return (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                    display: "flex",
                    alignItems: "center",
                    gap: "6px",
                    padding: "8px 12px",
                    background: palette.bg,
                    border: `0.5px solid ${palette.border}`,
                    borderRadius: theme.radius.lg,
                    fontSize: theme.fontSize.body,
                    color: palette.text,
                    width: "100%",
                    boxSizing: "border-box",
                }, children: SP_JSX.jsx("span", { children: children }) }) }) }));
}

const PHASE_TEXT = {
    idle: "",
    switching: "Switching backend...",
    reconnecting: "Reconnecting...",
    done: "",
    failed: "",
};
const EXPLANATION = "wpa_supplicant can fix disconnects after sleep, 5 GHz dropouts and " +
    "'invalid password' errors that some devices see with iwd, and reconnects " +
    "a little slower. Switching takes WiFi down for up to a minute. If WiFi " +
    "stops connecting afterwards, switch back. The choice survives reboots and " +
    "OS updates.";
// What a finished switch says, in the banner at the top of the panel and
// under the toggle. A failure is checked first: a switch that failed and
// left no WiFi card also asks for a restart, and was announced as
// "Switched to".
function backendResultText(r) {
    if (!r.success) {
        // The backend's own words say whether the switch itself took; this
        // fallback is true either way.
        if (r.needs_reboot) {
            return r.message ?? "WiFi didn't come back after the backend switch. Restart the device.";
        }
        return r.message ?? "The backend switch didn't work. If WiFi doesn't come back, restart the device.";
    }
    if (r.needs_reboot) {
        return `Switched to ${r.backend || r.target}, but the WiFi card didn't come back. Restart the device to finish.`;
    }
    const parts = [`Switched to ${r.backend || r.target}.`];
    if (r.recovery_performed)
        parts.push("The WiFi card had to be restarted along the way.");
    if (r.reconnect_timed_out) {
        parts.push("WiFi hasn't reconnected yet. Pick your network in the WiFi settings, or switch back here.");
    }
    return parts.join(" ");
}
function BackendToggleRow({ status, backendSwitch, error, isBusy, onToggle, }) {
    // The backend is genuinely unknown when no config declares one and neither
    // service reports active. Defaulting the label to iwd states something we
    // have not established, on a machine that may well be on wpa_supplicant.
    const currentBackend = status.live?.wifi_backend || "";
    const isWpa = currentBackend === "wpa_supplicant";
    const switching = backendSwitch?.in_progress ?? false;
    // Optimistic: during a switch, reflect the target so the toggle matches
    // the user's click until the operation completes. On failure, it snaps
    // back to the actual backend.
    const checked = switching && backendSwitch?.target
        ? backendSwitch.target === "wpa_supplicant"
        : isWpa;
    const phaseText = switching
        ? PHASE_TEXT[backendSwitch.phase] || "Working..."
        : null;
    // On the generic method a change made by Reset (or an uninstall and
    // reinstall without a reboot) lands at the next boot; until then the
    // backend running is the one shown, and this says what comes next.
    const next = status.live?.wifi_backend_next;
    const subtitle = phaseText ??
        (next && next !== currentBackend
            ? `Changes to ${next} from the next boot.`
            : isWpa
                ? "In use. Turn off to use iwd."
                : currentBackend === "iwd"
                    ? "Instead of iwd. Can fix sleep and stability problems."
                    : "Can fix sleep and stability problems.");
    const badge = error
        ? { badge: "error", text: "failed" }
        : switching
            ? { badge: "unknown", text: "switching" }
            : isWpa
                ? { badge: "active", text: "wpa_supplicant" }
                : currentBackend === "iwd"
                    ? { badge: "off", text: "iwd" }
                    : { badge: "unknown", text: "unknown" };
    // Inline result shown right under the toggle so it's visible where the
    // user clicked - the top-of-panel banner is often off-screen when the
    // user is scrolled down to the Advanced section.
    const lastResult = !switching && !error && backendSwitch?.result && !backendSwitch.result.needs_reboot
        ? backendSwitch.result
        : null;
    return (SP_JSX.jsx(InfoRow, { label: "Use wpa_supplicant backend", subtitle: subtitle, explanation: EXPLANATION, badge: badge.badge, text: badge.text, checked: checked, disabled: switching || isBusy, error: error, onChange: onToggle, children: lastResult?.success && (() => {
            const timedOut = lastResult.reconnect_timed_out;
            return (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                        fontSize: theme.fontSize.small,
                        color: timedOut ? theme.warning.text : theme.success.text,
                        padding: "2px 0",
                    }, children: backendResultText(lastResult) }) }));
        })() }));
}

const CHANNEL_OPTIONS = [
    { data: "stable", label: "Stable" },
    { data: "beta", label: "Beta" },
];
// The Updates panel section: channel selector, optional warning text for a
// stuck / failed update, and a button that context-switches between
// "Updating...", "Update now", "Up to date", and "Check for updates"
// depending on state.
function UpdatesSection({ channel, updating, updatingText, checkingUpdate, updateInfo, updateError, settingError, onChannelChange, notify, onNotifyChange, onApply, onCheck, busy = false, }) {
    return (SP_JSX.jsxs(DFL.PanelSection, { title: "Updates", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { label: "Update channel", rgOptions: CHANNEL_OPTIONS, selectedOption: channel, disabled: updating, onChange: (option) => onChannelChange(option.data) }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "Tell me about updates", description: "One notification per new version, never during a game or stream.", checked: notify, disabled: updating, onChange: onNotifyChange }) }), settingError && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.body, color: theme.warning.text }, children: settingError }) })), updateError && !updating && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.body, color: theme.warning.text }, children: updateError }) })), updating ? (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.body, color: theme.info.text }, children: updatingText }) })) : updateInfo?.update_available ? (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                                fontSize: theme.fontSize.body,
                                color: updateInfo.is_downgrade ? theme.info.text : theme.success.text,
                            }, children: updateInfo.is_downgrade
                                ? `Beta now offers v${updateInfo.latest_version}, older than your v${updateInfo.current_version}.`
                                : `v${updateInfo.latest_version} available (you have v${updateInfo.current_version})` }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: busy, onClick: onApply, children: updateInfo.is_downgrade
                                ? `Go back to v${updateInfo.latest_version}`
                                : "Update now" }) })] })) : (SP_JSX.jsxs(SP_JSX.Fragment, { children: [updateInfo && updateInfo.success === false && updateInfo.message && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.tiny, color: theme.error.text }, children: updateInfo.message }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: checkingUpdate || updating, onClick: onCheck, children: checkingUpdate
                                ? "Checking..."
                                : updateInfo?.success && !updateInfo?.update_available
                                    ? "Up to date"
                                    : "Check for updates" }) })] }))] }));
}

// Format a Unix-seconds timestamp as a relative-time string for display in
// the panel ("just now", "5 min ago", "2 hr ago", "1 day ago", "3 days ago").
// Returns "never" for the sentinel value 0 (settings not yet applied).
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
    const days = Math.floor(diff / 86400);
    return days === 1 ? "1 day ago" : `${days} days ago`;
}
// A backend call that never answers used to leave every control greyed out
// until the panel was reloaded. The slowest changes take about a minute.
const CALL_TIMEOUT_MS = 120 * 1000;
class NoAnswer extends Error {
}
// The call's own result, or NoAnswer once ms have passed without one.
function withTimeout(call, ms = CALL_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
        const id = setTimeout(() => reject(new NoAnswer()), ms);
        call.then((value) => {
            clearTimeout(id);
            resolve(value);
        }, (err) => {
            clearTimeout(id);
            reject(err);
        });
    });
}

// "Connected, 5 GHz, -55 dBm": the link at a glance, on the first screen.
// Whatever the link read leaves out is left out here too.
function connectionLine(live) {
    const parts = ["Connected"];
    if (live?.frequency && !isNaN(parseInt(live.frequency)))
        parts.push(bandLabel(live.frequency));
    const dbm = parseInt(live?.signal_dbm ?? "");
    if (!isNaN(dbm))
        parts.push(`${dbm} dBm`);
    return parts.join(", ");
}
function PanelHeader({ driver, lastApplied, lastEnforced, deviceLabel, streaming, fixesOn, connected = false, }) {
    const modelLabel = deviceLabel && deviceLabel !== "unknown"
        ? deviceLabel
        : driver && driver !== "unknown"
            ? driver
            : "Unknown WiFi card";
    const rowStyle = {
        fontSize: theme.fontSize.tiny,
        color: theme.text.muted,
    };
    return (SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("span", { style: {
                        fontSize: theme.fontSize.tiny,
                        background: theme.surface.md,
                        padding: "2px 8px",
                        borderRadius: theme.radius.pill,
                        color: theme.text.tertiary,
                    }, children: ["Device: ", modelLabel] }) }), connected && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.body, color: theme.text.primary }, children: connectionLine(streaming) }) })), streaming?.streaming_mode && (
            // Always in view, so nobody has to find the section to know whether
            // the fixes are on right now.
            SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("span", { style: {
                        fontSize: theme.fontSize.tiny,
                        background: streaming.streaming_active ? theme.success.badgeBg : theme.info.accentBg,
                        color: streaming.streaming_active ? theme.success.text : theme.info.text,
                        padding: "2px 8px",
                        borderRadius: theme.radius.pill,
                    }, children: streaming.streaming_active
                        ? streaming.streaming_app
                            ? `Auto mode: streaming ${streaming.streaming_app}${fixesOn ? ", fixes on" : ""}`
                            : streaming.streaming_held
                                ? fixesOn ? "Auto mode: held on, fixes on" : "Auto mode: held on"
                                : fixesOn ? "Auto mode: stream running, fixes on" : "Auto mode: stream running"
                        : fixesOn ? "Auto mode: no stream, fixes paused" : "Auto mode: no stream" }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: rowStyle, children: ["Last changed: ", timeAgo(lastApplied), lastEnforced ? (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx("br", {}), "Auto-applied: ", timeAgo(lastEnforced)] })) : ("")] }) })] }));
}

// The Send report preview, as text the controller can step through. Kept
// apart from the panel (no imports) so regress.py can run it under node.
// A block is what one D-pad step moves by. Twelve lines of up to 300
// characters did not fit the box, so a block is at most this many lines and
// about this many characters.
const BLOCK_LINES = 6;
const BLOCK_CHARS = 600;
// Control characters other than tab would print as nothing, or move the
// text around, so they are shown escaped, as JSON would show them.
function visible(text) {
    return text.replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, (c) => "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0"));
}
function keyText(key) {
    return /^[\w.-]+$/.test(key) ? key : JSON.stringify(key);
}
function walk(value, indent, head, out) {
    if (typeof value === "string") {
        const parts = value.replace(/\r\n?/g, "\n").replace(/\n+$/, "").split("\n");
        if (parts.length <= 1) {
            out.push(indent + head + JSON.stringify(value));
            return;
        }
        // Command output (iw, os-release) as the lines it has, not one line of
        // \n and \t escapes.
        out.push(indent + (head.trimEnd() || "|"));
        for (const part of parts)
            out.push(indent + "  " + visible(part.replace(/\t/g, "    ")));
        return;
    }
    if (Array.isArray(value)) {
        if (!value.length) {
            out.push(indent + head + "[]");
            return;
        }
        if (head)
            out.push(indent + head.trimEnd());
        const inner = head ? indent + "  " : indent;
        for (const item of value)
            walk(item, inner, "- ", out);
        return;
    }
    if (value && typeof value === "object") {
        const entries = Object.entries(value);
        if (!entries.length) {
            out.push(indent + head + "{}");
            return;
        }
        if (head)
            out.push(indent + head.trimEnd());
        const inner = head ? indent + "  " : indent;
        for (const [key, item] of entries)
            walk(item, inner, keyText(key) + ": ", out);
        return;
    }
    // Numbers, booleans and null, as JSON writes them.
    out.push(indent + head + String(JSON.stringify(value) ?? value));
}
// Every value in the report, one line each, with nesting shown by indent.
function previewLines(payload) {
    const out = [];
    walk(payload, "", "", out);
    return out;
}
// The lines in blocks of at most BLOCK_LINES lines and BLOCK_CHARS
// characters. A line longer than that is cut across blocks, so no block is
// taller than the box.
function previewBlocks(lines) {
    const pieces = [];
    for (const line of lines) {
        if (line.length <= BLOCK_CHARS)
            pieces.push(line);
        else
            for (let i = 0; i < line.length; i += BLOCK_CHARS)
                pieces.push(line.slice(i, i + BLOCK_CHARS));
    }
    const blocks = [];
    let current = [];
    let chars = 0;
    for (const piece of pieces) {
        if (current.length && (current.length >= BLOCK_LINES || chars + piece.length > BLOCK_CHARS)) {
            blocks.push(current.join("\n"));
            current = [];
            chars = 0;
        }
        current.push(piece);
        chars += piece.length;
    }
    if (current.length)
        blocks.push(current.join("\n"));
    return blocks;
}

const getReportState = callable("get_report_state");
const previewReport = callable("preview_report");
const sendReport = callable("send_report");
const dismissReportNotice = callable("dismiss_report_notice");
const cancelPendingReport = callable("cancel_pending_report");
const SEND_FAIL_TEXT = "Couldn't send the report. Try again later, or use Copy diagnostics and post it on GitHub.";
const PREVIEW_FAIL_TEXT = "Couldn't put the report together. Try again in a moment.";
// The report is saved before it is sent, so a send with no answer yet can
// still go through.
const SEND_SLOW_TEXT = "The report may still be sending. Check back here in a minute.";
const CANCEL_FAIL_TEXT = "Couldn't delete the saved report. Try again in a moment.";
const READY_TEXT = "Report ready to review.";
// The preview collects the same diagnostics as Copy diagnostics, and a send
// can take as long again, so both get the limit Copy diagnostics has
// (CALL_TIMEOUT_MS). The state read and the notices answer from a few small
// files and one bounded log read.
const STATE_LIMIT_MS = 60000;
// How often the state is read again while a report is waiting, or while no
// read has come through yet, so the waiting line and the reference follow a
// send made in the background without the panel being reopened.
const STATE_POLL_MS = 20000;
// Shared by the footer button and the wake notice, and kept for as long as
// the plugin is loaded, so a result stays on screen until it is dismissed
// even if the panel is closed and opened again.
// inFlight counts the preview, send and cancel calls under way, so the
// buttons stay disabled until the last of them has ended, not the first.
// collecting is set while a preview is being put together, which takes as
// long as Copy diagnostics and says so the same way.
let shared$1 = {
    state: null,
    inFlight: 0,
    collecting: false,
    outcome: null,
    ready: null,
    cancelFailed: false,
};
const listeners$1 = new Set();
let loading = null;
// The one read chained after a read under way (refreshReportState).
let again = null;
// Counts the actions that have ended (a send, a cancel, a dismiss), and the
// count a read under way started at.
let actionSeq = 0;
let loadingSeq = 0;
let pollTimer = null;
// Notices dismissed during this load. They stay hidden whatever the backend
// answered, so Dismiss never looks as if it did nothing. One the backend
// could not record comes back after a reload. The wake notice also goes once
// a report has been sent or saved, which answers it, and comes back if that
// saved report is withdrawn, unless Dismiss hid it (wakeDismissed).
const hidden = { wake: false, wakeDismissed: false, dropped: false, sentRef: "" };
function update(patch) {
    shared$1 = { ...shared$1, ...patch };
    listeners$1.forEach((listener) => listener());
}
function begin(patch = {}) {
    update({ ...patch, inFlight: shared$1.inFlight + 1 });
}
function end(patch = {}) {
    actionSeq += 1;
    update({ ...patch, inFlight: Math.max(0, shared$1.inFlight - 1) });
}
// One timer at a time, only while something here is on screen. A wake read
// that waits for its retry is polled too, so the notice follows its answer.
function schedulePoll() {
    if (pollTimer || !listeners$1.size)
        return;
    if (shared$1.state && !shared$1.state.pending && !shared$1.state.wake_unknown)
        return;
    pollTimer = setTimeout(() => {
        pollTimer = null;
        // While hidden, the visibility handler reads it when the panel is back.
        if (document.visibilityState === "visible")
            void refreshReportState();
    }, STATE_POLL_MS);
}
function stopPoll() {
    if (pollTimer) {
        clearTimeout(pollTimer);
        pollTimer = null;
    }
}
function onVisibility() {
    if (document.visibilityState === "visible")
        void refreshReportState();
}
// Read the state again. A read already under way is shared by whoever asks
// while it runs, unless an action has ended since it started: it may then
// answer with the state from before the action, so one more read is chained
// after it (only one, however many ask).
function refreshReportState() {
    if (loading) {
        if (loadingSeq === actionSeq)
            return loading;
        if (!again) {
            again = loading.then(() => {
                again = null;
                return refreshReportState();
            });
        }
        return again;
    }
    loadingSeq = actionSeq;
    const seq = loadingSeq;
    loading = withTimeout(getReportState(), STATE_LIMIT_MS)
        .then((state) => {
        if (!state || typeof state !== "object")
            return;
        // A dropped report the backend no longer reports was dismissed for
        // good, so the next one is shown again.
        if (!state.dropped)
            hidden.dropped = false;
        // "Saved, sent later" is over once nothing is waiting: the report was
        // sent (its reference shows), dropped or withdrawn. It stayed beside
        // "Your saved report was sent."
        const outcome = shared$1.outcome;
        const done = !!outcome?.queued && !state.pending && seq >= (outcome.seq ?? 0);
        update(done ? { state, outcome: null } : { state });
    })
        .catch((e) => console.error("report state error", e))
        .finally(() => {
        loading = null;
        schedulePoll();
    });
    return loading;
}
function useReportState() {
    const [, setTick] = SP_REACT.useState(0);
    SP_REACT.useEffect(() => {
        const listener = () => setTick((n) => n + 1);
        if (!listeners$1.size)
            document.addEventListener("visibilitychange", onVisibility);
        listeners$1.add(listener);
        void refreshReportState();
        return () => {
            listeners$1.delete(listener);
            if (!listeners$1.size) {
                document.removeEventListener("visibilitychange", onVisibility);
                stopPoll();
            }
        };
    }, []);
    return shared$1;
}
// Split into blocks the controller can step through: one long block of text
// cannot be scrolled with the D-pad, but focus moving from block to block
// scrolls the box along with it. previewBlocks keeps each block short enough
// to fit the box, and the block in focus is outlined.
function ReportPreviewModal({ text, onSend, closeModal, }) {
    const blocks = previewBlocks(text.split("\n"));
    const [focused, setFocused] = SP_REACT.useState(-1);
    return (SP_JSX.jsx(DFL.ConfirmModal, { strTitle: "Send this report to the developer?", strDescription: "This is everything that is sent. Network names and addresses are masked.", strOKButtonText: "Send", strCancelButtonText: "Cancel", onOK: onSend, closeModal: closeModal, children: SP_JSX.jsx(DFL.Focusable, { style: {
                maxHeight: "50vh",
                overflowY: "auto",
                marginTop: "8px",
                padding: "8px",
                background: theme.surface.md,
                borderRadius: theme.radius.md,
            }, children: blocks.map((block, i) => (SP_JSX.jsx(DFL.Focusable, { onActivate: () => { }, onGamepadFocus: () => setFocused(i), onGamepadBlur: () => setFocused((f) => (f === i ? -1 : f)), onFocus: () => setFocused(i), onBlur: () => setFocused((f) => (f === i ? -1 : f)), style: {
                    fontFamily: "monospace",
                    fontSize: theme.fontSize.body,
                    color: theme.text.primary,
                    whiteSpace: "pre-wrap",
                    wordBreak: "break-all",
                    padding: "2px 4px",
                    borderRadius: theme.radius.sm,
                    outline: focused === i ? `2px solid ${theme.info.text}` : "none",
                }, children: block }, i))) }) }));
}
async function send(id, from) {
    begin({ outcome: null, ready: null });
    let result = null;
    let noAnswer = false;
    try {
        result = await withTimeout(sendReport(id), CALL_TIMEOUT_MS);
    }
    catch (e) {
        noAnswer = e instanceof NoAnswer;
        console.error("send report error", e);
    }
    const status = result?.status;
    if (noAnswer || status === "sent" || status === "queued")
        hidden.wake = true;
    end({
        outcome: noAnswer
            ? { text: SEND_SLOW_TEXT, tone: "info", from }
            : {
                text: typeof result?.message === "string" ? result.message : SEND_FAIL_TEXT,
                tone: status === "sent" ? "success" : status === "queued" ? "info" : "error",
                from,
                queued: status === "queued",
                // end() counts this send as ended before it stores the outcome.
                seq: actionSeq + 1,
            },
    });
    await refreshReportState();
}
function openPreview(ready) {
    const { id, from } = ready;
    update({ ready: null });
    DFL.showModal(SP_JSX.jsx(ReportPreviewModal, { text: ready.text, onSend: () => void send(id, from) }));
}
async function startReport(from) {
    if (shared$1.inFlight)
        return;
    begin({ outcome: null, ready: null, collecting: true });
    let preview = null;
    try {
        preview = await withTimeout(previewReport(), CALL_TIMEOUT_MS);
    }
    catch (e) {
        console.error("preview report error", e);
    }
    end({ collecting: false });
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
    if (document.visibilityState === "visible")
        openPreview(ready);
    else
        update({ ready });
}
async function cancelPending() {
    if (shared$1.inFlight)
        return;
    begin({ cancelFailed: false });
    let done = false;
    try {
        done = (await withTimeout(cancelPendingReport(), STATE_LIMIT_MS))?.success === true;
    }
    catch (e) {
        console.error("cancel report error", e);
    }
    // A result that said the report was saved for later is no longer true,
    // and the wake notice that report answered is open again (the backend
    // shows it again too, D17), unless it was dismissed.
    if (done && !hidden.wakeDismissed)
        hidden.wake = false;
    end({ cancelFailed: !done, outcome: done ? null : shared$1.outcome });
    await refreshReportState();
}
const rowStyle = { fontSize: theme.fontSize.small, color: theme.text.tertiary };
function OutcomeRows({ outcome }) {
    const color = outcome.tone === "success"
        ? theme.success.text
        : outcome.tone === "error"
            ? theme.error.text
            : theme.info.text;
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { ...rowStyle, color }, children: outcome.text }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => update({ outcome: null }), children: "Dismiss" }) })] }));
}
function ReadyRows({ ready, working }) {
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { ...rowStyle, color: theme.info.text }, children: READY_TEXT }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: working, onClick: () => openPreview(ready), children: "Open the report" }) })] }));
}
// Beside Copy diagnostics, inside the footer's section.
function SendReportRow() {
    const { state, inFlight, collecting, outcome, ready, cancelFailed } = useReportState();
    const working = inFlight > 0;
    if (!state?.enabled)
        return null;
    const sentRef = state.sent_ref && state.sent_ref !== hidden.sentRef ? state.sent_ref : "";
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: working, onClick: () => void startReport("footer"), children: collecting ? "Collecting..." : working ? "Working..." : "Send report to the developer" }) }), ready && (ready.from === "footer" || !wakeNoticeOffersReady(state, outcome)) && (SP_JSX.jsx(ReadyRows, { ready: ready, working: working })), outcome?.from === "footer" && SP_JSX.jsx(OutcomeRows, { outcome: outcome }), state.pending && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: rowStyle, children: "A report is waiting to be sent." }) }), cancelFailed && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { ...rowStyle, color: theme.error.text }, children: CANCEL_FAIL_TEXT }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: working, onClick: () => void cancelPending(), children: "Don't send it" }) })] })), state.dropped && !hidden.dropped && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { ...rowStyle, color: theme.warning.text }, children: "Your saved report couldn't be sent. Use Copy diagnostics instead." }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: async () => {
                                hidden.dropped = true;
                                update({});
                                try {
                                    await withTimeout(dismissReportNotice("dropped"), STATE_LIMIT_MS);
                                }
                                catch (e) {
                                    console.error("dismiss report notice error", e);
                                }
                                actionSeq += 1;
                                await refreshReportState();
                            }, children: "Dismiss" }) })] })), sentRef && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { ...rowStyle, color: theme.success.text }, children: ["Your saved report was sent. Reference ", sentRef, ". Quote it if you post about this."] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: async () => {
                                hidden.sentRef = sentRef;
                                update({});
                                try {
                                    await withTimeout(dismissReportNotice("sent"), STATE_LIMIT_MS);
                                }
                                catch (e) {
                                    console.error("dismiss report notice error", e);
                                }
                                actionSeq += 1;
                                await refreshReportState();
                            }, children: "Dismiss" }) })] }))] }));
}
// The wake's date and time, in the device's own format.
function wakeNoticeText(at) {
    const when = at > 0
        ? ` (${new Date(at * 1000).toLocaleString(undefined, {
            year: "numeric",
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
        })})`
        : "";
    return `WiFi didn't come back after sleep${when}. A report helps find out why.`;
}
// Whether the wake notice is on screen with its own buttons, where a preview
// it started is offered. The same conditions WakeFailureNotice renders by.
function wakeNoticeOffersReady(state, outcome) {
    if (!state?.enabled || outcome?.from === "wake")
        return false;
    return state.wake_failure && !hidden.wake && !state.pending;
}
// Near the top of the panel, for a wake in the previous boot that left WiFi
// down, until dismissed or answered with a report.
function WakeFailureNotice() {
    const { state, inFlight, collecting, outcome, ready } = useReportState();
    const working = inFlight > 0;
    const fromHere = outcome?.from === "wake" ? outcome : null;
    if (!state?.enabled || (!state.wake_failure && !fromHere))
        return null;
    // A report already sent, saved or waiting answers the notice.
    if (!fromHere && (hidden.wake || state.pending))
        return null;
    const dismiss = async () => {
        hidden.wake = true;
        hidden.wakeDismissed = true;
        // Only the notice's own result goes with it, not one the footer shows.
        if (shared$1.outcome?.from === "wake")
            update({ outcome: null });
        else
            update({});
        try {
            await withTimeout(dismissReportNotice("wake"), STATE_LIMIT_MS);
        }
        catch (e) {
            console.error("dismiss wake notice error", e);
        }
        actionSeq += 1;
        await refreshReportState();
    };
    if (fromHere) {
        // After a failure the notice comes back with its button, to try again.
        // Once the report is sent or saved, the notice has done its job.
        const done = fromHere.tone !== "error";
        return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(Banner, { variant: fromHere.tone, children: fromHere.text }), SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => (done ? void dismiss() : update({ outcome: null })), children: "Dismiss" }) }) })] }));
    }
    return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(Banner, { variant: "warning", children: wakeNoticeText(state.wake_failure_at) }), SP_JSX.jsxs(DFL.PanelSection, { children: [ready?.from === "wake" ? (SP_JSX.jsx(ReadyRows, { ready: ready, working: working })) : (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: working, onClick: () => void startReport("wake"), children: collecting ? "Collecting..." : working ? "Working..." : "Send report to the developer" }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => void dismiss(), children: "Dismiss" }) })] })] }));
}

const KOFI_URL = "https://ko-fi.com/fanciestpeanut";
function PanelFooter({ version, showDetails, onShowDetails }) {
    const [diagState, setDiagState] = SP_REACT.useState("idle");
    // Where the file went when the clipboard was not available. Kept until
    // the button is pressed again: it is the only place the path is said, and
    // five seconds is not long enough to note it down.
    const [savedPath, setSavedPath] = SP_REACT.useState("");
    // The timer that puts the button back to idle after a result. Cleared on
    // each press: one left from an earlier press set the button back to idle
    // in the middle of the next collection, which let a second one start.
    const idleTimer = SP_REACT.useRef(null);
    SP_REACT.useEffect(() => () => {
        if (idleTimer.current)
            clearTimeout(idleTimer.current);
    }, []);
    const backToIdle = () => {
        idleTimer.current = setTimeout(() => {
            idleTimer.current = null;
            setDiagState("idle");
        }, 5000);
    };
    const rowStyle = {
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
            const info = await withTimeout(getDiagnosticInfo());
            // A collection that failed comes back as a result, not a throw, and
            // copying it said "Copied to clipboard" over an error message.
            if (info?.success === false)
                throw new Error(String(info.error ?? "diagnostics failed"));
            const text = JSON.stringify(info, null, 2);
            try {
                await navigator.clipboard.writeText(text);
                setDiagState("done");
            }
            catch {
                // A resolved failure is still a failure: saying "Saved" here sent
                // people looking for a file that was never written.
                let saved = null;
                try {
                    saved = await withTimeout(saveDiagnosticInfo());
                }
                catch {
                    saved = null;
                }
                if (saved?.success === true) {
                    setDiagState("saved");
                    setSavedPath(typeof saved.path === "string" ? saved.path : "");
                }
                else {
                    setDiagState("save_failed");
                }
            }
            backToIdle();
        }
        catch {
            setDiagState("error");
            backToIdle();
        }
    };
    const label = diagState === "done"
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
    return (SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: rowStyle, children: ["v", version, " by jasonridesabike"] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: rowStyle, children: ["If WiFi won't reconnect, a reboot usually fixes it.", SP_JSX.jsx("br", {}), "Bugs? Report at github.com/ArcadaLabs-Jason/WifiOptimizer"] }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "Show details", description: "Opens every (i) explanation.", checked: showDetails, onChange: onShowDetails }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: diagState === "copying", onClick: handleCopyDiagnostics, children: label }) }), (diagState === "save_failed" || diagState === "error") && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { ...rowStyle, color: theme.error.text }, children: "Try again in a moment. If it keeps failing, say so in your report." }) })), savedPath && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { ...rowStyle, wordBreak: "break-all" }, children: ["The clipboard wasn't available, so the diagnostics were saved to this file: ", savedPath] }) })), SP_JSX.jsx(SendReportRow, {}), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => {
                        DFL.Navigation.CloseSideMenus();
                        DFL.Navigation.NavigateToExternalWeb(KOFI_URL);
                    }, children: "Support on Ko-fi" }) })] }));
}

function ActionsSection({ connected, isBusy, onForceReapply, onReset, resetError, resetNotice, resetNoticeInfo, workingText, reapplyResult, }) {
    return (SP_JSX.jsxs(DFL.PanelSection, { title: "Actions", children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: !connected || isBusy, onClick: onForceReapply, children: "Force reapply all" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy, onClick: onReset, children: "Reset settings" }) }), workingText && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.small, color: theme.info.text }, children: workingText }) })), !workingText && reapplyResult && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.small, color: theme[reapplyResult.variant].text }, children: reapplyResult.text }) })), !workingText && (resetError || resetNotice) && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                        fontSize: theme.fontSize.small,
                        color: resetError
                            ? theme.error.text
                            : resetNoticeInfo
                                ? theme.info.text
                                : theme.success.text,
                    }, children: resetError ?? resetNotice }) }))] }));
}

// Auto mode never keeps the driver options file (main.py set_power_save,
// persist=not auto, and _drop_persistent_power_save). These are the
// DRIVER_PROFILES keys with modprobe_options (regress.py ties the two):
// rtw88 and rtw89 (Realtek), mt7921e (MediaTek), iwlwifi (Intel). On rtw88
// the parameters are read at runtime and come off between streams (except
// the CLKREQ part of disable_aspm, set only when the driver loads), and on
// the Steam Deck LCD they are the ones that help WiFi come back after
// sleep. On the others they only take effect when the driver loads (C19).
const LOAD_TIME_DRIVERS = ["rtw88", "rtw89", "mt7921e", "iwlwifi"];
const SLEEP_HELP_DRIVER = "rtw88";
const SLEEP_HELP_AUTO_TEXT = " In auto mode, the driver settings that help WiFi come back after sleep are off between streams.";
const LOAD_TIME_AUTO_TEXT = " In auto mode, driver settings that only apply when the driver loads are not in effect.";
// What auto mode costs on this card's driver, if anything.
function autoModeDriverText(driver) {
    if (driver === SLEEP_HELP_DRIVER)
        return SLEEP_HELP_AUTO_TEXT;
    return LOAD_TIME_DRIVERS.includes(driver) ? LOAD_TIME_AUTO_TEXT : "";
}
// Streaming auto mode: when the fixes apply, rather than another fix. Placed
// after Advanced for that reason. Most of what is here is about making it
// obvious which state you are in, because fixes that quietly stopped
// applying are worse than fixes left on.
function StreamingSection({ settings, live, disabled, errors, notices, onToggle, }) {
    const enabled = settings?.streaming_mode_enabled ?? false;
    const [apps, setApps] = SP_REACT.useState([]);
    const [patterns, setPatterns] = SP_REACT.useState(settings?.streaming_custom_patterns ?? "");
    const [patternsDirty, setPatternsDirty] = SP_REACT.useState(false);
    SP_REACT.useEffect(() => {
        if (!enabled)
            return;
        listStreamingApps()
            .then((r) => r.success && setApps(r.apps))
            .catch(() => { });
    }, [enabled]);
    // Follow the saved value unless something is being typed.
    SP_REACT.useEffect(() => {
        if (!patternsDirty)
            setPatterns(settings?.streaming_custom_patterns ?? "");
    }, [settings?.streaming_custom_patterns, patternsDirty]);
    const toggleApp = async (id, watched) => {
        setApps((prev) => prev.map((a) => (a.id === id ? { ...a, watched } : a)));
        const result = await onToggle("streaming_apps", () => setStreamingApp(id, watched));
        if (!result?.success) {
            listStreamingApps().then((r) => r.success && setApps(r.apps)).catch(() => { });
        }
    };
    // Back to what is saved, and following it again.
    const cancelPatterns = () => {
        setPatternsDirty(false);
        setPatterns(settings?.streaming_custom_patterns ?? "");
    };
    const savePatterns = async () => {
        const result = await onToggle("streaming_patterns", () => setStreamingPatterns(patterns));
        if (result?.success) {
            setPatternsDirty(false);
            setPatterns(String(result.patterns ?? ""));
        }
    };
    const active = live?.streaming_active ?? false;
    const held = live?.streaming_held ?? false;
    const app = live?.streaming_app ?? "";
    const process = live?.streaming_process ?? "";
    // The fixes this mode governs that are turned on, by their names above.
    const governed = [
        settings?.power_save_disabled ? "Prevent lag spikes" : "",
        settings?.buffer_tuning_enabled ? "Network buffer tuning" : "",
        settings?.cake_enabled ? "Traffic shaping (CAKE)" : "",
    ].filter(Boolean);
    const governedList = governed.length > 1
        ? `${governed.slice(0, -1).join(", ")} and ${governed[governed.length - 1]}`
        : governed[0] ?? "";
    // With none of them on, a stream has nothing to turn on either.
    const fixesText = governed.length ? " Fixes are on." : " No fixes are turned on.";
    let stateText;
    if (active && app) {
        stateText = `Streaming: ${app} (${process}).${fixesText}`;
    }
    else if (active && held) {
        const until = "until you turn off Keep fixes on or restart the device.";
        stateText = governed.length ? `Held on by you. Fixes are on ${until}` : `Held on by you ${until}${fixesText}`;
    }
    else if (active) {
        // Flagged as running before the app has been named, just after a reload.
        stateText = `A stream is running.${fixesText}`;
    }
    else if (governed.length) {
        stateText = `No streaming app running. ${governedList} ${governed.length > 1 ? "are" : "is"} paused until one starts.`;
    }
    else {
        stateText = "No streaming app running. No fixes are turned on.";
    }
    return (SP_JSX.jsxs(DFL.PanelSection, { title: "Streaming auto mode", children: [SP_JSX.jsx(InfoRow, { label: "Only fix while streaming", subtitle: enabled
                    ? "Fixes apply while a streaming app runs"
                    : "Off: enabled fixes apply all the time", explanation: `Applies Prevent lag spikes, Network buffer tuning and Traffic shaping (CAKE) only while a streaming app is running, and takes them off afterwards to save battery. Background scanning, band, DNS and IPv6 stay as they are, because changing them reconnects WiFi and would drop the stream.${autoModeDriverText(settings?.driver ?? "")} If a stream is not picked up, turn on Keep fixes on, or add the app's process name below. Idea and first version by bassobr.`, checked: enabled, disabled: disabled, error: errors.streaming, notice: notices.streaming, onChange: (val) => onToggle("streaming", () => setStreamingMode(val)) }), enabled && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: {
                                fontSize: theme.fontSize.body,
                                color: active ? theme.success.text : theme.info.text,
                            }, children: stateText }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: "Keep fixes on", description: "For a stream that is not detected. Lasts until you restart the device.", checked: held, disabled: disabled, onChange: (val) => onToggle("streaming_hold", () => setStreamingHold(val)) }) }), SP_JSX.jsx(MessageRow, { error: errors.streaming_hold, notice: notices.streaming_hold }), apps.map((a) => (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ToggleField, { label: a.label, checked: a.watched, disabled: disabled, onChange: (val) => toggleApp(a.id, val) }) }, a.id))), SP_JSX.jsx(MessageRow, { error: errors.streaming_apps, notice: notices.streaming_apps }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.TextField, { label: "Other process names (space-separated)", value: patterns, onChange: (e) => {
                                setPatternsDirty(true);
                                setPatterns(e.target.value);
                            } }) }), patternsDirty && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: disabled, onClick: savePatterns, children: "Save process names" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: disabled, onClick: cancelPatterns, children: "Cancel" }) })] })), SP_JSX.jsx(MessageRow, { error: errors.streaming_patterns, notice: notices.streaming_patterns })] }))] }));
}

// Follows a running update and, once the new files are in place, asks Decky
// to reload this plugin, which is what Decky's own store does after an
// install. Restarting plugin_loader instead restarts all of Steam, so the
// update script only falls back to that when no reload happens.
//
// This lives at module level, not in the panel, because the panel cannot be
// relied on while the Quick Access menu is closed (the page is hidden then,
// and it may be unmounted) and the reload must still be asked for.
//
// It also keeps going in a copy of this module that Decky has dismounted.
// An open Quick Access menu can go on showing that copy's panel after a
// reload (and Decky issue #611 can start two copies), so the copy the user
// pressed Update in may well be one of those. Measured on a Deck: with the
// watch tied to the module's lifetime, nothing asked for the reload and the
// update ended in the Steam restart this exists to avoid.
const PLUGIN_NAME = "WiFi Optimizer";
const POLL_MS = 1000;
// The script's own limit is 15 minutes. A watch stops once it has gone this
// long with no new phase reported, counted in time awake: a sleep stops the
// script too, and a watch that counted the sleep stopped before the script
// got to "swapped", so nothing asked for the reload and Steam restarted.
const MAX_WATCH_MS = 15 * 60 * 1000;
// The longest gap between two polls counted as time awake. Anything longer
// was a sleep (or a stalled page), and counts as one poll.
const MAX_TICK_MS = 5 * POLL_MS;
// A "done" this recent, for the version now running, is the update that
// just loaded this copy of the plugin.
const JUST_UPDATED_S = 120;
const shared = () => {
    const w = window;
    w.__wifiOptimizerUpdate = w.__wifiOptimizerUpdate ?? {};
    return w.__wifiOptimizerUpdate;
};
let timer = null;
// Time awake since the phase last changed, and the last poll's time.
let idleMs = 0;
let lastTickAt = 0;
let lastPhase = "";
let inFlight = false;
let target = "";
let current = null;
const listeners = new Set();
function publish(progress) {
    current = progress;
    listeners.forEach((fn) => fn(progress));
}
function stop() {
    if (timer) {
        clearInterval(timer);
        timer = null;
    }
}
function askDeckyToReload(version) {
    // Once per update across every copy of the module, so two copies
    // following the same update cannot reload the plugin twice. Cleared when
    // the user starts an update: installing the same version again later in
    // the same Steam session skipped the reload, and Steam restarted.
    if (shared().reloadAsked === version)
        return;
    shared().reloadAsked = version;
    // Out of the poll's own call, since the reload tears this module down.
    // DeckyBackend is Decky's internal client, not part of @decky/api, so a
    // Decky without it is expected; the script restarts plugin_loader then.
    setTimeout(() => {
        const deckyBackend = window.DeckyBackend;
        if (typeof deckyBackend?.call !== "function") {
            console.warn("WiFi Optimizer: Decky cannot reload a plugin here, waiting for a restart");
            return;
        }
        Promise.resolve(deckyBackend.call("loader/reload_plugin", PLUGIN_NAME)).catch((e) => console.error("WiFi Optimizer: reload request failed, waiting for a restart", e));
    }, 0);
}
async function poll() {
    // Before the in-flight check: a progress call that never settles kept
    // that set, and the watch then never stopped.
    const now = Date.now();
    idleMs += Math.min(Math.max(0, now - lastTickAt), MAX_TICK_MS);
    lastTickAt = now;
    if (idleMs > MAX_WATCH_MS) {
        stop();
        return;
    }
    if (inFlight)
        return;
    inFlight = true;
    try {
        const progress = await getUpdateProgress();
        if (!progress.success)
            return;
        // Until the script has written anything, the file is either absent or
        // removed by apply_update, so an empty phase is just "starting".
        if (progress.phase && progress.version !== target)
            return;
        if (progress.phase !== lastPhase) {
            lastPhase = progress.phase;
            idleMs = 0;
        }
        publish(progress);
        // Only while the old backend is still the one answering: once the new
        // version answers, the reload has happened.
        if (progress.phase === "swapped" && progress.running_version !== target) {
            askDeckyToReload(target);
        }
        if (progress.phase === "failed" || progress.phase === "done") {
            stop();
            // Told once. Kept, it was replayed to every panel opened afterwards,
            // so a failed update kept saying so after a successful check.
            current = null;
        }
    }
    catch (e) {
        // A reload or restart cancels calls in flight; that is the update
        // working, and the next copy of the plugin takes over from here.
        console.debug("WiFi Optimizer: update progress unavailable", e);
    }
    finally {
        inFlight = false;
    }
}
// Called once apply_update has started the script for this version.
// fresh: a new update the user just started, rather than a copy of the
// module picking up one already running.
function watchUpdate(version, fresh = false) {
    // Both guards are once per version, so a version installed again later in
    // the same Steam session would get neither the reload nor the "Updated"
    // toast. A new update the user started clears them.
    if (fresh) {
        shared().reloadAsked = undefined;
        shared().announced = undefined;
    }
    stop();
    target = version;
    idleMs = 0;
    lastTickAt = Date.now();
    lastPhase = "";
    publish(null);
    timer = setInterval(poll, POLL_MS);
}
function subscribeUpdate(fn) {
    listeners.add(fn);
    fn(current);
    return () => {
        listeners.delete(fn);
    };
}
// On load: pick up an update that is running (started from another copy of
// the module, or before a reload), and say so if one just finished.
async function checkUpdateOnLoad() {
    try {
        const progress = await getUpdateProgress();
        if (!progress.success || !progress.phase)
            return;
        const running = progress.running_version;
        if (["downloading", "installing", "swapped"].includes(progress.phase) &&
            progress.version !== running) {
            if (!timer)
                watchUpdate(progress.version);
            return;
        }
        if ((progress.phase === "loaded" || progress.phase === "done") &&
            progress.version === running &&
            progress.age < JUST_UPDATED_S &&
            shared().announced !== progress.version) {
            shared().announced = progress.version;
            toaster.toast({ title: PLUGIN_NAME, body: `Updated to v${progress.version}` });
        }
    }
    catch {
        // Nothing to pick up or announce.
    }
}
// Telling people an update is out. The panel only checks while it is open,
// and Decky does not announce updates for a plugin installed outside its
// store, so without this an install nobody reopens never hears of one.
// Runs for as long as the plugin is loaded: a first check a couple of
// minutes after load, then twice a day. Each version is announced once,
// remembered by the backend across reboots, and never while a game or a
// stream is running, since a toast there is exactly the interruption this
// plugin exists to avoid. Nothing is installed: the toast opens the panel.
const NOTICE_FIRST_MS = 2 * 60 * 1000;
const NOTICE_EVERY_MS = 12 * 60 * 60 * 1000;
const NOTICE_RETRY_MS = 60 * 60 * 1000;
const NOTICE_BUSY_MS = 10 * 60 * 1000;
function gameRunning() {
    try {
        return !!DFL.Router.MainRunningApp;
    }
    catch {
        return false;
    }
}
function openPanel() {
    try {
        DFL.Navigation.OpenQuickAccessMenu(DFL.QuickAccessTab.Decky);
        // Decky's internal state, not part of @decky/api: without it the menu
        // still opens, on whichever plugin was last shown.
        const loader = window.DeckyPluginLoader;
        loader?.deckyState?.setActivePlugin?.(PLUGIN_NAME);
    }
    catch (e) {
        console.error("WiFi Optimizer: could not open the panel", e);
    }
}
// Returns the delay before the next check.
async function noticeOnce() {
    try {
        const notice = await updateNotice();
        if (notice.retry)
            return NOTICE_RETRY_MS;
        if (!notice.notify || !notice.version)
            return NOTICE_EVERY_MS;
        if (notice.busy || gameRunning())
            return NOTICE_BUSY_MS;
        // A second copy of the module (Decky issue #611) must not toast too.
        if (shared().noticeShown === notice.version)
            return NOTICE_EVERY_MS;
        shared().noticeShown = notice.version;
        // Steam shows a toast's body on one line and cuts the rest, so it is
        // kept short; tapping it opens the panel, where the update button is.
        toaster.toast({
            title: PLUGIN_NAME,
            body: `Update available: v${notice.version}`,
            onClick: openPanel,
        });
        await markUpdateNotified(notice.version);
        return NOTICE_EVERY_MS;
    }
    catch {
        return NOTICE_RETRY_MS;
    }
}
// Starts the checks for this copy of the module; returns how to stop them.
function startUpdateNotices() {
    let stopped = false;
    let next = null;
    const schedule = (ms) => {
        next = setTimeout(async () => {
            next = null;
            const delay = await noticeOnce();
            if (!stopped)
                schedule(delay);
        }, ms);
    };
    schedule(NOTICE_FIRST_MS);
    return () => {
        stopped = true;
        if (next)
            clearTimeout(next);
    };
}
// When Decky dismounts this copy. Nothing is stopped: a watch in progress
// stops by itself (see the top of this file), and a panel still on screen
// keeps its subscription until it unmounts, so it hears how the update ended
// instead of waiting out its timeout and calling a finished update failed.
function disposeUpdateWatch() {
    if (!timer)
        listeners.clear();
}

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
// Shown on the lock and band toggles while the iwd backend is in use:
// NetworkManager can't reconnect a network pinned this way under iwd. "Paused"
// only for a setting that is on; one that is off says what it needs.
const IWD_PIN_NOTICE = "Paused under iwd. Switch to wpa_supplicant in Advanced to use it.";
const IWD_OFF_NOTICE = "Needs wpa_supplicant. Switch to it in Advanced.";
const BACKEND_POLL_MAX_FAILURES = 5;
// The lock moves by itself after a wake only on drivers whose names start
// with this, the Steam Deck OLED's ath11k, the one card it was measured on
// (C25, the same test as the backend's).
const WAKE_MOVE_DRIVER = "ath11k";
// How long the backend keeps an after-wake move's outcome at most (C22).
const WAKE_MOVE_KEPT_S = 12 * 60 * 60;
// The lock row's line for an after-wake move's outcome, once its banner is
// gone (C22).
function wakeMoveRowText(outcome, at) {
    const ago = timeAgo(at);
    if (outcome === "moved")
        return `Moved to a stronger access point after sleep, ${ago}.`;
    if (outcome === "back")
        return `Stayed after sleep: the stronger access point refused, ${ago}.`;
    if (outcome === "off")
        return `Couldn't move to a stronger access point after sleep, ${ago}.`;
    return undefined;
}
// An iw stuck in the driver, or a card the system can no longer use. Copy
// diagnostics first: after the restart they show the healthy boot's link and
// NetworkManager state, not the failed one's.
const NOT_RESPONDING_TEXT = "The WiFi card isn't responding. Copy diagnostics (bottom of this panel), then restart the device.";
// What Reset undoes, said before it happens. It reaches every network the
// plugin touched, so one stray press of the button should not be enough.
// Auto-fix on wake stays on, as on a fresh install (main.py _reset_locked),
// with nothing left for it to apply. The network settings are written to
// the saved networks without a reconnect, so the connection in use keeps
// them until WiFi next connects. The update channel and notices are kept,
// and streaming auto mode starts again from its defaults. Allow WPA2 is only
// recorded from 0.24.5-beta on (wpa2_uuids); that, the buffer and region
// values and the command for the rest are in the README, not here.
const RESET_CONFIRM_TEXT = "Turns off every fix except Auto-fix on wake, which then applies nothing. The lock, band, DNS, IPv6 and Allow WPA2 come off your networks at the next connect. Streaming auto mode is cleared. Update settings are kept.";
// Move the lock often finds nothing clearly stronger and stays put, with no
// reconnect, so its button does not promise one.
const MOVE_WORKING_TEXT = "Working, WiFi may reconnect...";
// Reset does not reconnect, so it does not say WiFi will.
const RESET_WORKING_TEXT = "Working...";
const RESET_DONE_TEXT = "Done. Settings are reset.";
// Only where the plugin can choose the backend itself. On SteamOS that is
// Valve's setting, and Reset leaves it alone.
const RESET_BACKEND_TEXT = " A WiFi backend chosen here goes back to the system default from the next boot.";
// The refresh after a change is bounded too, for the same reason.
const REFRESH_WAIT_MS = 15 * 1000;
const NO_ANSWER_TEXT = "No answer after 2 minutes. The change may still finish, so check this setting again in a moment. If WiFi doesn't come back, restart the device.";
// The device, not Steam: the backend runs in Decky's own service, which a
// Steam restart is not known to restart.
const UNREACHABLE_TEXT = "Couldn't reach the plugin's backend. Wait a moment and try again. If it keeps happening, restart the device.";
// Optimize Safe, Fix now and Force Reapply reconnect only when the lock or a
// network setting is among what they apply.
const BATCH_WORKING_TEXT = "Working. WiFi may reconnect, and this can take up to a minute.";
// For a failure that came with no message of its own.
const GENERIC_FAIL_TEXT = "That didn't work. Try again. If it keeps failing, use Copy diagnostics at the bottom of this panel and report it.";
// A setting that is on, but for another network than this one.
const OTHER_NETWORK_TEXT = "On for another network, so it does nothing here. To use it here, turn it off, then on again.";
// Drift keys, by the name the panel shows for the setting.
const DRIFT_LABELS = {
    power_save: "Prevent lag spikes",
    bssid_lock: "Stop background scanning",
    buffer_tuning: "Network buffer tuning",
    band_preference: "Band preference",
    dns: "Custom DNS",
    ipv6: "Disable IPv6",
    cake: "Traffic shaping (CAKE)",
};
// "A", "A and B", "A, B and C".
function joinNames(names) {
    return names.length > 1
        ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`
        : names[0] ?? "";
}
// What a failed result says, without the raw command output some carry in
// "detail". That text is for the log, and read as noise in the panel.
function failureText(result) {
    if (result.detail)
        console.warn("WiFi Optimizer: failure detail", result.detail);
    return result.message ?? ERROR_MESSAGES[result.error ?? ""] ?? GENERIC_FAIL_TEXT;
}
function isResult(value) {
    return !!value && typeof value.success === "boolean";
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
const UPDATE_FAILED_TEXT = "The update didn't finish. Try again. If it fails again, restart the device.";
const UPDATE_SLOW_TEXT = "The update is taking too long. Restart the device, then check the version at the bottom of this panel. If it didn't change, try the update again.";
// The update call itself failed (Decky restarting, say) and the script
// reports nothing: no update is running, so the panel says so at once
// instead of "Starting the update..." for UPDATE_TIMEOUT_MS.
const UPDATE_NOT_STARTED_TEXT = "Couldn't start the update. Try again in a moment.";
// The phases of a script that is still running (UPDATE_ACTIVE_PHASES in
// main.py).
const UPDATE_RUNNING_PHASES = ["downloading", "installing", "swapped", "loaded", "restarting"];
// The script writes its first phase within moments of starting, so this
// long after a failed call an update that did start has said so.
const UPDATE_START_LOOK_MS = 3000;
const UPDATE_LOOK_TIMEOUT_MS = 20 * 1000;
// After a failed apply_update call: whether the update script started for
// this version anyway. One bounded look at what it reports. Anything that
// cannot be read counts as started, so the watch follows it as before.
async function updateStarted(version) {
    await new Promise((r) => setTimeout(r, UPDATE_START_LOOK_MS));
    try {
        const progress = await withTimeout(getUpdateProgress(), UPDATE_LOOK_TIMEOUT_MS);
        if (!progress?.success)
            return true;
        if (UPDATE_RUNNING_PHASES.includes(progress.phase))
            return true;
        // "done" or "failed" for this version is this update's own outcome;
        // the watch shows it. Nothing at all, or another version's, is none.
        return !!progress.phase && progress.version === version;
    }
    catch {
        return true;
    }
}
// Long enough for the reloaded backend to have answered the update script,
// so a finished update is announced rather than mistaken for a running one.
const ANNOUNCE_DELAY_MS = 4000;
// What the panel says while an update runs, from what the script reports.
function updatingText(progress) {
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
// What Optimize Safe, Fix now or Force reapply came to, in one line. where:
// on which side of the settings it is shown, for the failed ones it points
// at. Only Optimize Safe saves anything, so only it says "Saved." for fixes
// streaming auto mode holds back.
function batchResultView(result, from, where) {
    // A failure has no counts at all, and undefined === undefined made it
    // read as "All applied".
    if (result.success === false) {
        return { variant: "error", text: result.message ?? GENERIC_FAIL_TEXT };
    }
    if (!result.total) {
        return { variant: "info", text: result.message ?? "Nothing to apply." };
    }
    const all = result.applied === result.total;
    // In streaming auto mode with no stream running, these were saved but
    // not applied, so "All applied" was not true.
    const paused = Object.entries(result.results ?? {})
        .filter(([, v]) => isResult(v) && v.success && v.paused)
        .map(([k]) => DRIFT_LABELS[k] ?? k);
    const pausedText = paused.length
        ? `${joinNames(paused)} ${paused.length > 1 ? "apply" : "applies"} only while a streaming app is running.`
        : "";
    if (all && paused.length) {
        return { variant: "info", text: from === "optimize" ? `Saved. ${pausedText}` : pausedText };
    }
    return all
        ? { variant: "success", text: "All applied" }
        : {
            variant: "warning",
            text: `${result.applied} of ${result.total} applied. The settings marked failed ${where} say why.${pausedText ? " " + pausedText : ""}`,
        };
}
function getBadge(driftKey, status, errorKey) {
    // Only surface a badge when it tells the user something the toggle position
    // doesn't - failure or drift between our setting and the system state.
    // Binary on/off badges are redundant with the toggle itself and are hidden.
    if (errorKey)
        return { badge: "error", text: "failed" };
    if (driftKey && status?.drift?.[driftKey])
        return { badge: "drifted", text: "drifted" };
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
// A region's name as the selector shows it, without the code: "Germany".
function regionName(code) {
    const option = REGION_OPTIONS.find((o) => o.data === code);
    return option ? option.label.replace(/ \(\w\w\).*$/, "") : code;
}
// The region the card uses, in words. The kernel reports three codes that
// are not countries: 00 the world default, 98 a mix of the card's own
// region and the one asked for, 99 the card's own.
function governingName(code, asked) {
    if (code === "00")
        return "the world default";
    // With nothing asked for here, the other half is the system's own choice.
    if (code === "98")
        return asked ? `a mix of its own region and ${regionName(asked)}` : "a mix of its own region and the system's";
    if (code === "99")
        return "its own region";
    return regionName(code);
}
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
class ErrorBoundary extends SP_REACT.Component {
    constructor() {
        super(...arguments);
        this.state = { err: null };
    }
    static getDerivedStateFromError(err) {
        return { err };
    }
    componentDidCatch(err, info) {
        console.error("WiFi Optimizer render error:", err, info);
    }
    render() {
        if (this.state.err) {
            // A retry rather than "close and reopen": the Quick Access Menu keeps
            // the panel mounted across close and open, so reopening alone would
            // show this same message again.
            return (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(Banner, { variant: "error", children: "WiFi Optimizer hit an unexpected error. If it keeps happening, please report at github.com/ArcadaLabs-Jason/WifiOptimizer." }), SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: () => this.setState({ err: null }), children: "Try again" }) }) })] }));
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
    const [status, setStatus] = SP_REACT.useState(null);
    const [errors, setErrors] = SP_REACT.useState({});
    const [notices, setNotices] = SP_REACT.useState({});
    const [busyState, setIsBusy] = SP_REACT.useState(false);
    // The key of the change running now, for the text on its row.
    const [working, setWorking] = SP_REACT.useState(null);
    const [applyingAll, setApplyingAll] = SP_REACT.useState(false);
    // Which button started the batch running now, so its working text shows
    // under that button only.
    const [batchFrom, setBatchFrom] = SP_REACT.useState(null);
    // The last status read failed, and when the first one is slow to come.
    const [statusFailed, setStatusFailed] = SP_REACT.useState(false);
    const [slowLoad, setSlowLoad] = SP_REACT.useState(false);
    const [showDetails, setShowDetails] = SP_REACT.useState(detailsShown);
    const [dupDismissed, setDupDismissed] = SP_REACT.useState(duplicatesDismissed);
    const [dismissed, setDismissed] = SP_REACT.useState(findingsDismissed);
    const [optimizeResult, setOptimizeResult] = SP_REACT.useState(null);
    // Which button the result above is for. Force reapply's is also said under
    // its own button, where focus is; "Saved." only for Optimize Safe, the one
    // that saves anything.
    const [resultFrom, setResultFrom] = SP_REACT.useState(null);
    const [regdomain, setRegdomainInfo] = SP_REACT.useState(null);
    // The wireless region is a device-level fact that does not change on its
    // own, so it is read once rather than added to the status poll.
    const refreshRegdomain = SP_REACT.useCallback(() => {
        getRegdomain()
            .then((info) => setRegdomainInfo(info))
            .catch(() => setRegdomainInfo(null));
    }, []);
    SP_REACT.useEffect(() => {
        refreshRegdomain();
    }, [refreshRegdomain]);
    const [customDnsInput, setCustomDnsInput] = SP_REACT.useState("");
    // Custom DNS is edited as a draft and applied with a button. Choosing
    // Custom used to send the empty field straight to the backend, which
    // refused it, so the field it gates never appeared and nobody starting
    // from the defaults could ever enter servers. The dirty flag stops the
    // status poll replacing what is being typed, which on the on-screen
    // keyboard takes longer than one poll.
    const [dnsCustomOpen, setDnsCustomOpen] = SP_REACT.useState(false);
    const dnsDirtyRef = SP_REACT.useRef(false);
    // --- Update flow state ---
    const [updateInfo, setUpdateInfo] = SP_REACT.useState(null);
    const [checkingUpdate, setCheckingUpdate] = SP_REACT.useState(false);
    const [updating, setUpdating] = SP_REACT.useState(false);
    // Why an update could not start or did not finish. Shown under the update
    // buttons, and expires like the other messages.
    const [updateError, setUpdateError] = SP_REACT.useState(null);
    // A failed change to the channel or the notice toggle. Kept apart, so it
    // is shown beside those controls rather than under the update button.
    const [updateSettingError, setUpdateSettingError] = SP_REACT.useState(null);
    const [updateProgress, setUpdateProgress] = SP_REACT.useState(null);
    // --- Backend switch state ---
    const [backendSwitch, setBackendSwitch] = SP_REACT.useState(null);
    // --- Refs ---
    // busyRef is the synchronous re-entrancy guard; busyState (above) is the
    // React state mirror so UI can respond. setBusy below writes both together.
    const busyRef = SP_REACT.useRef(false);
    const backendPollRef = SP_REACT.useRef(null);
    const prevConnectedRef = SP_REACT.useRef(null);
    const lastUpdateCheckAtRef = SP_REACT.useRef(0);
    // Bumped when the update channel changes, so a check that was already
    // running for the old channel cannot put its answer back up.
    const updateCheckSeqRef = SP_REACT.useRef(0);
    const statusSeqRef = SP_REACT.useRef(0);
    const setBusy = SP_REACT.useCallback((val) => {
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
    const runUpdateCheck = SP_REACT.useCallback((force = false, keepError = false) => {
        const now = Date.now();
        if (!force && now - lastUpdateCheckAtRef.current < UPDATE_CHECK_DEDUPE_MS) {
            return;
        }
        lastUpdateCheckAtRef.current = now;
        const seq = updateCheckSeqRef.current;
        checkForUpdate()
            .then((info) => {
            if (seq !== updateCheckSeqRef.current)
                return;
            setUpdateInfo(info);
            if (info?.success && !keepError)
                setUpdateError(null);
        })
            .catch(() => { });
    }, []);
    const refreshStatus = SP_REACT.useCallback(async (force = false) => {
        // Background interval ticks defer while a user operation is in flight;
        // handler-driven refreshes (at the end of an op) force through so the UI
        // catches up immediately instead of waiting for the next interval tick.
        if (!force && busyRef.current)
            return;
        // Status collection runs off the event loop on the backend, so a slow
        // call can still be in flight when the next tick fires. Without a
        // sequence check the older reply lands last and overwrites the newer
        // one, which shows up as toggles and stats flicking back to stale
        // values a second after they settled.
        const seq = ++statusSeqRef.current;
        try {
            const s = await getStatus();
            if (seq !== statusSeqRef.current)
                return;
            setStatusFailed(false);
            setStatus(s);
            if (s.settings) {
                // Only adopt a stored value that exists. Older backends cleared
                // dns_servers when DNS was turned off, and copying that empty string
                // back over the field every few seconds made the setting impossible
                // to re-enable: the servers field is only rendered while DNS is on,
                // and turning it on with an empty custom list is rejected.
                if (!dnsDirtyRef.current &&
                    s.settings.dns_provider === "custom" &&
                    s.settings.dns_servers) {
                    setCustomDnsInput(s.settings.dns_servers);
                }
            }
        }
        catch (e) {
            console.error("WiFi Optimizer: failed to get status", e);
            if (seq === statusSeqRef.current)
                setStatusFailed(true);
        }
    }, []);
    // The refresh at the end of a change, bounded so a backend that stopped
    // answering cannot keep the panel busy.
    const refreshAfterChange = SP_REACT.useCallback(() => withTimeout(refreshStatus(true), REFRESH_WAIT_MS).catch(() => { }), [refreshStatus]);
    const stopBackendPoll = () => {
        if (backendPollRef.current) {
            clearInterval(backendPollRef.current);
            backendPollRef.current = null;
        }
    };
    const beginBackendPoll = SP_REACT.useCallback(() => {
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
        const giveUp = async (message) => {
            done = true;
            stopBackendPoll();
            setBackendSwitch(null);
            setErrors((prev) => ({ ...prev, wifi_backend: message }));
            await refreshAfterChange();
            setBusy(false);
        };
        const tooLong = "The backend switch is taking too long. Restart the device if WiFi does not come back.";
        backendPollRef.current = setInterval(async () => {
            if (done)
                return;
            // Checked before the in-flight guard: a status call that never
            // settles kept the guard set, and with it every control disabled.
            if (Date.now() - pollStartedAt > BACKEND_SWITCH_MAX_S * 1000) {
                await giveUp(tooLong);
                return;
            }
            if (inFlight)
                return;
            inFlight = true;
            try {
                const s = await withTimeout(getBackendSwitchStatus(), BACKEND_POLL_TIMEOUT_MS);
                if (done)
                    return;
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
                    if (s.result.detail)
                        console.warn("WiFi Optimizer: backend switch detail", s.result.detail);
                    setErrors((prev) => ({
                        ...prev,
                        wifi_backend: backendResultText(s.result),
                    }));
                }
                await refreshAfterChange();
                setBackendSwitch(s);
                setBusy(false);
            }
            catch (e) {
                if (done)
                    return;
                console.error("backend switch poll error", e);
                // One failed call is not the switch failing; the worker carries on
                // regardless. Only after several in a row is it lost.
                failures += 1;
                if (failures >= BACKEND_POLL_MAX_FAILURES) {
                    await giveUp("Lost track of the backend switch. Check WiFi, then try again.");
                }
            }
            finally {
                inFlight = false;
            }
        }, BACKEND_POLL_INTERVAL);
    }, [refreshAfterChange, setBusy]);
    // Main refresh interval, paused when the panel is hidden so we don't burn
    // CPU/battery on ~12 subprocess calls every 3s in the background. On return
    // to visible, run one refresh immediately to catch anything that changed
    // while hidden, then resume the interval.
    SP_REACT.useEffect(() => {
        let id = null;
        const start = () => {
            if (id)
                return;
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
            if (document.hidden)
                stop();
            else
                start();
        };
        if (!document.hidden)
            start();
        document.addEventListener("visibilitychange", onVis);
        return () => {
            document.removeEventListener("visibilitychange", onVis);
            stop();
        };
    }, [refreshStatus]);
    // Say something while the first status is slow to come, rather than show
    // an empty section with no sign that anything is happening.
    const loaded = !!status && !status.initializing;
    SP_REACT.useEffect(() => {
        if (loaded)
            return;
        const id = setTimeout(() => setSlowLoad(true), 10 * 1000);
        return () => clearTimeout(id);
    }, [loaded]);
    // A dismissed duplicate notice comes back the next time duplicates are
    // found, not while the same ones are still reported.
    const hasDuplicates = !!status?.external?.duplicate_profiles;
    SP_REACT.useEffect(() => {
        if (loaded && !hasDuplicates) {
            duplicatesDismissed = false;
            setDupDismissed(false);
        }
    }, [loaded, hasDuplicates]);
    // The same for the WPA2 and weaker-lock notices: once a dismissed finding
    // is no longer reported, the next one is shown again.
    const hasWpa2Finding = !!status?.wpa2_only_stronger;
    const hasLockWeaker = !!status?.lock_weaker;
    SP_REACT.useEffect(() => {
        if (!loaded)
            return;
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
    SP_REACT.useEffect(() => {
        // Initial update check. If it fails (e.g., no network yet), the effect
        // below retries on connectivity recovery. QAM tends to cache the panel
        // across close/open, so we can't rely on remount to retry.
        runUpdateCheck();
        // Resume backend-switch polling if one is in flight (panel was reopened mid-switch)
        getBackendSwitchStatus()
            .then((s) => {
            if (s.in_progress) {
                setBackendSwitch(s);
                setBusy(true);
                beginBackendPoll();
            }
        })
            .catch(() => { });
        return () => {
            if (backendPollRef.current)
                clearInterval(backendPollRef.current);
        };
    }, [beginBackendPoll, runUpdateCheck, setBusy]);
    // Retry update check when connectivity recovers. The initial one-shot check
    // in the mount effect misses the case where the panel was already open when
    // the network came back. Skip until status has loaded to avoid a spurious
    // null-to-true transition firing an extra check on every mount.
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
    // Follow the update script. The watch itself is module level, so a panel
    // closed and reopened mid-update picks the progress back up from here.
    SP_REACT.useEffect(() => subscribeUpdate((progress) => {
        setUpdateProgress(progress);
        if (progress?.phase === "failed") {
            setUpdating(false);
            setUpdateError(UPDATE_FAILED_TEXT);
        }
        else if (progress?.phase === "done") {
            // Seen by a panel left open across the reload; the new copy of the
            // plugin is the one running now. Its offer to update is stale too,
            // and kept showing "Update to" the version just installed.
            setUpdating(false);
            setUpdateInfo(null);
            runUpdateCheck(true);
        }
        else if (progress) {
            setUpdating(true);
        }
    }), [runUpdateCheck]);
    // Safety timeout: if an update was initiated but the script has reported
    // nothing new for as long as it allows itself, it probably failed (network
    // drop, tarball corruption, etc.). Revert the UI so the user isn't stuck staring
    // at "Updating..." forever, and surface a message they can act on. Started
    // again at each new phase, as the watch in updateWatch.ts is.
    const updatePhase = updateProgress?.phase ?? "";
    SP_REACT.useEffect(() => {
        if (!updating)
            return;
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
    SP_REACT.useEffect(() => {
        if (!updateError)
            return;
        const shown = updateError;
        const id = setTimeout(() => setUpdateError((prev) => (prev === shown ? null : prev)), MESSAGE_TTL_MS);
        return () => clearTimeout(id);
    }, [updateError]);
    SP_REACT.useEffect(() => {
        if (!updateSettingError)
            return;
        const shown = updateSettingError;
        const id = setTimeout(() => setUpdateSettingError((prev) => (prev === shown ? null : prev)), MESSAGE_TTL_MS);
        return () => clearTimeout(id);
    }, [updateSettingError]);
    SP_REACT.useEffect(() => {
        if (!optimizeResult)
            return;
        const shown = optimizeResult;
        const id = setTimeout(() => setOptimizeResult((prev) => (prev === shown ? null : prev)), MESSAGE_TTL_MS);
        return () => clearTimeout(id);
    }, [optimizeResult]);
    // Periodic update re-check - QAM often caches the panel across close/reopen,
    // so the mount-effect check doesn't re-fire. This heartbeat catches new
    // releases when the panel has been left open for a while. Paused when the
    // panel/tab is hidden to avoid pointless GitHub calls accumulating while the
    // user isn't looking; re-fires one check immediately on visibility return.
    SP_REACT.useEffect(() => {
        let id = null;
        const start = () => {
            if (id)
                return;
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
            }
            else {
                runUpdateCheck();
                start();
            }
        };
        if (!document.hidden)
            start();
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
    SP_REACT.useEffect(() => {
        const r = backendSwitch?.result;
        if (!backendSwitch || backendSwitch.in_progress || !r || r.needs_reboot)
            return;
        const shown = backendSwitch;
        const id = setTimeout(() => setBackendSwitch((prev) => (prev === shown ? null : prev)), BACKEND_RESULT_MS);
        return () => clearTimeout(id);
    }, [backendSwitch]);
    // Expire each error and notice MESSAGE_TTL_MS after it first appeared. A
    // message set again with the same text keeps its time; one that changed,
    // or went and came back, starts over.
    const messageSeenRef = SP_REACT.useRef({});
    SP_REACT.useEffect(() => {
        const now = Date.now();
        const seen = {};
        const shown = [
            ...Object.entries(errors).map(([k, v]) => [`e:${k}`, v]),
            ...Object.entries(notices).map(([k, v]) => [`n:${k}`, v]),
        ];
        for (const [key, text] of shown) {
            const before = messageSeenRef.current[key];
            seen[key] = before && before.text === text ? before : { text, at: now };
        }
        messageSeenRef.current = seen;
        const times = Object.values(seen).map((m) => m.at);
        if (!times.length)
            return;
        const id = setTimeout(() => {
            // A second of slack, so a timer that fires a little early still
            // clears the message it was set for.
            const due = Date.now() + 1000 - MESSAGE_TTL_MS;
            const expire = (prefix) => (prev) => {
                const stale = Object.keys(prev).filter((k) => {
                    const m = messageSeenRef.current[prefix + k];
                    return m && m.text === prev[k] && m.at <= due;
                });
                if (!stale.length)
                    return prev;
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
    const handleToggle = async (key, fn) => {
        if (busyRef.current)
            return undefined;
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
        let outcome;
        try {
            const answer = await withTimeout(fn());
            // A reply without a success flag is not a result the panel can read.
            const result = isResult(answer)
                ? answer
                : { success: false, message: GENERIC_FAIL_TEXT };
            outcome = result;
            // A setter can succeed and still have done something other than what
            // was asked - locking to the access point you were already on because
            // the stronger one would not take, say. Saying nothing there leaves the
            // user with a toggle that flipped and no idea why the outcome differs.
            if (result.success && result.message) {
                setNotices((prev) => ({ ...prev, [key]: result.message }));
            }
            if (!result.success) {
                const msg = failureText(result);
                setErrors((prev) => ({ ...prev, [key]: msg }));
            }
            // Wait for WiFi to reconnect before refreshing status
            if (result.reconnected) {
                await new Promise((r) => setTimeout(r, RECONNECT_DELAY));
            }
        }
        catch (e) {
            // A call that throws (Decky restarting, the backend gone) used to
            // surface as an unhandled rejection with the toggle silently unchanged.
            console.error(`WiFi Optimizer: ${key} call failed`, e);
            setErrors((prev) => ({
                ...prev,
                [key]: e instanceof NoAnswer ? NO_ANSWER_TEXT : UNREACHABLE_TEXT,
            }));
        }
        finally {
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
    const runBatch = async (fn, from) => {
        if (busyRef.current)
            return;
        setBusy(true);
        setApplyingAll(true);
        setBatchFrom(from);
        setErrors({});
        setNotices({});
        setOptimizeResult(null);
        try {
            const answer = await withTimeout(fn());
            const result = isResult(answer)
                ? answer
                : { success: false, total: 0, applied: 0, results: {}, message: GENERIC_FAIL_TEXT };
            setResultFrom(from);
            setOptimizeResult(result);
            if (result.results) {
                // Successes can carry a message too, as they do for a single toggle:
                // a lock that stayed on the access point you were already on, or a
                // fix kept for the next stream. Dropping those here hid the reason.
                const newErrors = {};
                const newNotices = {};
                for (const [k, v] of Object.entries(result.results)) {
                    if (!isResult(v)) {
                        newErrors[k] = GENERIC_FAIL_TEXT;
                    }
                    else if (!v.success) {
                        newErrors[k] = failureText(v);
                    }
                    else if (v.message) {
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
        }
        catch (e) {
            console.error("optimize error", e);
            setResultFrom(from);
            setOptimizeResult({
                success: false, total: 0, applied: 0, results: {},
                message: e instanceof NoAnswer ? NO_ANSWER_TEXT : UNREACHABLE_TEXT,
            });
        }
        finally {
            // Refresh before clearing applyingAll so button doesn't briefly flip to
            // "All good" from stale status. Refresh runs on errors too.
            await refreshAfterChange();
            setBusy(false);
            setApplyingAll(false);
            setBatchFrom(null);
        }
    };
    const handleOptimize = () => runBatch(() => optimizeSafe(), "optimize");
    const handleRepairDrift = () => runBatch(() => repairDrifted(), "fix");
    const handleForceReapply = () => runBatch(() => reapplyAll(), "reapply");
    const handleAllowWpa2 = () => handleToggle("allow_wpa2", () => allowWpa2());
    const handleMoveLock = () => handleToggle("move_bssid_lock", () => moveBssidLock());
    const dismissError = (key) => setErrors((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
    });
    // Dismiss hides the notice and any failed attempt's message with it. A
    // finding comes back only once it has cleared and is reported again. With
    // the finding already cleared (the banner up only for a failed Move or
    // Allow WPA2), only the message goes: a flag set then would never be
    // cleared, since the effect above sees no change, and would hide the next
    // finding.
    const dismissFinding = (key, errorKey) => {
        dismissError(errorKey);
        const reported = key === "lockWeaker" ? hasLockWeaker : hasWpa2Finding;
        if (!reported)
            return;
        findingsDismissed = { ...findingsDismissed, [key]: true };
        setDismissed(findingsDismissed);
    };
    const handleDismissLockWeaker = () => dismissFinding("lockWeaker", "move_bssid_lock");
    const handleDismissWpa2 = () => dismissFinding("wpa2", "allow_wpa2");
    const handleDismissDuplicates = () => {
        duplicatesDismissed = true;
        setDupDismissed(true);
    };
    const handleShowDetails = (on) => {
        detailsShown = on;
        setShowDetails(on);
    };
    // Reset asks first: see RESET_CONFIRM_TEXT.
    const handleResetSettings = () => {
        if (busyRef.current)
            return;
        const distro = status?.settings?.distro_id ?? "";
        const description = RESET_CONFIRM_TEXT + (distro && distro !== "steamos" ? RESET_BACKEND_TEXT : "");
        DFL.showModal(SP_JSX.jsx(DFL.ConfirmModal, { strTitle: "Reset WiFi Optimizer?", strDescription: description, strOKButtonText: "Reset", strCancelButtonText: "Cancel", bDestructiveWarning: true, onOK: () => void runReset() }));
    };
    const runReset = async () => {
        if (busyRef.current)
            return;
        setBusy(true);
        setWorking("reset");
        setOptimizeResult(null);
        setErrors({});
        setNotices({});
        resetDnsDraft();
        try {
            const result = await withTimeout(resetSettings());
            if (!isResult(result) || !result.success) {
                setErrors({ reset: isResult(result) ? failureText(result) : GENERIC_FAIL_TEXT });
            }
            else {
                // Networks Reset could not change yet are retried while the plugin
                // runs, so Reset worked, but "Done" alone would not be true.
                setNotices({
                    reset: result.pins_left && result.message ? result.message : RESET_DONE_TEXT,
                });
                // Reset keeps the update channel, but an older backend put it back
                // to Stable. Either way the offer on screen may be for a channel no
                // longer chosen, so it is dropped and asked for again.
                updateCheckSeqRef.current += 1;
                setUpdateInfo(null);
                runUpdateCheck(true);
            }
        }
        catch (e) {
            console.error("reset error", e);
            setErrors({ reset: e instanceof NoAnswer ? NO_ANSWER_TEXT : UNREACHABLE_TEXT });
        }
        finally {
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
            await acknowledgeSettingsRecovered();
        }
        catch (e) {
            console.error("acknowledge settings recovered error", e);
        }
        await refreshStatus(true);
    };
    // The switch takes WiFi down for up to a minute, so it asks first, as
    // Reset does.
    const handleBackendToggle = (on) => {
        if (busyRef.current)
            return;
        const target = on ? "wpa_supplicant" : "iwd";
        const paused = !on && (status?.settings?.bssid_lock_enabled || status?.settings?.band_preference_enabled)
            ? " Under iwd, Stop background scanning and the band preference are paused."
            : "";
        DFL.showModal(SP_JSX.jsx(DFL.ConfirmModal, { strTitle: "Switch the WiFi backend?", strDescription: `This switches to ${target}. WiFi disconnects while it switches, usually for less than a minute.${paused} If WiFi doesn't come back, switch back here or restart the device.`, strOKButtonText: "Switch", strCancelButtonText: "Cancel", onOK: () => void runBackendSwitch(target) }));
    };
    const runBackendSwitch = async (target) => {
        // Re-entrancy guard: drop rapid clicks or clicks that land while another
        // operation is already running. Protects against duplicate backend calls
        // and the Force-Reapply/backend-switch overlap case.
        if (busyRef.current)
            return;
        setBusy(true);
        setErrors((prev) => {
            const next = { ...prev };
            delete next.wifi_backend;
            return next;
        });
        setOptimizeResult(null);
        try {
            // Bounded: a call that never settled kept the panel busy for good.
            const res = await withTimeout(startBackendSwitch(target));
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
        }
        catch (e) {
            console.error("startBackendSwitch error", e);
            // The reply may be what was lost, with the switch running anyway.
            // Follow it then, rather than say it never started.
            try {
                const s = await withTimeout(getBackendSwitchStatus(), BACKEND_POLL_TIMEOUT_MS);
                if (s.in_progress) {
                    setBackendSwitch(s);
                    beginBackendPoll();
                    return;
                }
            }
            catch {
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
            const result = (await applyUpdate(updateInfo?.latest_version ?? ""));
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
            if (version)
                watchUpdate(version, true);
        }
        catch {
            // The script may well be running anyway. Follow it, so the plugin is
            // still reloaded when the files are in place rather than Steam
            // restarted. If it reports nothing, nothing started: say so now.
            const version = updateInfo?.latest_version ?? "";
            if (!(await updateStarted(version))) {
                setUpdating(false);
                setUpdateError(UPDATE_NOT_STARTED_TEXT);
                return;
            }
            if (version)
                watchUpdate(version, true);
        }
    };
    const handleCheckForUpdate = async () => {
        setCheckingUpdate(true);
        setUpdateError(null);
        lastUpdateCheckAtRef.current = Date.now();
        const seq = updateCheckSeqRef.current;
        try {
            // Bounded, or "Checking..." could stay up with the button disabled.
            const result = await withTimeout(checkForUpdate());
            if (seq === updateCheckSeqRef.current)
                setUpdateInfo(result);
        }
        catch {
            // refreshStatus has its own catch; no need to log here
        }
        setCheckingUpdate(false);
    };
    const handleNotifyChange = async (enabled) => {
        try {
            const result = await withTimeout(setUpdateNotify(enabled), REFRESH_WAIT_MS);
            if (!result.success) {
                setUpdateSettingError(result.message ?? "Couldn't change the update notice setting. Try again.");
                return;
            }
            setUpdateSettingError(null);
        }
        catch (e) {
            console.error("set update notify error", e);
            setUpdateSettingError("Couldn't change the update notice setting. Try again.");
        }
        await refreshAfterChange();
    };
    const handleChannelChange = async (nextChannel) => {
        try {
            const result = await withTimeout(setUpdateChannel(nextChannel), REFRESH_WAIT_MS);
            if (!result.success) {
                setUpdateSettingError(result.message ?? "Couldn't change the update channel. Try again.");
                return;
            }
            updateCheckSeqRef.current += 1;
            setUpdateSettingError(null);
            setUpdateError(null);
            setUpdateInfo(null);
        }
        catch (e) {
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
        setCustomDnsInput(status?.settings?.dns_provider === "custom" ? status.settings.dns_servers : "");
    };
    const applyCustomDns = async () => {
        const result = await handleToggle("dns", async () => {
            const r = await setDns(true, "custom", customDnsInput.trim());
            // Before the refresh that follows, so it adopts the servers as the
            // backend saved them. Cleared after it, Apply and Cancel stayed up
            // until the next poll whenever the saved form differed from the typed.
            if (r.success)
                dnsDirtyRef.current = false;
            return r;
        });
        if (result?.success)
            setDnsCustomOpen(false);
    };
    // Don't render content until first status arrives (prevents disconnect flash).
    // A status marked initializing is the backend saying a collection is still
    // running and it has nothing to report yet, which is the same situation.
    if (!status || status.initializing) {
        return (SP_JSX.jsx(DFL.PanelSection, { title: "WiFi Optimizer", children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.body, color: theme.text.secondary }, children: statusFailed
                        ? "Couldn't reach the plugin. It keeps trying. If this stays, restart the device."
                        : slowLoad
                            ? "Still reading WiFi status. This can take a while just after waking from sleep."
                            : "Loading WiFi status..." }) }) }));
    }
    const s = status?.settings;
    // A change the backend is still making, which may have outlasted the
    // panel's own wait for it. Every control waits for it too.
    const changeRunning = !!status?.profile_change_in_flight;
    // The change running is the lock moving by itself after a wake (C13).
    const wakeMoveRunning = status?.activity === "lock_move_after_wake";
    // What that move came to (C22). The backend keeps it until the user next
    // acts on the lock, at most 12 h. A banner for the first two minutes, as
    // other notices last; after that a line on the lock row, with the time.
    const wakeMove = status?.live?.lock_move_after_wake;
    const wakeMoveOutcome = wakeMove && Date.now() / 1000 - wakeMove.at < MESSAGE_TTL_MS / 1000 ? wakeMove.outcome : null;
    const wakeMoveLine = wakeMove && !wakeMoveOutcome && Date.now() / 1000 - wakeMove.at < WAKE_MOVE_KEPT_S
        ? wakeMoveRowText(wakeMove.outcome, wakeMove.at)
        : undefined;
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
        const drop = (prev) => {
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
    // Under iwd the lock and the band preference are paused, so turning one on
    // can only be refused. Each stays enabled while on, to be turned off.
    const onIwd = status?.live?.wifi_backend === "iwd" || !!status?.live?.pins_inactive_iwd;
    // Force 6 GHz with the network in use refused before it is tried (C21:
    // only with a 6 GHz access point of this network in range; otherwise the
    // key is absent, the row stays enabled and a press says why).
    const sixGhzRefusal = on6GHz ? "" : status?.live?.six_ghz_refusal ?? "";
    // The badge and line for a setting that is on for another network.
    const otherNetworkBadge = { badge: "unknown", text: "other network" };
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
    const gatedBadge = (key, on) => streamingIdle && on && !errors[key]
        ? { badge: "paused", text: "paused" }
        : getBadge(key, status, errors[key] ?? null);
    // The batch result, at the top of the panel and, for Force reapply, under
    // its own button too: the top is off screen from there (F2).
    const resultView = optimizeResult ? batchResultView(optimizeResult, resultFrom, "below") : null;
    const reapplyResult = optimizeResult && resultFrom === "reapply" ? batchResultView(optimizeResult, resultFrom, "above") : null;
    // The findings with a Dismiss, unless dismissed.
    const wpa2Shown = !!status?.wpa2_only_stronger && !dismissed.wpa2;
    const lockWeakerShown = !!status?.lock_weaker && !dismissed.lockWeaker;
    // What the first-run prompt says is on already.
    const alreadyOn = [
        s?.power_save_disabled ? "Prevent lag spikes" : "",
        s?.auto_fix_on_wake ? "Auto-fix on wake" : "",
    ].filter(Boolean);
    // Why there is no connection, where the backend can tell (older backends
    // send nothing, and a value this panel does not know, both of which read
    // as plainly disconnected). "Connect first, then optimize" only before
    // anything has been applied. asleep is NetworkManager left paused, most
    // often after a wake it missed, which a restart clears.
    const wifiState = status?.live?.wifi_state ?? "disconnected";
    // no_device is no wireless card in sysfs at all: an adapter unplugged, or a
    // built-in card that fell off the bus, which only a restart brings back.
    const disconnectedText = wifiState === "no_device"
        ? "No WiFi card was found. If this device has one built in, restart the device."
        : wifiState === "not_responding" || wifiState === "unavailable"
            ? NOT_RESPONDING_TEXT
            : wifiState === "radio_off"
                ? "WiFi is off. Turn it on in Steam's settings."
                : wifiState === "asleep"
                    ? "WiFi is paused by the system. Restart the device."
                    : s?.last_applied
                        ? "Not connected to WiFi."
                        : "Not connected to WiFi. Connect first, then optimize.";
    // Under iwd Optimize Safe leaves the lock out, so it is not one of the
    // things to wait for there, or "All good" never came. A lock that is on
    // for another network counts as done too: Optimize Safe leaves it where it
    // was set (_lock_held_elsewhere in main.py), so waiting for it here meant
    // "All good" never came on any other network. The row itself says the
    // lock does nothing here.
    const lockSafe = status?.live?.wifi_backend === "iwd" ||
        (s?.bssid_lock_enabled &&
            (!!status?.live?.bssid_lock_other_network || !status?.drift?.bssid_lock));
    // Check if all safe optimizations are already active
    const allSafeActive = !!connected &&
        (status?.live?.power_save_off || (streamingIdle && s?.power_save_disabled)) &&
        !status?.drift?.power_save &&
        lockSafe &&
        status?.live?.dispatcher_installed &&
        (status?.live?.buffer_tuning_applied || (streamingIdle && s?.buffer_tuning_enabled)) &&
        !status?.drift?.buffer_tuning;
    return (SP_JSX.jsxs(DetailsContext.Provider, { value: showDetails, children: [SP_JSX.jsx(PanelHeader, { driver: s?.driver ?? "", lastApplied: s?.last_applied ?? 0, lastEnforced: status?.live?.last_enforced, streaming: status?.live, fixesOn: !!(s?.power_save_disabled || s?.buffer_tuning_enabled || s?.cake_enabled), deviceLabel: deviceParts.length ? deviceParts.join(", ") : undefined, connected: connected }), statusFailed && (SP_JSX.jsx(Banner, { variant: "warning", children: "Lost contact with the plugin, so this may be out of date. It keeps trying. If this stays, restart the device." })), (status?.settings_unwritable || status?.settings_link_refused) && (SP_JSX.jsx(Banner, { variant: "warning", children: status?.settings_link_refused
                    ? "A folder on the way to the plugin's settings (~/homebrew/settings) is a link, which the plugin doesn't follow. Make it a real folder."
                    : "The plugin couldn't save its settings, so changes may not last past a restart. Check that the device has free storage space, then try the change again." })), changeRunning && !busyState && (SP_JSX.jsx(Banner, { variant: "info", children: wakeMoveRunning
                    ? "Checking the access point lock after sleep. The controls come back when it is done."
                    : "A WiFi change is still finishing. The controls come back when it is done." })), wakeMoveOutcome === "moved" && (SP_JSX.jsx(Banner, { variant: "info", children: "After sleep, the lock moved to a stronger access point." })), wakeMoveOutcome === "back" && (SP_JSX.jsx(Banner, { variant: "info", children: "After sleep, a stronger access point refused the connection. The lock stayed." })), wakeMoveOutcome === "off" && (SP_JSX.jsx(Banner, { variant: "warning", children: "After sleep, the lock couldn't move to a stronger access point." })), supportTier === 3 && (SP_JSX.jsx(Banner, { variant: "info", children: "Unknown WiFi card: driver-specific fixes are off. Copy diagnostics (bottom of this panel) and report it on GitHub." })), status?.settings_recovered && (SP_JSX.jsx(Banner, { variant: "warning", children: ERROR_MESSAGES.parse_error })), status?.settings_recovered && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: handleDismissRecovered, children: "Dismiss" }) }) })), SP_JSX.jsx(WakeFailureNotice, {}), updateInfo?.update_available && !updating && (SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: handleApplyUpdate, children: updateInfo.is_downgrade
                                ? `Go back to v${updateInfo.latest_version}`
                                : `Update to v${updateInfo.latest_version}` }) }), updateError && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.body, color: theme.warning.text }, children: updateError }) }))] })), updating && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.body, color: theme.info.text }, children: updatingText(updateProgress) }) }) })), connected && !s?.last_applied && (SP_JSX.jsxs(Banner, { variant: "info", children: [alreadyOn.length > 0 &&
                        `${joinNames(alreadyOn)} ${alreadyOn.length > 1 ? "are" : "is"} already on. `, "Select ", SP_JSX.jsx("strong", { children: "Optimize Safe" }), " to turn on the rest of the recommended settings."] })), hasDuplicates && !dupDismissed && (SP_JSX.jsxs(Banner, { variant: "warning", children: ["This network is saved more than once with different security.", " ", status?.duplicates_prioritized
                        ? "The copy in use now is kept first."
                        : "If the connection acts up, remove the extra copy in Desktop Mode."] })), hasDuplicates && !dupDismissed && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", onClick: handleDismissDuplicates, children: "Dismiss" }) }) })), status?.address_conflict && (SP_JSX.jsxs(Banner, { variant: "warning", children: ["Another device on your network has this device's address (", status.address_conflict.ip, "), so the connection may not work. Restart your router."] })), (wpa2Shown || errors.allow_wpa2) && (SP_JSX.jsxs(Banner, { variant: "info", children: [status?.wpa2_only_stronger && wpa2Shown && (SP_JSX.jsx(SP_JSX.Fragment, { children: `A stronger access point (signal ${status.wpa2_only_stronger.signal} vs ${status.wpa2_only_stronger.current_signal} of 100${status.wpa2_only_stronger.bssid_tail
                            ? `, address ends ${status.wpa2_only_stronger.bssid_tail}`
                            : ""}) offers only WPA2. Allow WPA2 only if that access point is yours: it makes your WiFi password easier to attack. Reconnects WiFi.` })), errors.allow_wpa2 && (SP_JSX.jsx("div", { style: { color: theme.error.text }, children: errors.allow_wpa2 }))] })), wpa2Shown && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: handleAllowWpa2, children: working === "allow_wpa2" ? WORKING_TEXT : "Allow WPA2 here" }) }) })), (wpa2Shown || errors.allow_wpa2) && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: handleDismissWpa2, children: "Dismiss" }) }) })), (lockWeakerShown || errors.move_bssid_lock) && (SP_JSX.jsxs(Banner, { variant: "warning", children: [status?.lock_weaker && lockWeakerShown && (SP_JSX.jsx(SP_JSX.Fragment, { children: `Locked to a weak access point (signal ${status.lock_weaker.current_signal} of 100). A stronger one is in range (${status.lock_weaker.signal} of 100). Moving reconnects WiFi.` })), errors.move_bssid_lock && (SP_JSX.jsx("div", { style: { color: theme.error.text }, children: errors.move_bssid_lock }))] })), lockWeakerShown && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: handleMoveLock, children: working === "move_bssid_lock" ? MOVE_WORKING_TEXT : "Move the lock" }) }) })), (lockWeakerShown || errors.move_bssid_lock) && (SP_JSX.jsx(DFL.PanelSection, { children: SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: handleDismissLockWeaker, children: "Dismiss" }) }) })), notices.move_bssid_lock && (SP_JSX.jsx(Banner, { variant: "info", children: notices.move_bssid_lock })), notices.allow_wpa2 && (SP_JSX.jsx(Banner, { variant: "info", children: notices.allow_wpa2 })), status?.external?.pin_released && (SP_JSX.jsx(Banner, { variant: "warning", children: "WiFi couldn't connect, so the plugin let go of its pin. The setting stays on and comes back once WiFi works." })), connected && !!s?.last_applied && driftCount > 0 && (SP_JSX.jsx(Banner, { variant: "warning", children: driftCount > 1
                    ? `These settings no longer match what you set: ${driftNames.join(", ")}. Fix now puts them back.`
                    : `${driftNames[0]} no longer matches what you set. Fix now puts it back.` })), connected && !!s?.last_applied && driftCount > 0 && (SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: handleRepairDrift, children: "Fix now" }) }), applyingAll && batchFrom === "fix" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.small, color: theme.info.text }, children: BATCH_WORKING_TEXT }) }))] })), status?.success === false && (SP_JSX.jsxs(Banner, { variant: "error", children: [status?.message ?? "Couldn't read WiFi status.", " The panel keeps trying. If this stays, restart the device."] })), status?.success !== false && !connected && (SP_JSX.jsx(Banner, { variant: "error", children: disconnectedText })), status?.success !== false && connected && status?.live?.wifi_state === "not_responding" && (SP_JSX.jsx(Banner, { variant: "warning", children: NOT_RESPONDING_TEXT })), backendSwitch && !backendSwitch.in_progress && backendSwitch.result && (() => {
                const r = backendSwitch.result;
                // Treat reconnect timeout as a warning even when the backend-level
                // switch succeeded - the system is switched but WiFi didn't come back.
                const isWarning = !r.success || r.needs_reboot || r.reconnect_timed_out;
                return (SP_JSX.jsx(Banner, { variant: isWarning ? "warning" : "success", children: backendResultText(r) }));
            })(), resultView && SP_JSX.jsx(Banner, { variant: resultView.variant, children: resultView.text }), SP_JSX.jsxs(DFL.PanelSection, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", 
                            // "All good" is a state, not an action: pressing it ran Optimize
                            // Safe again, which can reconnect WiFi for nothing.
                            disabled: !connected || applyingAll || isBusy || updating || allSafeActive, onClick: handleOptimize, children: applyingAll
                                ? "Applying..."
                                : allSafeActive
                                    ? "All good"
                                    : "Optimize Safe" }) }), applyingAll && batchFrom === "optimize" && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: theme.fontSize.small, color: theme.info.text }, children: BATCH_WORKING_TEXT }) }))] }), SP_JSX.jsxs(DFL.PanelSection, { title: "Power & stability", children: [SP_JSX.jsx(InfoRow, { label: "Prevent lag spikes", subtitle: "Disables WiFi power save and PCIe power states", explanation: "The OS saves WiFi power in several places: the wireless chip, the PCIe link between the chip and the CPU, and low power modes in the driver. These can cause latency spikes, packets sent in batches, and lower throughput during a long stream. This turns them off, so the WiFi hardware stays fully awake. It uses a little more battery. On some cards part of it needs a restart to take effect.", ...gatedBadge("power_save", s?.power_save_disabled), checked: s?.power_save_disabled ?? false, disabled: isBusy || updating, error: errors.power_save, notice: notices.power_save, onChange: (val) => handleToggle("power_save", () => setPowerSave(val)) }), SP_JSX.jsx(InfoRow, { label: "Stop background scanning", subtitle: "The access point lock: pins one access point. Turn off to roam.", 
                        // The move after a wake runs only on the card it was measured on
                        // (C25), so the (i) promises it nowhere else.
                        explanation: `Stops the scans for a better access point, which make a stream stutter, by pinning the strongest access point of this network. Only for the network you turn it on for. ${s?.driver?.startsWith(WAKE_MOVE_DRIVER)
                            ? "It stays there until you move it or turn the lock off. After a wake, with no game or stream running, it moves to a clearly stronger one."
                            : "It stays there until you move it or turn the lock off."} Turning it on or off reconnects WiFi for up to half a minute.`, ...(status?.live?.bssid_lock_other_network && !errors.bssid_lock
                            ? otherNetworkBadge
                            : getBadge("bssid_lock", status, errors.bssid_lock ?? null)), checked: s?.bssid_lock_enabled ?? false, disabled: isBusy || updating || (!s?.bssid_lock_enabled && (!connected || onIwd)), working: working === "bssid_lock", error: errors.bssid_lock, notice: notices.bssid_lock ??
                            (onIwd
                                ? s?.bssid_lock_enabled ? IWD_PIN_NOTICE : IWD_OFF_NOTICE
                                : status?.live?.bssid_lock_other_network && s?.bssid_lock_enabled
                                    ? OTHER_NETWORK_TEXT
                                    : status?.external?.bssid_lock
                                        ? "This network is already pinned to one access point by a setting outside this plugin. Turn this on and off to clear it."
                                        : wakeMoveLine), onChange: (val) => handleToggle("bssid_lock", () => setBssidLock(val)) }), SP_JSX.jsx(InfoRow, { label: "Auto-fix on wake", subtitle: "Reapplies settings after sleep", explanation: "The OS often resets WiFi settings after sleep or updates. This installs a small script that automatically re-applies your optimizations every time the WiFi reconnects. It runs outside of Decky, so it works even if Decky has issues. Removing the plugin will also remove this script.", ...getBadge(undefined, status, errors.auto_fix ?? null), checked: s?.auto_fix_on_wake ?? false, disabled: isBusy || updating, error: errors.auto_fix, notice: notices.auto_fix, onChange: (val) => handleToggle("auto_fix", () => setAutoFix(val)) }), SP_JSX.jsx(InfoRow, { label: "Network buffer tuning", subtitle: "Larger UDP buffers for streaming", explanation: "Increases the kernel's network buffer sizes to handle the bursty UDP traffic that game streaming produces. Without this, packets can be dropped during high-bitrate moments, causing frame drops or brief quality dips. These settings apply to every network connection, wired ones too. They reset on every reboot, and are put back when the plugin starts, after a game launches, and when WiFi reconnects if Auto-fix on wake is on.", ...gatedBadge("buffer_tuning", s?.buffer_tuning_enabled), checked: s?.buffer_tuning_enabled ?? false, disabled: isBusy || updating, error: errors.buffer_tuning, notice: notices.buffer_tuning, onChange: (val) => handleToggle("buffer_tuning", () => setBufferTuning(val)) })] }), SP_JSX.jsxs(DFL.PanelSection, { title: "Advanced", children: [regdomain?.readable && regdomain.changeable && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { label: "WiFi region", description: "Which channels this device may use. The wrong region can hide 5 GHz and 6 GHz channels.", rgOptions: [
                                // Not set by the plugin: labelled with the region in use.
                                // Once the plugin has set one, choosing this puts back the
                                // region from before, so it says that instead of naming the
                                // plugin's own region.
                                {
                                    data: REGION_UNMANAGED,
                                    label: regdomain.setting
                                        ? "Not managed (put back the region from before)"
                                        : `Not managed (${regdomain.governing ? governingName(regdomain.governing, regdomain.setting) : "unknown"})`,
                                },
                                ...REGION_OPTIONS,
                                ...(regdomain.setting &&
                                    !REGION_OPTIONS.some((o) => o.data === regdomain.setting)
                                    ? [{ data: regdomain.setting, label: regdomain.setting }]
                                    : []),
                            ], selectedOption: regdomain.setting || REGION_UNMANAGED, disabled: isBusy || updating, onChange: (option) => {
                                const unmanaged = option.data === REGION_UNMANAGED;
                                // "" puts back the region from before the plugin set one,
                                // or World when there was none, so it is only sent to stop
                                // managing a region the plugin did set.
                                if (unmanaged && !regdomain.setting)
                                    return;
                                void handleToggle("regdomain", () => setRegdomain(unmanaged ? "" : option.data)).then(refreshRegdomain);
                            } }) })), SP_JSX.jsx(MessageRow, { error: errors.regdomain, notice: notices.regdomain }), regdomain?.readable &&
                        regdomain.changeable &&
                        !errors.regdomain &&
                        !notices.regdomain &&
                        regdomain.setting &&
                        regdomain.governing &&
                        regdomain.governing !== regdomain.setting && (SP_JSX.jsx(MessageRow, { notice: `${regionName(regdomain.setting)} (not in effect: the card uses ${governingName(regdomain.governing, regdomain.setting)})` })), regdomain?.readable && !regdomain.changeable && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx("div", { style: { fontSize: "12px", opacity: 0.8, padding: "4px 0" }, children: regdomain.governing === "99"
                                ? "WiFi region: set by the WiFi card. That's normal and hides no band."
                                : regdomain.governing === "00"
                                    ? "WiFi region: the world default, set by the WiFi card."
                                    : `WiFi region: ${regdomain.governing ? governingName(regdomain.governing, regdomain.setting) : "unknown"}, set by the WiFi card. That's normal and hides no band.` }) })), SP_JSX.jsx(InfoRow, { label: "Force 5 GHz band", subtitle: "Avoid 2.4 GHz Bluetooth interference", explanation: `This is the band preference. Keeps WiFi on 5 GHz, away from Bluetooth on 2.4 GHz${isDeckLcd ? ", which shares the antennas on the Steam Deck LCD" : ""}. Refused if no 5 GHz access point is in range; released after about a minute out of range. Reconnects WiFi for up to half a minute.${supports6GHz
                            ? status?.live?.six_ghz_offered || on6GHz
                                ? " This keeps the device on 5 GHz specifically, so leave it off if you connect over 6 GHz. To stay on 6 GHz, use Force 6 GHz band below instead."
                                : " This keeps the device on 5 GHz specifically, so leave it off if you connect over 6 GHz."
                            : ""}`, ...(bandOther && !on6GHz && !band5Error
                            ? otherNetworkBadge
                            : getBadge(on6GHz ? undefined : "band_preference", status, band5Error ?? null)), checked: (s?.band_preference_enabled ?? false) && !on6GHz, disabled: isBusy || updating || (!(s?.band_preference_enabled && !on6GHz) && (!connected || onIwd)), working: working === "band_5ghz", error: band5Error, notice: notices.band_5ghz ??
                            (on6GHz
                                ? undefined
                                : notices.band_preference ??
                                    (onIwd
                                        ? s?.band_preference_enabled ? IWD_PIN_NOTICE : IWD_OFF_NOTICE
                                        : bandOther
                                            ? OTHER_NETWORK_TEXT
                                            : onBg
                                                ? "This network is held to 2.4 GHz by an older setting, not 5 GHz. Turn this off, then on again, to use 5 GHz."
                                                : status?.external?.band
                                                    ? "This network is already limited to one band by a setting outside this plugin. Turn this on and off to clear it."
                                                    : undefined)), onChange: (val) => {
                            clearBandMessages();
                            handleToggle("band_5ghz", () => 
                            // Turning it on always asks for 5 GHz, which is what the
                            // toggle says, even over a 2.4 GHz setting from an old
                            // version. Never "6GHz": turning that off leaves it recorded
                            // as the band.
                            val
                                ? setBandPreference(true, "a")
                                : setBandPreference(val, s?.band_preference === "bg" ? "bg" : "a"));
                        } }), (status?.live?.six_ghz_offered || on6GHz) && (SP_JSX.jsx(InfoRow, { label: "Force 6 GHz band", 
                        // Refused for the network in use (C21): the reason and what to do
                        // stand where the subtitle is, and the toggle can't be turned on.
                        subtitle: sixGhzRefusal || "Stay on 6 GHz where your router has it", explanation: "This is the band preference. Keeps the device on 6 GHz for this network: the least crowded band, but shorter range than 5 GHz, and only on WiFi 6E or newer routers. Needs WPA3. If this network can't use it, the row says why. If the device can't connect on 6 GHz, the band is put back. Turning it on or off reconnects WiFi. Out of range, it is released after about a minute.", ...(bandOther && on6GHz && !band6Error
                            ? otherNetworkBadge
                            : getBadge(on6GHz ? "band_preference" : undefined, status, band6Error ?? null)), checked: on6GHz, disabled: isBusy || updating || (!on6GHz && (!connected || !!sixGhzRefusal)), working: working === "band_6ghz", error: band6Error, notice: notices.band_6ghz ??
                            (on6GHz
                                ? notices.band_preference ??
                                    (onIwd
                                        ? IWD_PIN_NOTICE
                                        : bandOther
                                            ? OTHER_NETWORK_TEXT
                                            : undefined)
                                : undefined), onChange: (val) => {
                            clearBandMessages();
                            handleToggle("band_6ghz", () => setBandPreference(val, "6GHz"));
                        } })), SP_JSX.jsx(InfoRow, { label: "Custom DNS", subtitle: "Override DNS servers for this network", explanation: "Replaces this network's DNS servers, including any it offers over IPv6, with the ones you pick. Public DNS like Cloudflare or Google can be faster and more reliable than your provider's. Only for the network you set it on. Leave it off on a network with no IPv4. Each change reconnects WiFi for up to half a minute.", ...(status?.live?.dns_other_network && s?.dns_enabled && !errors.dns
                            ? otherNetworkBadge
                            : getBadge("dns", status, errors.dns ?? null)), checked: s?.dns_enabled ?? false, disabled: isBusy || updating || (!connected && !s?.dns_enabled), working: working === "dns", error: errors.dns, notice: notices.dns ??
                            (status?.live?.dns_other_network && s?.dns_enabled ? OTHER_NETWORK_TEXT : undefined), onChange: (val) => {
                            if (!val)
                                resetDnsDraft();
                            handleToggle("dns", () => setDns(val, s?.dns_provider ?? "cloudflare", customDnsInput));
                        }, children: s?.dns_enabled && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.DropdownItem, { label: "DNS provider", rgOptions: DNS_OPTIONS, selectedOption: dnsCustomOpen ? "custom" : s?.dns_provider ?? "cloudflare", disabled: isBusy || updating, onChange: (option) => {
                                            if (option.data === "custom") {
                                                // Open the editor; nothing is sent until Apply.
                                                setDnsCustomOpen(true);
                                                return;
                                            }
                                            resetDnsDraft();
                                            handleToggle("dns", () => setDns(true, option.data, ""));
                                        } }) }), (dnsCustomOpen || s?.dns_provider === "custom") && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.TextField, { label: "DNS servers (space-separated)", value: customDnsInput, onChange: (e) => {
                                                    dnsDirtyRef.current = true;
                                                    setCustomDnsInput(e.target.value);
                                                } }) }), (dnsCustomOpen ||
                                            customDnsInput.trim() !== (s?.dns_servers ?? "")) && (SP_JSX.jsxs(SP_JSX.Fragment, { children: [SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating || !customDnsInput.trim(), onClick: applyCustomDns, children: "Apply DNS servers" }) }), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(DFL.ButtonItem, { layout: "below", disabled: isBusy || updating, onClick: resetDnsDraft, children: "Cancel" }) })] }))] }))] })) }), SP_JSX.jsx(InfoRow, { label: "Disable IPv6", subtitle: "Use IPv4 only on this network", explanation: "This is the IPv6 setting. Forces all traffic through IPv4. Only turn this on if this network's IPv6 is broken (slow lookups, timeouts). Most networks handle IPv6 fine. Turning it on or off reconnects WiFi for up to half a minute.", ...(status?.live?.ipv6_other_network && s?.ipv6_disabled && !errors.ipv6
                            ? otherNetworkBadge
                            : getBadge("ipv6", status, errors.ipv6 ?? null)), notice: notices.ipv6 ??
                            (status?.external?.ipv6
                                ? "IPv6 is off on this network, set outside this plugin. To turn it back on, turn this on, then off."
                                : status?.live?.ipv6_other_network
                                    ? OTHER_NETWORK_TEXT
                                    : undefined), checked: s?.ipv6_disabled ?? false, disabled: isBusy || updating || (!connected && !s?.ipv6_disabled), working: working === "ipv6", error: errors.ipv6, onChange: (val) => handleToggle("ipv6", () => setIpv6(val)) }), SP_JSX.jsx(InfoRow, { label: "Traffic shaping (CAKE)", subtitle: "Fair queuing for traffic this device sends", explanation: "Fair queuing on the network interface. Most built-in WiFi cards already do this in the driver, so the effect there is small. It is lost on reboot, and put back when the plugin starts, after a game launches, and when WiFi reconnects if Auto-fix on wake is on.", ...gatedBadge("cake", s?.cake_enabled), checked: s?.cake_enabled ?? false, disabled: isBusy || updating || (!connected && !s?.cake_enabled), error: errors.cake, notice: notices.cake, onChange: (val) => handleToggle("cake", () => setCake(val)) }), status?.live?.backend_tool_available && (SP_JSX.jsx(BackendToggleRow, { status: status, backendSwitch: backendSwitch, error: errors.wifi_backend, isBusy: isBusy || updating, onToggle: handleBackendToggle }))] }), SP_JSX.jsx(StreamingSection, { settings: s, live: status?.live, disabled: isBusy || updating, errors: errors, notices: notices, onToggle: handleToggle }), SP_JSX.jsxs(DFL.PanelSection, { title: "Live status", children: [connected && status?.live?.ip_address && (SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsxs("div", { style: { fontSize: theme.fontSize.tiny, color: theme.text.tertiary }, children: ["IP: ", status.live.ip_address] }) })), SP_JSX.jsx(DFL.PanelSectionRow, { children: SP_JSX.jsx(StatsGrid, { live: status?.live ?? {}, connected: connected }) })] }), SP_JSX.jsx(ActionsSection, { connected: connected, isBusy: isBusy || updating, onForceReapply: handleForceReapply, onReset: handleResetSettings, resetError: errors.reset, resetNotice: notices.reset, resetNoticeInfo: !!notices.reset && notices.reset !== RESET_DONE_TEXT, reapplyResult: reapplyResult, workingText: working === "reset" ? RESET_WORKING_TEXT
                    : applyingAll && batchFrom === "reapply" ? BATCH_WORKING_TEXT
                        : null }), SP_JSX.jsx(UpdatesSection, { channel: s?.update_channel ?? "stable", updating: updating, updatingText: updatingText(updateProgress), checkingUpdate: checkingUpdate, updateInfo: updateInfo, updateError: updateError, settingError: updateSettingError, onChannelChange: handleChannelChange, notify: s?.update_notify ?? true, onNotifyChange: handleNotifyChange, onApply: handleApplyUpdate, onCheck: handleCheckForUpdate, busy: isBusy }), SP_JSX.jsx(PanelFooter, { version: status?.version ?? "?", showDetails: showDetails, onShowDetails: handleShowDetails })] }));
}
var index = definePlugin(() => {
    let gameLifetimeUnsub = null;
    // A launch just before the plugin is unloaded still had its delayed
    // reapply pending, which then called into a backend being torn down.
    let disposed = false;
    const timers = new Set();
    const announce = setTimeout(() => {
        timers.delete(announce);
        if (!disposed)
            checkUpdateOnLoad();
    }, ANNOUNCE_DELAY_MS);
    timers.add(announce);
    const stopUpdateNotices = startUpdateNotices();
    try {
        const later = (ms, fn) => {
            const id = setTimeout(() => {
                timers.delete(id);
                if (!disposed)
                    fn();
            }, ms);
            timers.add(id);
        };
        gameLifetimeUnsub = SteamClient.GameSessions.RegisterForAppLifetimeNotifications((notification) => {
            if (disposed)
                return;
            // Streaming auto mode looks now and again once the app has had time
            // to start or exit, instead of waiting for its next pass. With auto
            // mode off it still records the app event, which keeps the
            // after-wake move and the lock scans from reconnecting as a game
            // starts.
            pokeDetection().catch(() => { });
            later(3000, () => pokeDetection().catch(() => { }));
            if (notification.bRunning) {
                later(3000, () => reapplyVolatile().catch(() => { }));
            }
        });
    }
    catch (e) {
        console.error("WiFi Optimizer: failed to register game launch listener", e);
    }
    return {
        name: "WiFi Optimizer",
        titleView: SP_JSX.jsx("div", { className: DFL.staticClasses.Title, children: "WiFi Optimizer" }),
        content: (SP_JSX.jsx(ErrorBoundary, { children: SP_JSX.jsx(Content, {}) })),
        icon: SP_JSX.jsx(FaWifi, {}),
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

export { index as default };
//# sourceMappingURL=index.js.map
