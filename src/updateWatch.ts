import { toaster } from "@decky/api";
import { Navigation, QuickAccessTab, Router } from "@decky/ui";
import * as backend from "./backend";
import type { UpdateProgress } from "./types";

// Follows a running update and, once the new files are in place, asks Decky
// to reload this plugin, which is what Decky's own store does after an
// install. Restarting plugin_loader instead restarts all of Steam, so the
// update script only falls back to that when no reload happens.
//
// This lives at module level, not in the panel, because the panel cannot be
// relied on while the Quick Access menu is closed (the page is hidden then,
// and it may be unmounted) and the reload must still be asked for.
//
// It also keeps going in a copy of this module that Decky has dismounted.
// An open Quick Access menu can go on showing that copy's panel after a
// reload (and Decky issue #611 can start two copies), so the copy the user
// pressed Update in may well be one of those. Measured on a Deck: with the
// watch tied to the module's lifetime, nothing asked for the reload and the
// update ended in the Steam restart this exists to avoid.

const PLUGIN_NAME = "WiFi Optimizer";
const POLL_MS = 1000;
// The script's own limit is 15 minutes. A watch stops once it has gone this
// long with no new phase reported, counted in time awake: a sleep stops the
// script too, and a watch that counted the sleep stopped before the script
// got to "swapped", so nothing asked for the reload and Steam restarted.
const MAX_WATCH_MS = 15 * 60 * 1000;
// The longest gap between two polls counted as time awake. Anything longer
// was a sleep (or a stalled page), and counts as one poll.
const MAX_TICK_MS = 5 * POLL_MS;
// A "done" this recent, for the version now running, is the update that
// just loaded this copy of the plugin.
const JUST_UPDATED_S = 120;

type Listener = (progress: UpdateProgress | null) => void;

// Shared by every copy of this module on the page.
type Shared = { reloadAsked?: string; announced?: string; noticeShown?: string };
const shared = (): Shared => {
  const w = window as unknown as { __wifiOptimizerUpdate?: Shared };
  w.__wifiOptimizerUpdate = w.__wifiOptimizerUpdate ?? {};
  return w.__wifiOptimizerUpdate;
};

let timer: ReturnType<typeof setInterval> | null = null;
// Time awake since the phase last changed, and the last poll's time.
let idleMs = 0;
let lastTickAt = 0;
let lastPhase = "";
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
  // Before the in-flight check: a progress call that never settles kept
  // that set, and the watch then never stopped.
  const now = Date.now();
  idleMs += Math.min(Math.max(0, now - lastTickAt), MAX_TICK_MS);
  lastTickAt = now;
  if (idleMs > MAX_WATCH_MS) {
    stop();
    return;
  }
  if (inFlight) return;
  inFlight = true;
  try {
    const progress = await backend.getUpdateProgress();
    if (!progress.success) return;
    // Until the script has written anything, the file is either absent or
    // removed by apply_update, so an empty phase is just "starting".
    if (progress.phase && progress.version !== target) return;
    if (progress.phase !== lastPhase) {
      lastPhase = progress.phase;
      idleMs = 0;
    }
    publish(progress);
    // Only while the old backend is still the one answering: once the new
    // version answers, the reload has happened.
    if (progress.phase === "swapped" && progress.running_version !== target) {
      askDeckyToReload(target);
    }
    if (progress.phase === "failed" || progress.phase === "done") {
      stop();
      // Told once. Kept, it was replayed to every panel opened afterwards,
      // so a failed update kept saying so after a successful check.
      current = null;
    }
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
  idleMs = 0;
  lastTickAt = Date.now();
  lastPhase = "";
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

// Telling people an update is out. The panel only checks while it is open,
// and Decky does not announce updates for a plugin installed outside its
// store, so without this an install nobody reopens never hears of one.
// Runs for as long as the plugin is loaded: a first check a couple of
// minutes after load, then twice a day. Each version is announced once,
// remembered by the backend across reboots, and never while a game or a
// stream is running, since a toast there is exactly the interruption this
// plugin exists to avoid. Nothing is installed: the toast opens the panel.
const NOTICE_FIRST_MS = 2 * 60 * 1000;
const NOTICE_EVERY_MS = 12 * 60 * 60 * 1000;
const NOTICE_RETRY_MS = 60 * 60 * 1000;
const NOTICE_BUSY_MS = 10 * 60 * 1000;

function gameRunning(): boolean {
  try {
    return !!Router.MainRunningApp;
  } catch {
    return false;
  }
}

function openPanel() {
  try {
    Navigation.OpenQuickAccessMenu(QuickAccessTab.Decky);
    // Decky's internal state, not part of @decky/api: without it the menu
    // still opens, on whichever plugin was last shown.
    const loader = (window as unknown as {
      DeckyPluginLoader?: { deckyState?: { setActivePlugin?: (name: string) => void } };
    }).DeckyPluginLoader;
    loader?.deckyState?.setActivePlugin?.(PLUGIN_NAME);
  } catch (e) {
    console.error("WiFi Optimizer: could not open the panel", e);
  }
}

// Returns the delay before the next check.
async function noticeOnce(): Promise<number> {
  try {
    const notice = await backend.updateNotice();
    if (notice.retry) return NOTICE_RETRY_MS;
    if (!notice.notify || !notice.version) return NOTICE_EVERY_MS;
    if (notice.busy || gameRunning()) return NOTICE_BUSY_MS;
    // A second copy of the module (Decky issue #611) must not toast too.
    if (shared().noticeShown === notice.version) return NOTICE_EVERY_MS;
    shared().noticeShown = notice.version;
    // Steam shows a toast's body on one line and cuts the rest, so it is
    // kept short; tapping it opens the panel, where the update button is.
    toaster.toast({
      title: PLUGIN_NAME,
      body: `Update available: v${notice.version}`,
      onClick: openPanel,
    });
    await backend.markUpdateNotified(notice.version);
    return NOTICE_EVERY_MS;
  } catch {
    return NOTICE_RETRY_MS;
  }
}

// Starts the checks for this copy of the module; returns how to stop them.
export function startUpdateNotices(): () => void {
  let stopped = false;
  let next: ReturnType<typeof setTimeout> | null = null;
  const schedule = (ms: number) => {
    next = setTimeout(async () => {
      next = null;
      const delay = await noticeOnce();
      if (!stopped) schedule(delay);
    }, ms);
  };
  schedule(NOTICE_FIRST_MS);
  return () => {
    stopped = true;
    if (next) clearTimeout(next);
  };
}

// When Decky dismounts this copy. Nothing is stopped: a watch in progress
// stops by itself (see the top of this file), and a panel still on screen
// keeps its subscription until it unmounts, so it hears how the update ended
// instead of waiting out its timeout and calling a finished update failed.
export function disposeUpdateWatch() {
  if (!timer) listeners.clear();
}
