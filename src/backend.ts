import { callable } from "@decky/api";
import type {
  PluginStatus,
  MethodResult,
  OptimizeSafeResult,
  UpdateCheckResult,
  UpdateProgress,
  BackendSwitchStartResult,
  BackendSwitchStatus,
  RegdomainInfo,
  StreamingAppList,
} from "./types";

export const getStatus = callable<[], PluginStatus>("get_status");
export const setPowerSave = callable<[disabled: boolean], MethodResult>("set_power_save");
export const setAutoFix = callable<[enabled: boolean], MethodResult>("set_auto_fix");
export const setBssidLock = callable<[enabled: boolean], MethodResult>("set_bssid_lock");
export const setBandPreference = callable<[enabled: boolean, band: string], MethodResult>("set_band_preference");
export const setDns = callable<[enabled: boolean, provider: string, customServers: string], MethodResult>("set_dns");
export const setIpv6 = callable<[disabled: boolean], MethodResult>("set_ipv6");
export const setBufferTuning = callable<[enabled: boolean], MethodResult>("set_buffer_tuning");
export const setCake = callable<[enabled: boolean], MethodResult>("set_cake");
export const optimizeSafe = callable<[], OptimizeSafeResult>("optimize_safe");
export const reapplyAll = callable<[], OptimizeSafeResult>("reapply_all");
export const repairDrifted = callable<[], OptimizeSafeResult>("repair_drifted");
export const reapplyVolatile = callable<[], MethodResult>("reapply_volatile");
export const allowWpa2 = callable<[], MethodResult>("allow_wpa2");
export const resetSettings = callable<[], MethodResult>("reset_settings");
export const setUpdateChannel = callable<[channel: string], MethodResult>("set_update_channel");
export const checkForUpdate = callable<[], UpdateCheckResult>("check_for_update");
export const applyUpdate = callable<[], MethodResult>("apply_update");
export const getUpdateProgress = callable<[], UpdateProgress>("get_update_progress");
export const startBackendSwitch = callable<[backend: string], BackendSwitchStartResult>("start_backend_switch");
export const getBackendSwitchStatus = callable<[], BackendSwitchStatus>("get_backend_switch_status");
export const setStreamingMode = callable<[enabled: boolean], MethodResult>("set_streaming_mode");
export const setStreamingHold = callable<[held: boolean], MethodResult>("set_streaming_hold");
export const setStreamingApp = callable<[appId: string, watched: boolean], MethodResult>("set_streaming_app");
export const setStreamingPatterns = callable<[text: string], MethodResult>("set_streaming_patterns");
export const listStreamingApps = callable<[], StreamingAppList>("list_streaming_apps");
export const pokeDetection = callable<[], MethodResult>("poke_detection");
export const getRegdomain = callable<[], RegdomainInfo>("get_regdomain");
export const setRegdomain = callable<[country: string], MethodResult>("set_regdomain");
export const getDiagnosticInfo = callable<[], Record<string, unknown>>("get_diagnostic_info");
export const saveDiagnosticInfo = callable<[], Record<string, unknown>>("save_diagnostic_info");
