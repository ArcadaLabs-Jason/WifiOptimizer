import { callable } from "@decky/api";
import type { PluginStatus, MethodResult, OptimizeSafeResult } from "./types";

export const getStatus = callable<[], PluginStatus>("get_status");
export const setPowerSave = callable<[disabled: boolean], MethodResult>("set_power_save");
export const setAutoFix = callable<[enabled: boolean], MethodResult>("set_auto_fix");
export const setBssidLock = callable<[enabled: boolean], MethodResult>("set_bssid_lock");
export const setBandPreference = callable<[enabled: boolean, band: string], MethodResult>("set_band_preference");
export const setDns = callable<[enabled: boolean, provider: string, customServers: string], MethodResult>("set_dns");
export const setIpv6 = callable<[disabled: boolean], MethodResult>("set_ipv6");
export const setBufferTuning = callable<[enabled: boolean], MethodResult>("set_buffer_tuning");
export const optimizeSafe = callable<[], OptimizeSafeResult>("optimize_safe");
export const reapplyAll = callable<[], OptimizeSafeResult>("reapply_all");
export const resetSettings = callable<[], MethodResult>("reset_settings");
