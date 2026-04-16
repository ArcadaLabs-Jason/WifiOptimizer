import { useState } from "react";
import { PanelSectionRow, ToggleField } from "@decky/ui";
import type { BadgeStatus } from "../types";
import { StatusBadge } from "./StatusBadge";

interface InfoRowProps {
  label: string;
  subtitle: string;
  explanation: string;
  badge: BadgeStatus;
  text: string;
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
                  background: expanded
                    ? "rgba(55,138,221,0.2)"
                    : "rgba(255,255,255,0.08)",
                  color: expanded ? "#60baff" : "#8a8a9a",
                  fontSize: "10px",
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
            error ? (
              <span style={{ color: "#ff878c" }}>{error}</span>
            ) : (
              <span style={{ color: "#7a7a8a", fontSize: "11px" }}>{subtitle}</span>
            )
          }
          checked={checked}
          disabled={disabled}
          onChange={onChange}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <div style={{ padding: "0 0 4px 0", marginTop: "-6px" }}>
          <StatusBadge badge={badge} text={text} />
        </div>
      </PanelSectionRow>
      {expanded && (
        <PanelSectionRow>
          <div
            style={{
              padding: "8px 12px",
              background: "rgba(255,255,255,0.02)",
              borderRadius: "6px",
              fontSize: "11px",
              lineHeight: "1.5",
              color: "#9a9aaa",
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
