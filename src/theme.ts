// Design tokens for the WiFi Optimizer panel. Keep this in sync with the
// palette used in the README mockup and the four semantic banner colors
// (success/warning/error/info) that Decky's Quick Access Menu supports well
// against its dark background.

export const theme = {
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
} as const;
