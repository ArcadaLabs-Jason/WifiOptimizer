# WiFi Optimizer v0.24.5-beta

> **Heads up:** This plugin modifies WiFi and network settings. Some optimizations (band preference, custom DNS, WiFi backend switch) can temporarily prevent WiFi from connecting. If this happens, a reboot usually fixes it. You can also try forgetting and rejoining your WiFi network from Steam settings.

A [Decky Loader](https://decky.xyz/) plugin that fixes WiFi problems that cause lag, stuttering, and dropped connections during game streaming. It helps any streaming over WiFi: Steam Remote Play, [Moonlight](https://moonlight-stream.org/) / [Sunshine](https://app.lizardbyte.dev/Sunshine/), Parsec, Chiaki (PS Remote Play), and more.

**Supported devices:** Steam Deck (LCD and OLED), Legion Go, ROG Ally, and other PC handhelds. Works on SteamOS, Bazzite, and CachyOS.

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/fanciestpeanut)

## The problem

The OS resets WiFi settings after every system update and sleep/wake cycle. Power management gets re-enabled and network buffers reset to defaults. The result: latency spikes, connection drops, and degraded streaming quality, and the only fix is a trip to Desktop Mode.

WiFi Optimizer fixes this from Game Mode. One tap, and it stays fixed.

## Install / Update

