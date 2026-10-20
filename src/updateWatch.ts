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
//
// It also keeps going in a copy of this module that Decky has dismounted.
// An open Quick Access menu can go on showing that copy's panel after a
// reload (and Decky issue #611 can start two copies), so the copy the user
// pressed Update in may well be one of those. Measured on a Deck: with the
// watch tied to the module's lifetime, nothing asked for the reload and the
// update ended in the Steam restart this exists to avoid.

const PLUGIN_NAME = "WiFi Optimizer";
const POLL_MS = 1000;
// The script's own limit is 15 minutes; a watch never outlives that.
const MAX_WATCH_MS = 15 * 60 * 1000;
// A "done" this recent, for the version now running, is the update that
// just loaded this copy of the plugin.
const JUST_UPDATED_S = 120;

type Listener = (progress: UpdateProgress | null) => void;

// Shared by every copy of this module on the page.
type Shared = { reloadAsked?: string; announced?: string };
const shared = (): Shared => {
  const w = window as unknown as { __wifiOptimizerUpdate?: Shared };
  w.__wifiOptimizerUpdate = w.__wifiOptimizerUpdate ?? {};
  return w.__wifiOptimizerUpdate;
};

let timer: ReturnType<typeof setInterval> | null = null;
let startedAt = 0;
let inFlight = false;
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

function askDeckyToReload(version: string) {
  // Once per update across every copy of the module, so two copies
  // following the same update cannot reload the plugin twice. Cleared when
  // the user starts an update: installing the same version again later in
  // the same Steam session skipped the reload, and Steam restarted.
  if (shared().reloadAsked === version) return;
  shared().reloadAsked = version;
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
  if (inFlight) return;
  if (Date.now() - startedAt > MAX_WATCH_MS) {
    stop();
    return;
  }
  inFlight = true;
  try {
    const progress = await backend.getUpdateProgress();
    if (!progress.success) return;
    // Until the script has written anything, the file is either absent or
    // removed by apply_update, so an empty phase is just "starting".
    if (progress.phase && progress.version !== target) return;
    publish(progress);
    // Only while the old backend is still the one answering: once the new
    // version answers, the reload has happened.
    if (progress.phase === "swapped" && progress.running_version !== target) {
      askDeckyToReload(target);
    }
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
// fresh: a new update the user just started, rather than a copy of the
// module picking up one already running.
export function watchUpdate(version: string, fresh = false) {
  // Both guards are once per version, so a version installed again later in
  // the same Steam session would get neither the reload nor the "Updated"
  // toast. A new update the user started clears them.
  if (fresh) {
    shared().reloadAsked = undefined;
    shared().announced = undefined;
  }
  stop();
  target = version;
  startedAt = Date.now();
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

// On load: pick up an update that is running (started from another copy of
// the module, or before a reload), and say so if one just finished.
export async function checkUpdateOnLoad() {
  try {
    const progress = await backend.getUpdateProgress();
    if (!progress.success || !progress.phase) return;
    const running = progress.running_version;
    if (
      ["downloading", "installing", "swapped"].includes(progress.phase) &&
      progress.version !== running
    ) {
      if (!timer) watchUpdate(progress.version);
      return;
    }
    if (
      (progress.phase === "loaded" || progress.phase === "done") &&
      progress.version === running &&
      progress.age < JUST_UPDATED_S &&
      shared().announced !== progress.version
    ) {
      shared().announced = progress.version;
      toaster.toast({ title: PLUGIN_NAME, body: `Updated to v${progress.version}` });
    }
  } catch {
    // Nothing to pick up or announce.
  }
}

// When Decky dismounts this copy. Nothing is stopped: a watch in progress
// stops by itself (see the top of this file), and a panel still on screen
// keeps its subscription until it unmounts, so it hears how the update ended
// instead of waiting out its timeout and calling a finished update failed.
export function disposeUpdateWatch() {
  if (!timer) listeners.clear();
}
