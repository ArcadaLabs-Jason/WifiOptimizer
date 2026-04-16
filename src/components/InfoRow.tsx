import { useState } from "react";
import { PanelSectionRow, ToggleField } from "@decky/ui";
import type { BadgeStatus } from "../types";
import { StatusBadge } from "./StatusBadge";
import { theme } from "../theme";

interface InfoRowProps {
  label: string;
  subtitle: string;
  explanation: string;
  badge?: BadgeStatus;
  text?: string;
  checked: boolean;
  disabled?: boolean;
  error?: string | null;
  onChange: (val: boolean) => void;
  children?: React.ReactNode;
}

export function InfoRow({
  label,
  subtitle,
  explanation,
  badge,
  text,
  checked,
  disabled = false,
  error,
  onChange,
  children,
}: InfoRowProps) {
  const showBadge = badge !== undefined && text !== undefined;
  const [expanded, setExpanded] = useState(false);

  return (
    <>
      <PanelSectionRow>
        <ToggleField
          label={
            <span style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <span
                onClick={(e: React.MouseEvent) => {
                  e.stopPropagation();
                  setExpanded(!expanded);
                }}
                style={{
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
                }}
              >
                i
              </span>
              <span>{label}</span>
            </span>
          }
          description={
            <span style={{ display: "block" }}>
              {showBadge && (
                <span
                  style={{
                    display: "flex",
                    justifyContent: "flex-end",
                    marginBottom: "4px",
                  }}
                >
                  <StatusBadge badge={badge!} text={text!} />
                </span>
              )}
              {error ? (
                <span style={{ color: theme.error.text }}>{error}</span>
              ) : (
                <span style={{ color: theme.text.subtitle, fontSize: theme.fontSize.small }}>
                  {subtitle}
                </span>
              )}
            </span>
          }
          checked={checked}
          disabled={disabled}
          onChange={onChange}
        />
      </PanelSectionRow>
      {expanded && (
        <PanelSectionRow>
          <div
            style={{
              padding: "8px 12px",
              background: theme.surface.xs,
              borderRadius: theme.radius.md,
              fontSize: theme.fontSize.small,
              lineHeight: "1.5",
              color: theme.text.secondary,
            }}
          >
            {explanation}
          </div>
        </PanelSectionRow>
      )}
      {children}
    </>
  );
}
