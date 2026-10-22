import { createContext, useContext, useState } from "react";
import { PanelSectionRow, ToggleField } from "@decky/ui";
import type { BadgeStatus } from "../types";
import { StatusBadge } from "./StatusBadge";
import { theme } from "../theme";

// The panel's "Show details" toggle. The (i) next to each label only answers
// a tap: it sits inside the toggle's label, where the D-pad cannot focus it
// apart from the toggle itself. The toggle opens every explanation at once,
// so they can be read with a controller too.
export const DetailsContext = createContext(false);

// Shown in place of the subtitle while a change that reconnects WiFi runs.
// Those take from a few seconds to about a minute.
export const WORKING_TEXT = "Working, WiFi will reconnect...";

// An error or notice on a row of its own, under the control it is about,
// in the colors and size a toggle's own message uses.
export function MessageRow({ error, notice }: { error?: string | null; notice?: string | null }) {
  if (!error && !notice) return null;
  return (
    <PanelSectionRow>
      <div
        style={{
          fontSize: theme.fontSize.small,
          color: error ? theme.error.text : theme.info.text,
        }}
      >
        {error || notice}
      </div>
    </PanelSectionRow>
  );
}

interface InfoRowProps {
  label: string;
  subtitle: string;
  explanation: string;
  badge?: BadgeStatus;
  text?: string;
  checked: boolean;
  disabled?: boolean;
  error?: string | null;
  // Something worth saying that is not a failure - a setter that did
  // something other than exactly what was asked, and succeeded.
  notice?: string | null;
  // This row's change is running now.
  working?: boolean;
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
  notice,
  working = false,
  onChange,
  children,
}: InfoRowProps) {
  const showBadge = badge !== undefined && text !== undefined;
  const [tapped, setTapped] = useState(false);
  const showAll = useContext(DetailsContext);
  const expanded = tapped || showAll;

  return (
    <>
      <PanelSectionRow>
        <ToggleField
          label={
            <span style={{ display: "flex", alignItems: "center", gap: "4px" }}>
              <span
                onClick={(e: React.MouseEvent) => {
                  e.stopPropagation();
                  setTapped(!tapped);
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
              {working ? (
                <span style={{ color: theme.info.text, fontSize: theme.fontSize.small }}>
                  {WORKING_TEXT}
                </span>
              ) : error ? (
                <span style={{ color: theme.error.text }}>{error}</span>
              ) : notice ? (
                <span style={{ color: theme.info.text, fontSize: theme.fontSize.small }}>
                  {notice}
                </span>
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
