import { Focusable } from "@decky/ui";
import type { LiveStatus } from "../types";

function signalColor(dbm: string | undefined): string {
  if (!dbm) return "#6a6a7a";
  const val = parseInt(dbm);
  if (isNaN(val)) return "#6a6a7a";
  if (val > -50) return "#3fc56e";
  if (val > -70) return "#e0e0e0";
  if (val > -80) return "#ffc669";
  return "#ff878c";
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
  if (!freqStr) return "#6a6a7a";
  const mhz = parseInt(freqStr);
  if (isNaN(mhz)) return "#e0e0e0";
  if (mhz < 3000) return "#ffc669"; // yellow - 2.4 GHz (suboptimal)
  return "#3fc56e"; // green - 5/6 GHz (good)
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
    background: "rgba(255,255,255,0.04)",
    borderRadius: "4px",
    padding: "4px 8px",
  };
  const lbl: React.CSSProperties = {
    fontSize: "9px",
    color: "#6a6a7a",
    textTransform: "uppercase",
    letterSpacing: "0.5px",
  };
  const val = (color: string): React.CSSProperties => ({
    fontSize: "13px",
    fontWeight: 500,
    color: connected ? color : "#6a6a7a",
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
        <div style={val("#e0e0e0")}>{speed}</div>
      </div>
      <div style={cell}>
        <div style={lbl}>Band</div>
        <div style={val(bandColor(live.frequency))}>{band}</div>
        {freq && <div style={{ fontSize: "9px", color: "#6a6a7a" }}>{freq} MHz</div>}
      </div>
      <div style={cell}>
        <div style={lbl}>Channel</div>
        <div style={val("#e0e0e0")}>{channel}</div>
      </div>
    </Focusable>
  );
}
