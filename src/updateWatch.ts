import { toaster } from "@decky/api";
import * as backend from "./backend";
import type { UpdateProgress } from "./types";

// Follows a running update and, once the new files are in place, asks Decky
// to reload this plugin, which is what Decky's own store does after an
// install. Restarting plugin_loader instead restarts all of Steam, so the
// update script only falls back to that when no reload happens.
//
// This lives at module level, not in the panel, because closing the Quick
// Access menu unmounts the panel and the reload must still be asked for.

const PLUGIN_NAME = "WiFi Optimizer";
const POLL_MS = 1000;
// A "done" this recent, for the version now running, is the update that
// just loaded this copy of the plugin.
const JUST_UPDATED_S = 120;

type Listener = (progress: UpdateProgress | null) => void;

let timer: ReturnType<typeof setInterval> | null = null;
let inFlight = false;
let reloadAsked = false;
let disposed = false;
let target = "";
let current: UpdateProgress | null = null;
const listeners = new Set<Listener>();

function publish(progress: UpdateProgress | null) {
  current = progress;
  listeners.forEach((fn) => fn(progress));
}

function stop() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

function askDeckyToReload() {
  if (reloadAsked || disposed) return;
  reloadAsked = true;
  // Out of the poll's own call, since the reload tears this module down.
  // DeckyBackend is Decky's internal client, not part of @decky/api, so a
  // Decky without it is expected; the script restarts plugin_loader then.
  setTimeout(() => {
    const deckyBackend = (window as unknown as {
      DeckyBackend?: { call?: (route: string, ...args: unknown[]) => Promise<unknown> };
    }).DeckyBackend;
    if (typeof deckyBackend?.call !== "function") {
      console.warn("WiFi Optimizer: Decky cannot reload a plugin here, waiting for a restart");
      return;
    }
    Promise.resolve(deckyBackend.call("loader/reload_plugin", PLUGIN_NAME)).catch((e) =>
      console.error("WiFi Optimizer: reload request failed, waiting for a restart", e),
    );
  }, 0);
}

async function poll() {
  if (inFlight || disposed) return;
  inFlight = true;
  try {
    const progress = await backend.getUpdateProgress();
    if (disposed || !progress.success) return;
    // Until the script has written anything, the file is either absent or
    // removed by apply_update, so an empty phase is just "starting".
    if (progress.phase && progress.version !== target) return;
    publish(progress);
    if (progress.phase === "swapped") askDeckyToReload();
    if (progress.phase === "failed" || progress.phase === "done") stop();
  } catch (e) {
    // A reload or restart cancels calls in flight; that is the update
    // working, and the next copy of the plugin takes over from here.
    console.debug("WiFi Optimizer: update progress unavailable", e);
  } finally {
    inFlight = false;
  }
}

// Called once apply_update has started the script for this version.
export function watchUpdate(version: string) {
  if (disposed) return;
  stop();
  target = version;
  reloadAsked = false;
  publish(null);
  timer = setInterval(poll, POLL_MS);
}

export function subscribeUpdate(fn: Listener): () => void {
  listeners.add(fn);
  fn(current);
  return () => {
    listeners.delete(fn);
  };
}

// On load: say so if the update that just finished is what loaded us. Keyed
// on the window, since a reload can start this module twice (Decky #611).
export async function announceIfJustUpdated() {
  try {
    const progress = await backend.getUpdateProgress();
    const w = window as unknown as { __wifiOptimizerAnnounced?: string };
    if (
      !disposed &&
      progress.success &&
      (progress.phase === "loaded" || progress.phase === "done") &&
      progress.version === progress.running_version &&
      progress.age < JUST_UPDATED_S &&
      w.__wifiOptimizerAnnounced !== progress.version
    ) {
      w.__wifiOptimizerAnnounced = progress.version;
      toaster.toast({ title: PLUGIN_NAME, body: `Updated to v${progress.version}` });
    }
  } catch {
    // Nothing to announce.
  }
}

// A reload can start this module twice (Decky issue #611), so everything it
// started has to stop when Decky dismounts it.
export function disposeUpdateWatch() {
  disposed = true;
  stop();
  listeners.clear();
}
