import type { BadgeStatus } from "../types";
import { theme } from "../theme";

const BADGE_STYLES: Record<BadgeStatus, { background: string; color: string }> = {
  active: { background: theme.success.badgeBg, color: theme.success.text },
  locked: { background: theme.success.badgeBg, color: theme.success.text },
  set: { background: theme.success.badgeBg, color: theme.success.text },
  drifted: { background: theme.warning.badgeBg, color: theme.warning.text },
  off: { background: theme.surface.md, color: theme.text.muted },
  error: { background: theme.error.badgeBg, color: theme.error.text },
  unknown: { background: theme.surface.md, color: theme.text.tertiary },
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
        fontSize: theme.fontSize.small,
        padding: "2px 6px",
        borderRadius: theme.radius.sm,
        whiteSpace: "nowrap",
        background: style.background,
        color: style.color,
      }}
    >
      {text}
    </span>
  );
}
