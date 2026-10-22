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

That link always points at the latest stable release. You can also download `wifi-optimizer.zip` from the [releases page](https://github.com/ArcadaLabs-Jason/WifiOptimizer/releases) and use **Install Plugin from ZIP File** > **Browse** instead. The zip is listed in each release's signed checksums and has its own VirusTotal scan, so you can check it before installing ([SECURITY.md](SECURITY.md)). Decky shows its standard warning for plugins that aren't from its store; tap **Continue**, then **Install**. With a Decky Loader release from late 2024 or newer, installing the zip over an existing copy keeps your settings (some Decky builds from earlier in 2024 removed the plugin first, settings included). For updates, though, the update button in the plugin is better: it checks the release signature before installing, and Decky's zip install doesn't.

**From Konsole.** Open Desktop Mode > Konsole and run:

```bash
curl -fsSL https://github.com/ArcadaLabs-Jason/WifiOptimizer/raw/main/install.sh -o /tmp/wifi-opt-install.sh && sudo bash /tmp/wifi-opt-install.sh
```

This requires a user password. Set one with `passwd` in Konsole if you haven't already. It keeps your settings if the plugin is already installed.

Switch back to Game Mode. Open the Quick Access Menu (**...** button) > Decky > WiFi Optimizer.

**Updating:** The plugin checks for updates automatically when you open it, and from v0.24.0-beta on also twice a day in the background: when a new version comes out you get one notification (never during a game or a stream), and tapping it opens the plugin. You can turn that off with **Tell me about updates** in the Updates section. Nothing installs on its own. If an update is available, an update button appears at the top of the panel. Tap it and the plugin updates. From v0.19.0-beta on it then reloads itself without restarting Steam; if Decky can't reload it, the plugin restarts Decky instead, which restarts Steam. Earlier versions restart Decky, and with it Steam, every time. From v0.24.2-beta on, the button won't start an update while a game or a stream is running, or while **Keep fixes on** is on in streaming auto mode; close the game or stream, or turn Keep fixes on off, first. You can also update manually by running the install command above again. That always installs the latest stable release, so on the beta channel it can take you back to an older version.

Releases are signed. From v0.21.0-beta on, before installing an update, the plugin checks the release's signature against the keys built into it and the download against the signed checksum, and installs nothing that doesn't match. Earlier versions install what they download. The install command doesn't check signatures: it downloads the latest stable release over HTTPS from GitHub, or the current main branch if it can't look up the release. [SECURITY.md](SECURITY.md) lists the keys and how to check a release yourself. The beta channel installs the newest release of either kind, beta or stable.

> **Note:** If you're on a version before v0.6.6, the in-app updater had bugs. Please update manually using the install command above to get working auto-updates.

## Getting started

1. Open WiFi Optimizer from the Decky sidebar
2. Two of the optimizations are already on from the moment the plugin is installed: **Prevent lag spikes** (WiFi power save and PCIe power states off) and **Auto-fix on wake**. Turn either off in the panel if you don't want it.
3. Tap **Optimize Safe** to add the rest of the four that suit most setups. Between them they:
   - Disable WiFi power save and PCIe power states (prevents lag spikes and streaming degradation)
   - Lock you to one access point (stops background scanning interruptions, but also stops roaming between access points, see below). If the lock is already on for another network, it is left there. Under the `iwd` WiFi backend the lock is left out.
   - Enable auto-fix on wake (reapplies settings after sleep)
   - Tune network buffers (handles streaming traffic bursts)
4. That's it. The plugin maintains these settings automatically, even after sleep/wake and OS updates.

Want to go further? The remaining optimizations are available as individual toggles. Each one has an **(i)** icon you can tap for a full explanation of what it does and any tradeoffs, and **Show details** at the top of the panel opens all of them at once, for reading with a controller. Advanced options include forcing 5 GHz (or 6 GHz, where your setup supports it), custom DNS, disabling IPv6, CAKE traffic shaping, and switching between the `iwd` and `wpa_supplicant` WiFi backends.

## All optimizations

**Safe tier (applied by Optimize Safe)**

| Optimization | What it does |
|---|---|
| Prevent lag spikes | Disables WiFi power management and PCIe power states that cause packet batching, latency spikes, and throughput degradation during sustained streaming. On some cards part of it needs a restart to take effect. |
| Stop background scanning | The access point lock. Pins your connection to one access point so your device stops scanning now and then for a better access point of your network. On a mesh it pins the strongest access point your network offers rather than whichever one you happen to be on, preferring 5 GHz where the signal is equal, and stays put if the one you are on is already healthy. If the stronger one turns out not to accept the connection, the previous one is restored. After that the lock keeps you on that one access point, so if you carry your device somewhere a much stronger one is in range, the panel says so and **Move the lock** moves it. Since moving reconnects, it only moves on its own after a wake, and only from a weak access point to a clearly stronger one, never during a game or stream. If the pinned access point later stops being reachable and the device has been offline for a minute, the pin is released so you can connect again, and reapplied to whichever access point works. It only applies to the network it was turned on for, so other networks you join are left alone. Disable it if you need to roam between access points. Needs the `wpa_supplicant` backend: under `iwd`, NetworkManager can't reconnect a network pinned this way, so the lock is paused, and its pins removed, while `iwd` is in use. |
| Auto-fix on wake | Installs a script that reapplies your settings every time WiFi reconnects. Works even if Decky isn't running. |
| Network buffer tuning | Increases kernel buffer sizes and TX queue length to handle bursty streaming traffic without dropping packets |

**Manual opt-in (require configuration or have tradeoffs)**

| Optimization | What it does | Why it's manual |
|---|---|---|
| Force 5 GHz band | The band preference. Locks WiFi to 5 GHz to avoid Bluetooth interference on 2.4 GHz | No use on a 2.4 GHz only network, and keeps a 6 GHz capable device off 6 GHz. Refused when no 5 GHz access point for your network is in range. Paused while the `iwd` backend is in use, for the same reason as the access point lock. |
| Force 6 GHz band | The band preference, for 6 GHz. Locks WiFi to 6 GHz, the least crowded band, on routers with WiFi 6E or newer | Only shown on a 6 GHz capable card with NetworkManager 1.58 or newer (expected with SteamOS 3.9) and the `wpa_supplicant` backend. Needs WPA3: a network saved for WPA2 works from NetworkManager 1.58.1. On 1.58.0, set your router to WPA3 only, then forget the network and join it again so it is saved for WPA3. Refused when no 6 GHz access point for your network is in range, and if the device can't connect on 6 GHz the band is put back as it was. 6 GHz reaches less far than 5 GHz; out of range, the pin is released after a minute like the others. |
| Traffic shaping (CAKE) | Replaces the default queue for traffic your device sends with CAKE, for fair queuing and ACK filtering. It cannot fix lag that starts at your router or internet connection. Does not limit bandwidth. | Replaces your system's default queue; resets on reboot |
| Custom DNS | Overrides your ISP's DNS with Cloudflare, Google, Quad9, or custom servers. It replaces the network's own DNS servers, including any it offers over IPv6, with IPv4 ones, so leave it off on the rare network that has no IPv4 at all. | Requires choosing a provider |
| Disable IPv6 | The IPv6 setting. Forces all traffic through IPv4 | Only helps on networks with broken IPv6. Most are fine. |
| Allow WPA2 here | Offered only when a stronger access point of your network offers just WPA2 while the network is saved as WPA3-only, so your device can't use it. Allowing it lets the device use that access point; ones that offer both keep using WPA3. | Only for an access point that is yours: anyone nearby can broadcast your network's name. The network then accepts WPA2 from any access point with its name, which makes its password easier to attack. Before NetworkManager 1.58.1 a network allowed WPA2 doesn't join an access point that offers only WPA3 by itself, so there it is only offered while no such access point is in view. Reset puts the network back to WPA3 only if WPA2 was allowed with version 0.24.5-beta or later and you haven't gone back to an older version since. Otherwise see [Uninstall](#uninstall) for the command that does it. |
| WiFi region | Sets the wireless regulatory domain, which decides the channels your device may use. The wrong region can hide 5 GHz and 6 GHz channels entirely. | Only offered on devices whose WiFi card does not carry its own region. Some cards carry their own region, the Steam Deck OLED's among them; there the panel reports the region in use and offers no control. |
| WiFi backend (iwd / wpa_supplicant) | Switches between `iwd` and `wpa_supplicant`. Some devices are more stable with wpa_supplicant across sleep/wake and 5 GHz. On SteamOS 3.8 this goes through the same system service as the setting in SteamOS Settings, so the two agree. Switching to `iwd` pauses the access point lock and the band preference. | Only shown when the system provides a way to switch; some networks (certain WPA3, enterprise setups) behave differently between the two |

**Streaming auto mode**

Off by default. When it's on, Prevent lag spikes, Network buffer tuning and Traffic shaping (CAKE) apply only while a streaming app is running, and come off when it exits, so the rest of the time your device saves battery as it normally would. Background scanning, Force 5 GHz, DNS and IPv6 stay as they are, because changing them reconnects WiFi and would drop the stream.

On Realtek, MediaTek and Intel cards (the Steam Deck LCD, ROG Ally and Legion Go among them), driver settings that only apply at startup are not in effect in auto mode. On the Steam Deck LCD these include the ones that help WiFi come back after sleep.

It watches for Moonlight, GeForce NOW, Chiaki (PS Remote Play), Steam Remote Play, Steam Link, Greenlight (Xbox), Parsec and Xbox Cloud Gaming, each of which can be unticked, plus any process names you add. The top of the panel always says which state you're in. If a stream isn't picked up, turn on **Keep fixes on**, which holds the fixes on until you restart the device, or add the app's process name.

Idea and first implementation by [bassobr](https://github.com/bassobr) ([#5](https://github.com/ArcadaLabs-Jason/WifiOptimizer/issues/5)).

## Hardware support

| Device | WiFi Chip | Driver | Notes |
|---|---|---|---|
| Steam Deck LCD | WiFi 5 (RTL8822CE) | rtw88 | Full support |
| Steam Deck OLED | WiFi 6E (QCA206X) | ath11k_pci | Full support |
| Legion Go (all models) | WiFi 6E (MT7922) | mt7921e | Full support |
| ROG Ally (all models) | WiFi 6E (MT7922) | mt7921e | Full support |
| MSI Claw, AYANEO, GPD | Varies (often MediaTek MT7922 or Intel) | mt7921e, iwlwifi, rtw89, ath12k | Recognized; untested on hardware, reports welcome |
| Other PC handhelds | Varies | iwlwifi, etc. | Detected automatically; driver-specific fixes applied when available |

The plugin detects your WiFi hardware at startup and applies the right optimizations for your chip. Devices with unrecognized hardware still get universal optimizations (power save, buffer tuning, the access point lock, etc.).

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

## Uninstall

**Before uninstalling:** tap **Reset settings** in the plugin's Actions section. This undoes buffer tuning and CAKE where they were applied, putting back the values your system had before, and removes the plugin's power save config files. WiFi power save goes back on straight away (unless Steam's own WiFi power management setting has it off), the driver's power settings go back to what your system had, and a WiFi region chosen in the plugin is put back. Prevent lag spikes then stays off until you turn it on again. The access point lock, the band preference, the IPv6 setting, custom DNS and Allow WPA2 (when it was turned on with version 0.24.5-beta or later, and not lost by going back to an older version in between) are taken off every network the plugin applied them to, because a network still pinned to one access point, or to a band it cannot find, stops connecting; the connection in use keeps them until WiFi next connects. If a backend switch made from the plugin copied its power save setting into SteamOS's own WiFi config, that is put back too. On SteamOS the WiFi backend choice (iwd vs wpa_supplicant) is Valve's own setting and is left as it is. On other systems, a backend chosen from the plugin goes back to the system's default from the next boot, and that backend's service is turned back on so WiFi still works. Streaming auto mode is turned off and its app choices and process names are cleared. Your update channel and update notices are kept. Reset leaves Auto-fix on wake on, as on a fresh install, so its script stays installed, but with the other fixes off it changes nothing. One more thing is left in place: where a network is saved more than once, the plugin gives the copy you use a higher autoconnect priority so the same copy is picked every time, and that priority stays, since it does no harm. Uninstalling from Decky does the same as Reset, and also removes the auto-fix script, as long as the plugin is enabled in Decky (see below).

Allow WPA2 turned on with a version before 0.24.5-beta wasn't recorded, so Reset and uninstalling can't put those networks back to WPA3 only, and the same goes for one turned on later if you went back to an older version in between. To do it yourself, run this in Desktop Mode's Konsole for each such network, with the network's saved name in place of `NAME`. Only do it for a network that was WPA3 only before you allowed WPA2: on a network that only offers WPA2, the device would stop connecting. It applies from the next time WiFi connects.

```bash
sudo nmcli connection modify "NAME" 802-11-wireless-security.key-mgmt sae
```

Disabling the plugin in Decky stops the plugin and its auto-fix script, but the files behind Prevent lag spikes stay in force: WiFi power save stays off at every connect, and the WiFi driver's power settings load at every restart. To run without any of the plugin's changes, tap **Reset settings** first, then disable it, then restart the device.

If the plugin is disabled in Decky, enable it again, tap **Reset settings**, then uninstall: Decky doesn't run a disabled plugin's uninstall, so nothing would be undone. If you already uninstalled it while disabled, install it again (its settings are still there), tap **Reset settings**, then uninstall.

Then uninstall from Decky: open Decky's settings, go to **Plugins**, select the **...** beside WiFi Optimizer and choose **Uninstall**. Or remove it by hand from a terminal in Desktop Mode:

```bash
sudo rm -rf ~/homebrew/plugins/WiFi\ Optimizer
sudo systemctl restart plugin_loader
sudo rm -f /etc/NetworkManager/dispatcher.d/99-wifi-optimizer
sudo rm -f /etc/NetworkManager/conf.d/99-wifi-optimizer.conf
sudo rm -f /etc/modprobe.d/99-wifi-optimizer.conf
sudo nmcli general reload conf
sudo rm -rf ~/homebrew/settings/WiFi\ Optimizer ~/homebrew/logs/WiFi\ Optimizer
```

Decky is restarted straight after the plugin folder is removed, so the plugin is no longer running to write its settings or logs back. Restarting Decky also restarts Steam.

Restart the device afterwards: WiFi power save, the driver's power settings and the PCIe power states go back to your system's own from the next boot.

If you switched the WiFi backend from the plugin on a system other than SteamOS, switch it back in the plugin before removing it by hand, or leave `/etc/NetworkManager/conf.d/99-wifi-optimizer-backend.conf` in place: removing that file alone leaves the other backend turned off.

If you couldn't run Reset first (the panel wouldn't open, say), the access point lock, the band preference, custom DNS, the IPv6 setting and Allow WPA2 can still be on the networks they were set on. Forget each of those networks in Steam's WiFi settings and join it again, or run this once for each, with the network's saved name in place of `NAME`:

```bash
sudo nmcli connection modify "NAME" 802-11-wireless.bssid "" 802-11-wireless.band "" ipv4.ignore-auto-dns no ipv4.dns "" ipv6.ignore-auto-dns no ipv6.method auto
```

That command leaves Allow WPA2 as it is, and forgetting and joining the network again may not undo it either, since the network can be saved as WPA2 again. Use the `key-mgmt sae` command above for that.

On SteamOS, a WiFi backend switch made from the plugin can also have copied its power save setting into SteamOS's own WiFi config. If Steam's WiFi power management setting reads off after removing the plugin by hand, turn it back on.

## Building from source

Requires Node.js 22 and pnpm 10.

```bash
git clone https://github.com/ArcadaLabs-Jason/WifiOptimizer.git
cd WifiOptimizer
pnpm i
pnpm run build
```

To deploy to your Deck, copy `.vscode/defsettings.json` to `.vscode/settings.json` and fill in your Deck's address, then use the VS Code **builddeploy** task (Terminal > Run Task). It needs SSH access to the Deck (set a password with `passwd` in Desktop Mode, then `sudo systemctl enable --now sshd`). It copies the plugin to `~/wifi-optimizer-dev` on the Deck, then asks for the Deck's sudo password to install it into the plugin folder, which belongs to root, and restart Decky Loader.

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