[Decky Loader](https://decky.xyz/) must be installed first. Then pick one:

**From Decky, no Desktop Mode needed.** In Decky's settings, under **General**, turn on **Developer mode**. Then open the **Developer** tab, enter this under **Install Plugin from URL** and tap **Install**:

```
https://github.com/ArcadaLabs-Jason/WifiOptimizer/releases/latest/download/wifi-optimizer.zip
```

That link always points at the latest stable release. You can also download `wifi-optimizer.zip` from the [releases page](https://github.com/ArcadaLabs-Jason/WifiOptimizer/releases) and use **Install Plugin from ZIP File** > **Browse** instead. The zip is listed in each release's signed checksums and has its own VirusTotal scan, so you can check it before installing ([SECURITY.md](SECURITY.md)). Decky shows its standard warning for plugins that aren't from its store; tap **Continue**, then **Install**. Installing the zip over an existing copy keeps your settings, but for updates the update button in the plugin is better: it checks the release signature before installing, and Decky's zip install doesn't.

**From Konsole.** Open Desktop Mode > Konsole and run:

```bash
curl -sL https://github.com/ArcadaLabs-Jason/WifiOptimizer/raw/main/install.sh -o /tmp/wifi-opt-install.sh && sudo bash /tmp/wifi-opt-install.sh
```

This requires a user password. Set one with `passwd` in Konsole if you haven't already. It keeps your settings if the plugin is already installed.

Switch back to Game Mode. Open the Quick Access Menu (**...** button) > Decky > WiFi Optimizer.

**Updating:** The plugin checks for updates automatically when you open it, and from v0.24.0 on also twice a day in the background: when a new version comes out you get one notification (never during a game or a stream), and tapping it opens the plugin. You can turn that off with **Tell me about updates** in the Updates section. Nothing installs on its own. If an update is available, an update button appears at the top of the panel. Tap it and the plugin updates and reloads itself, without restarting Steam. If Decky can't reload it, the plugin restarts Decky instead, which restarts Steam. The button won't start an update while a game or a stream is running; close it first. You can also update manually by running the install command above again. That always installs the latest stable release, so on the beta channel it can take you back to an older version.

Releases are signed. From v0.21.0-beta on, before installing an update, the plugin checks the release's signature against the keys built into it and the download against the signed checksum, and installs nothing that doesn't match. Earlier versions install what they download. The install command doesn't check signatures: it downloads the latest stable release over HTTPS from GitHub, or the current main branch if it can't look up the release. [SECURITY.md](SECURITY.md) lists the keys and how to check a release yourself. The beta channel installs the newest release of either kind, beta or stable.

> **Note:** If you're on a version before v0.6.6, the in-app updater had bugs. Please update manually using the install command above to get working auto-updates.

## Getting started

1. Open WiFi Optimizer from the Decky sidebar
2. Tap **Optimize Safe**. This applies four optimizations that suit most setups (three when the WiFi backend is `iwd`, where the BSSID lock is left out):
   - Disables WiFi power save and PCIe power states (prevents lag spikes and streaming degradation)
   - Locks you to one access point (stops background scanning interruptions, but also stops roaming between access points, see below). If the lock is already on for another network, it is left there
   - Enables auto-fix on wake (reapplies settings after sleep)
   - Tunes network buffers (handles streaming traffic bursts)
3. That's it. The plugin maintains these settings automatically, even after sleep/wake and OS updates.

Want to go further? The remaining optimizations are available as individual toggles. Each one has an **(i)** icon you can tap for a full explanation of what it does and any tradeoffs, and **Show details** at the top of the panel opens all of them at once, for reading with a controller. Advanced options include forcing 5 GHz (or 6 GHz, where your setup supports it), custom DNS, disabling IPv6, CAKE traffic shaping, and switching between the `iwd` and `wpa_supplicant` WiFi backends.

## All optimizations

**Safe tier (applied by Optimize Safe)**

| Optimization | What it does |
|---|---|
| Prevent lag spikes | Disables WiFi power management and PCIe power states that cause packet batching, latency spikes, and throughput degradation during sustained streaming. |
| Stop background scanning | Pins your connection to one access point so your device stops scanning for other networks every few minutes. On a mesh it pins the strongest access point your network offers rather than whichever one you happen to be on, preferring 5 GHz where the signal is equal, and stays put if the one you are on is already healthy. If the stronger one turns out not to accept the connection, the previous one is restored. After that the lock keeps you on that one access point, so if you carry your device somewhere a much stronger one is in range, the panel says so and **Move the lock** moves it. It never moves on its own, since moving reconnects. If the pinned access point later stops being reachable and the device has been offline for a minute, the pin is released so you can connect again, and reapplied to whichever access point works. It only applies to the network it was turned on for, so other networks you join are left alone. Disable it if you need to roam between access points. Needs the `wpa_supplicant` backend: under `iwd`, NetworkManager can't reconnect a network pinned this way, so the lock is paused, and its pins removed, while `iwd` is in use. |
| Auto-fix on wake | Installs a script that reapplies your settings every time WiFi reconnects. Works even if Decky isn't running. |
| Network buffer tuning | Increases kernel buffer sizes and TX queue length to handle bursty streaming traffic without dropping packets |

**Manual opt-in (require configuration or have tradeoffs)**

| Optimization | What it does | Why it's manual |
|---|---|---|
| Force 5 GHz band | Locks WiFi to 5 GHz to avoid Bluetooth interference on 2.4 GHz | No use on a 2.4 GHz only network, and keeps a 6 GHz capable device off 6 GHz. Refused when no 5 GHz access point for your network is in range. Paused while the `iwd` backend is in use, for the same reason as the access point lock. |
| Force 6 GHz band | Locks WiFi to 6 GHz, the least crowded band, on routers with WiFi 6E or newer | Only shown on a 6 GHz capable card with NetworkManager 1.58 or newer (expected with SteamOS 3.9) and the `wpa_supplicant` backend. Needs WPA3: a network saved for WPA2 works from NetworkManager 1.58.1. Refused when no 6 GHz access point for your network is in range, and if the device can't connect on 6 GHz the band is put back as it was. 6 GHz reaches less far than 5 GHz; out of range, the pin is released after a minute like the others. |
| Traffic shaping (CAKE) | Replaces the default queue for traffic your device sends with CAKE, for fair queuing and ACK filtering. It cannot fix lag that starts at your router or internet connection. Does not limit bandwidth. | Replaces your system's default qdisc; resets on reboot |
| Custom DNS | Overrides your ISP's DNS with Cloudflare, Google, Quad9, or custom servers | Requires choosing a provider |
| Disable IPv6 | Forces all traffic through IPv4 | Only helps on networks with broken IPv6. Most are fine. |
| WiFi region | Sets the wireless regulatory domain, which decides the channels your device may use. The wrong region can hide 5 GHz and 6 GHz channels entirely. | Only offered on devices whose WiFi card does not carry its own region. Steam Deck cards do, so the panel reports the region in use and offers no control there. |
| WiFi backend (iwd / wpa_supplicant) | Switches between `iwd` and `wpa_supplicant`. Some devices are more stable with wpa_supplicant across sleep/wake and 5 GHz. On SteamOS 3.8 this goes through the same system service as the setting in SteamOS Settings, so the two agree. Switching to `iwd` pauses the access point lock and Force 5 GHz. | Only shown when the system provides a way to switch; some networks (certain WPA3, enterprise setups) behave differently between the two |

**Streaming auto mode**

Off by default. When it's on, Prevent lag spikes, Network buffer tuning and Traffic shaping (CAKE) apply only while a streaming app is running, and come off when it exits, so the rest of the time your device saves battery as it normally would. Background scanning, Force 5 GHz, DNS and IPv6 stay as they are, because changing them reconnects WiFi and would drop the stream.

It watches for Moonlight, GeForce NOW, Chiaki (PS Remote Play), Steam Remote Play, Steam Link, Greenlight (Xbox), Parsec and Xbox Cloud Gaming, each of which can be unticked, plus any process names you add. The top of the panel always says which state you're in. If a stream isn't picked up, turn on **Keep fixes on**, which holds the fixes on until you restart, or add the app's process name.

Idea and first implementation by [bassobr](https://github.com/bassobr) ([#5](https://github.com/ArcadaLabs-Jason/WifiOptimizer/issues/5)).

## Hardware support

| Device | WiFi Chip | Driver | Notes |
|---|---|---|---|
| Steam Deck LCD | WiFi 5 (RTL8822CE) | rtw88 | Full support |
| Steam Deck OLED | WiFi 6E (QCA206X) | ath11k_pci | Full support |
| Legion Go (all models) | WiFi 6E (MT7922) | mt7921e | Full support |
| ROG Ally (all models) | WiFi 6E (MT7922) | mt7921e | Full support |
| MSI Claw, AYANEO, GPD | WiFi 6 (RTL8852) or WiFi 7 (WCN7850) | rtw89, ath12k | Recognized; untested on hardware, reports welcome |
| Other PC handhelds | Varies | iwlwifi, etc. | Detected automatically; driver-specific fixes applied when available |

The plugin detects your WiFi hardware at startup and applies the right optimizations for your chip. Devices with unrecognized hardware still get universal optimizations (power save, buffer tuning, BSSID lock, etc.).

## How it works

The plugin has two parts:

1. **The Decky plugin** runs in the Quick Access Menu. It applies optimizations when you toggle them and shows live status (signal, speed, frequency, channel). It notices when settings have drifted, after wake or because something else changed them: most are restored on their own, and anything left is one tap away.

2. **A NetworkManager dispatcher script** runs independently of Decky, outside of Steam. Every time your WiFi reconnects (including after sleep), it automatically reapplies the volatile settings (power save, PCIe power states, buffers, CAKE). In streaming auto mode it only does that while a stream is running. If you uninstall the plugin, the script removes itself.

The panel polls for live status only while it is open. A few things run
without it: the dispatcher script, whenever WiFi reconnects; a reapply of the
volatile settings shortly after a game launches; a check every 20 seconds,
while the access point lock or a band preference is on, that releases a pin
keeping the device offline; and the update check twice a day, unless **Tell me
about updates** is off. In streaming auto mode the plugin also checks every few
seconds whether a streaming app is running.

## Send report

**Send report to the developer**, next to **Copy diagnostics** at the bottom of
the panel, sends the same report that Copy diagnostics copies: your device and
WiFi driver, the plugin's settings, and the WiFi parts of the system log from
this boot and the one before, with network names and addresses already taken
out. Nothing is sent until you have seen it: the panel shows you the whole
report first, and it only goes if you confirm with **Send**. It goes to the
developer and nowhere else, and is kept for 90 days. You get a reference like
WO-7K2M9Q back; quote it if you post about the problem.

With no connection, the report is saved where only root can read it
(`/var/lib/wifi-optimizer`) and sent once WiFi is back. Only one waits at a
time, a newer one replaces it, and one still waiting after two weeks is
dropped. Uninstalling removes it.

If the last time your device woke from sleep WiFi never came back (the
previous boot's log shows the wake, errors from the WiFi driver, and no
reconnection after it), the panel says so once and offers to send a report.
Only that it happened and when are shown; the log itself goes nowhere unless
you send a report.

## Uninstall

**Before uninstalling:** tap **Reset Settings** in the plugin's Actions section. This undoes buffer tuning and CAKE where they were applied, putting back the values your system had before, and deletes the plugin's own config files. WiFi power save goes back on straight away (unless Steam's own WiFi power management setting has it off), the driver's power settings go back to what your system had, and a WiFi region chosen in the plugin is put back. Prevent lag spikes then stays off until you turn it on again. The BSSID lock, the band preference, the IPv6 setting and custom DNS are undone on every network the plugin applied them to, because a network still pinned to one access point, or to a band it cannot find, stops connecting. If a backend switch made from the plugin copied its power save setting into SteamOS's own WiFi config, that is put back too. On SteamOS the WiFi backend choice (iwd vs wpa_supplicant) is Valve's own setting and is left as it is. On other systems, a backend chosen from the plugin goes back to the system's default from the next boot, and that backend's service is turned back on so WiFi still works. One thing is left in place: where a network is saved more than once, the plugin gives the copy you use a higher autoconnect priority so the same copy is picked every time, and that priority stays, since it does no harm. Uninstalling from Decky does the same.

Then uninstall from Decky's plugin manager (Decky settings > WiFi Optimizer > Uninstall), or manually:

```bash
sudo rm -rf ~/homebrew/plugins/WiFi\ Optimizer
sudo rm -f /etc/NetworkManager/dispatcher.d/99-wifi-optimizer
sudo rm -f /etc/NetworkManager/conf.d/99-wifi-optimizer.conf
sudo rm -f /etc/modprobe.d/99-wifi-optimizer.conf
sudo nmcli general reload conf
rm -rf ~/homebrew/settings/WiFi\ Optimizer
sudo systemctl restart plugin_loader
```

Restart the device afterwards: WiFi power save, the driver's power settings and the PCIe power states go back to your system's own from the next boot.

If you switched the WiFi backend from the plugin on a system other than SteamOS, switch it back in the plugin before removing it by hand, or leave `/etc/NetworkManager/conf.d/99-wifi-optimizer-backend.conf` in place: removing that file alone leaves the other backend turned off.

## Building from source

Requires Node.js 22 and pnpm 10.

```bash
git clone https://github.com/ArcadaLabs-Jason/WifiOptimizer.git
cd WifiOptimizer
pnpm i
pnpm run build
```

To deploy to your Deck, copy `.vscode/defsettings.json` to `.vscode/settings.json`, fill in your Deck's IP and password, then use the VS Code **builddeploy** task (Terminal > Run Task).

## Support

WiFi Optimizer is free and stays free. If you want to support it:
[ko-fi.com/fanciestpeanut](https://ko-fi.com/fanciestpeanut).

## Contact

Follow development on [Bluesky](https://bsky.app/profile/thefanciestpeanut.bsky.social).

## License

BSD 3-Clause. See [LICENSE](LICENSE).

### Third-party code

The built frontend (`dist/index.js`) includes code from these libraries, under their own licenses:

- [@decky/api](https://github.com/SteamDeckHomebrew/loader-api), LGPL-2.1
- [react-icons](https://github.com/react-icons/react-icons), MIT

The panel also uses [@decky/ui](https://github.com/SteamDeckHomebrew/decky-frontend-lib) (LGPL-2.1), which Decky Loader provides at runtime rather than this bundle, and [tslib](https://github.com/microsoft/tslib) (0BSD) is installed with the build tools, though none of it ends up in the bundle.
