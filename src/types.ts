export interface PluginSettings {
  model: string;
  driver: string;
  device_family: string;
  device_label: string;
  chip_label: string;
  supports_6ghz: boolean;
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
  cake_enabled: boolean;
  distro_id: string;
  distro_name: string;
  last_connection_uuid: string;
  priority_set: boolean;
  update_channel: string;
  streaming_mode_enabled: boolean;
  streaming_apps: Record<string, boolean>;
  streaming_custom_patterns: string;
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
  bssid_lock_other_network?: boolean;
  /** IPv6 is turned off for a different network, not this one. */
  ipv6_other_network?: boolean;
  ip_address?: string;
  dns?: string;
  ipv6_method?: string;
  band?: string;
  buffer_tuning_applied?: boolean;
  cake_applied?: boolean;
  dispatcher_installed?: boolean;
  last_enforced?: number;
  wifi_backend?: string;
  pins_inactive_iwd?: boolean;
  backend_tool_available?: boolean;
  /** Streaming auto mode is on. */
  streaming_mode?: boolean;
  /** In auto mode: the fixes are on now (a stream runs, or held on). */
  streaming_active?: boolean;
  streaming_held?: boolean;
  /** What was detected: the app's name and the process that matched. */
  streaming_app?: string;
  streaming_process?: string;
}

export interface StreamingApp {
  id: string;
  label: string;
  watched: boolean;
}

export interface StreamingAppList {
  success: boolean;
  apps: StreamingApp[];
}

export interface PluginStatus {
  success: boolean;
  connected: boolean;
  /** First collection has not finished; connected is not yet meaningful. */
  initializing?: boolean;
  support_tier: number;
  version?: string;
  settings: PluginSettings;
  live: LiveStatus;
  drift: Record<string, boolean>;
  // Settings this plugin did not make, found on the connection. Distinct
  // from drift, which is our own setting no longer holding.
  external?: Record<string, boolean>;
  // A stronger access point of this network that only offers WPA2, while the
  // profile is saved as WPA3-only and so cannot use it. Signals are 0-100.
  wpa2_only_stronger?: { signal: number; current_signal: number };
  // The access point lock is holding the device on an access point that a
  // much stronger one of the same network, which the lock could use, has
  // overtaken on two reads in a row. Signals are 0-100. Reported only: the
  // move is the user's to make, through move_bssid_lock.
  lock_weaker?: { signal: number; current_signal: number };
  // NetworkManager saw another device on the network holding our address.
  address_conflict?: { ip: string; other: string };
  // The settings file did not parse and was moved aside to
  // settings.json.corrupt. Reported until the panel acknowledges it.
  settings_recovered?: boolean;
  last_applied?: number;
  error?: string;
  message?: string;
}

export interface MethodResult {
  success: boolean;
  error?: string;
  message?: string;
  detail?: string;
  reconnected?: boolean;
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
  is_downgrade?: boolean;
  channel?: string;
  release_url?: string;
  error?: string;
  message?: string;
}

// What the update script reports while it runs. "swapped" means the new
// files are in place and the plugin should be reloaded.
export type UpdatePhase =
  | ""
  | "downloading"
  | "installing"
  | "swapped"
  | "loaded"
  | "restarting"
  | "done"
  | "failed";

export interface UpdateProgress {
  success: boolean;
  phase: UpdatePhase;
  version: string;
  // Seconds since the script last reported.
  age: number;
  // The version of the backend answering.
  running_version: string;
}

export type BadgeStatus =
  | "active"
  | "drifted"
  | "off"
  | "error"
  | "unknown"
  | "paused";

export const ERROR_MESSAGES: Record<string, string> = {
  no_wifi: "Not connected to WiFi. Connect first, then optimize.",
  iw_failed: "Couldn't change WiFi setting. Try toggling WiFi off/on.",
  nmcli_failed: "Couldn't update connection. Forget and reconnect to this network.",
  write_failed: "Couldn't install auto-fix script. The filesystem may be locked.",
  // Shown while the backend reports settings_recovered.
  parse_error:
    "The settings file could not be read, so settings were reset to defaults. The old file was kept as settings.json.corrupt.",
  unexpected: "Something went wrong. Check the Decky log for details.",
};

export interface RegdomainPhy {
  phy: string;
  self_managed: boolean;
  country: string;
}

export interface RegdomainInfo extends MethodResult {
  readable: boolean;
  // False when the radio carries its own regulatory domain, in which case
  // changing this setting is cosmetic and the panel must say so rather than
  // offering a control that does nothing.
  changeable: boolean;
  self_managed: boolean;
  global: string;
  governing: string;
  phys: RegdomainPhy[];
  setting: string;
}
