import { Focusable } from "@decky/ui";
import type { LiveStatus } from "../types";
import { theme } from "../theme";

function signalColor(dbm: string | undefined): string {
  if (!dbm) return theme.text.muted;
  const val = parseInt(dbm);
  if (isNaN(val)) return theme.text.muted;
  if (val > -50) return theme.success.text;
  if (val > -70) return theme.text.primary;
  if (val > -80) return theme.warning.text;
  return theme.error.text;
}

function bandLabel(freqStr: string | undefined): string {
  if (!freqStr) return "--";
  const mhz = parseInt(freqStr);
  if (isNaN(mhz)) return freqStr;
  if (mhz < 3000) return "2.4 GHz";
  if (mhz < 5925) return "5 GHz";
  return "6 GHz";
}

function bandColor(freqStr: string | undefined): string {
  if (!freqStr) return theme.text.muted;
  const mhz = parseInt(freqStr);
  if (isNaN(mhz)) return theme.text.primary;
  if (mhz < 3000) return theme.warning.text; // 2.4 GHz (suboptimal)
  return theme.success.text; // 5/6 GHz (good)
}

interface StatsGridProps {
  live: LiveStatus;
  connected: boolean;
}

export function StatsGrid({ live, connected }: StatsGridProps) {
  const na = "--";
  const signal = connected ? live.signal_dbm ?? na : na;
  const speed = connected ? live.tx_bitrate ?? na : na;
  const band = connected ? bandLabel(live.frequency) : na;
  const freq = connected ? live.frequency ?? "" : "";
  const channel = connected ? live.channel ?? na : na;

  const cell: React.CSSProperties = {
    background: theme.surface.sm,
    borderRadius: theme.radius.sm,
    padding: "4px 8px",
  };
  const lbl: React.CSSProperties = {
    fontSize: theme.fontSize.label,
    color: theme.text.muted,
    textTransform: "uppercase",
    letterSpacing: "0.5px",
  };
  const val = (color: string): React.CSSProperties => ({
    fontSize: theme.fontSize.heading,
    fontWeight: 500,
    color: connected ? color : theme.text.muted,
  });

  return (
    <Focusable
      style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "4px" }}
    >
      <div style={cell}>
        <div style={lbl}>Signal</div>
        <div style={val(signalColor(live.signal_dbm))}>{signal}</div>
      </div>
      <div style={cell}>
        <div style={lbl}>Speed</div>
        <div style={val(theme.text.primary)}>{speed}</div>
      </div>
      <div style={cell}>
        <div style={lbl}>Band</div>
        <div style={val(bandColor(live.frequency))}>{band}</div>
        {freq && (
          <div style={{ fontSize: theme.fontSize.label, color: theme.text.muted }}>
            {freq} MHz
          </div>
        )}
      </div>
      <div style={cell}>
        <div style={lbl}>Channel</div>
        <div style={val(theme.text.primary)}>{channel}</div>
      </div>
    </Focusable>
  );
}
