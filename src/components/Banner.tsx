import { PanelSection, PanelSectionRow } from "@decky/ui";
import type { ReactNode } from "react";
import { theme } from "../theme";

type BannerVariant = "success" | "warning" | "error" | "info";

// No icon glyphs: the color says how serious it is, and the text says what
// happened and what to do.
interface BannerProps {
  variant: BannerVariant;
  children: ReactNode;
}

// Shared banner styling for the panel's many inline notifications
// (unknown hardware, drift alert, optimize result, backend switch
// result, update errors, etc.). Each banner sits in its own
// PanelSection so Decky's QAM can focus it with gamepad navigation.
export function Banner({ variant, children }: BannerProps) {
  const palette = theme[variant];
  return (
    <PanelSection>
      <PanelSectionRow>
        <div
          style={{
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
          }}
        >
          <span>{children}</span>
        </div>
      </PanelSectionRow>
    </PanelSection>
  );
}
