import type { BadgeStatus } from "../types";

const BADGE_STYLES: Record<BadgeStatus, { background: string; color: string }> = {
  active: { background: "rgba(29,158,117,0.15)", color: "#3fc56e" },
  locked: { background: "rgba(29,158,117,0.15)", color: "#3fc56e" },
  set: { background: "rgba(29,158,117,0.15)", color: "#3fc56e" },
  drifted: { background: "rgba(223,138,0,0.15)", color: "#ffc669" },
  off: { background: "rgba(255,255,255,0.06)", color: "#6a6a7a" },
  error: { background: "rgba(211,36,43,0.15)", color: "#ff878c" },
  unknown: { background: "rgba(255,255,255,0.06)", color: "#8a8a9a" },
};

interface StatusBadgeProps {
  badge: BadgeStatus;
  text: string;
}

export function StatusBadge({ badge, text }: StatusBadgeProps) {
  const style = BADGE_STYLES[badge];
  return (
    <span
      style={{
        fontSize: "11px",
        padding: "2px 6px",
        borderRadius: "4px",
        whiteSpace: "nowrap",
        background: style.background,
        color: style.color,
      }}
    >
      {text}
    </span>
  );
}
