export interface PluginSettings {
  model: string;
  driver: string;
  power_save_disabled: boolean;
  auto_fix_on_wake: boolean;
  bssid_lock_enabled: boolean;
  bssid_lock_value: string;
  bssid_lock_connection_uuid: string;
  band_preference: string;
  band_preference_enabled: boolean;
  dns_provider: string;
  dns_servers: string;
  dns_enabled: boolean;
  ipv6_disabled: boolean;
  buffer_tuning_enabled: boolean;
  last_connection_uuid: string;
  priority_set: boolean;
  update_channel: string;
  last_applied: number;
}

export interface LiveStatus {
  power_save_off?: boolean;
  signal_dbm?: string;
  tx_bitrate?: string;
  frequency?: string;
  channel?: string;
  connected_bssid?: string;
  bssid_lock?: string;
  ip_address?: string;
  dns?: string;
  ipv6_method?: string;
  band?: string;
  buffer_tuning_applied?: boolean;
  dispatcher_installed?: boolean;
  last_enforced?: number;
  wifi_backend?: string;
  backend_tool_available?: boolean;
}

export interface PluginStatus {
  success: boolean;
  connected: boolean;
  supported: boolean;
  version?: string;
  settings: PluginSettings;
  live: LiveStatus;
  drift: Record<string, boolean>;
  last_applied?: number;
  error?: string;
  message?: string;
}

export interface MethodResult {
  success: boolean;
  error?: string;
  message?: string;
  detail?: string;
  [key: string]: unknown;
}

export interface OptimizeSafeResult extends MethodResult {
  total: number;
  applied: number;
  results: Record<string, MethodResult>;
}

export type BackendSwitchPhase =
  | "idle"
  | "switching"
  | "reconnecting"
  | "done"
  | "failed";

export interface BackendSwitchResult {
  success: boolean;
  backend?: string | null;
  target: string;
  recovery_performed?: boolean;
  needs_reboot?: boolean;
  reconnect_timed_out?: boolean;
  message?: string;
  detail?: string;
}

export interface BackendSwitchStatus {
  success: boolean;
  in_progress: boolean;
  phase: BackendSwitchPhase;
  target: string | null;
  started_at: number;
  result: BackendSwitchResult | null;
  message?: string;
}

export interface BackendSwitchStartResult {
  accepted: boolean;
  reason?: string;
  message?: string;
  target?: string;
  from?: string | null;
  backend?: string;
}

export interface UpdateCheckResult {
  success: boolean;
  current_version?: string;
  latest_version?: string;
  update_available?: boolean;
  channel?: string;
  release_url?: string;
  error?: string;
  message?: string;
}

export type BadgeStatus = "active" | "locked" | "set" | "drifted" | "off" | "error" | "unknown";

export const ERROR_MESSAGES: Record<string, string> = {
  no_wifi: "Not connected to WiFi. Connect first, then optimize.",
  iw_failed: "Couldn't change WiFi setting. Try toggling WiFi off/on.",
  nmcli_failed: "Couldn't update connection. Forget and reconnect to this network.",
  timeout: "Command timed out. The system may be busy. Try again.",
  write_failed: "Couldn't install auto-fix script. The filesystem may be locked.",
  parse_error: "Settings were reset to defaults.",
  unexpected: "Something went wrong. Check the Decky log for details.",
};
