# WiFi Optimizer v0.10.1-beta

> **Heads up:** This plugin modifies WiFi and network settings. Some optimizations (band preference, custom DNS, WiFi backend switch) can temporarily prevent WiFi from connecting. If this happens, a reboot usually fixes it. You can also try forgetting and rejoining your WiFi network from Steam settings.

A [Decky Loader](https://decky.xyz/) plugin that fixes WiFi problems that cause lag, stuttering, and dropped connections during game streaming. Benefits any streaming over WiFi - Steam Remote Play, [Moonlight](https://moonlight-stream.org/) / [Sunshine](https://app.lizardbyte.dev/Sunshine/), Parsec, Chiaki, and more.

**Supported devices:** Steam Deck (LCD and OLED). Multi-device support (Legion Go, ROG Ally, and other PC handhelds) is in beta testing.

## The problem

The OS resets WiFi settings after every system update and sleep/wake cycle. Power management gets re-enabled and network buffers reset to defaults. The result: latency spikes, connection drops, and degraded streaming quality - and the only fix is a trip to Desktop Mode.

WiFi Optimizer fixes this from Game Mode. One tap, and it stays fixed.

## Install / Update

[Decky Loader](https://decky.xyz/) must be installed first. Then open Desktop Mode > Konsole and run:

```bash
curl -sL https://github.com/ArcadaLabs-Jason/WifiOptimizer/raw/main/install.sh -o /tmp/wifi-opt-install.sh && sudo bash /tmp/wifi-opt-install.sh
```

This requires a user password - set one with `passwd` in Konsole if you haven't already.

Switch back to Game Mode. Open the Quick Access Menu (**...** button) > Decky > WiFi Optimizer.

**Updating:** The plugin checks for updates automatically when you open it. If an update is available, an update button appears at the top of the panel - tap it and the plugin updates and restarts itself. You can also update manually by running the install command above again.

> **Note:** If you're on a version before v0.6.6, the in-app updater had bugs. Please update manually using the install command above to get working auto-updates.

## Getting started

1. Open WiFi Optimizer from the Decky sidebar
2. Tap **Optimize Safe** - this applies the four no-brainer optimizations that are always beneficial:
   - Disables WiFi power save and PCIe power states (prevents lag spikes and streaming degradation)
   - Locks your BSSID (stops background scanning interruptions)
   - Enables auto-fix on wake (reapplies settings after sleep)
   - Tunes network buffers (handles streaming traffic bursts)
3. That's it. The plugin maintains these settings automatically, even after sleep/wake and OS updates.

Want to go further? The remaining optimizations are available as individual toggles - each one has an **(i)** icon you can tap for a full explanation of what it does and any tradeoffs. Advanced options include forcing 5/6 GHz, custom DNS, disabling IPv6, and switching between the `iwd` and `wpa_supplicant` WiFi backends.

## All optimizations

**Safe tier (applied by Optimize Safe)**

| Optimization | What it does |
|---|---|
| Prevent lag spikes | Disables WiFi power management and PCIe power states that cause packet batching, latency spikes, and throughput degradation during sustained streaming. |
| Stop background scanning | Locks to your current access point so your device stops scanning for other networks every few minutes. Disable before switching networks or if you use a mesh/multi-AP setup and need to roam. |
| Auto-fix on wake | Installs a script that reapplies your settings every time WiFi reconnects - works even if Decky isn't running |
| Network buffer tuning | Increases kernel buffer sizes and TX queue length to handle bursty streaming traffic without dropping packets |

**Manual opt-in (require configuration or have tradeoffs)**

| Optimization | What it does | Why it's manual |
|---|---|---|
| Force 5 GHz / 6 GHz | Locks WiFi to the higher-frequency band to avoid Bluetooth interference | Won't connect if your network is 2.4 GHz only |
| Custom DNS | Overrides your ISP's DNS with Cloudflare, Google, Quad9, or custom servers | Requires choosing a provider |
| Disable IPv6 | Forces all traffic through IPv4 | Only helps on networks with broken IPv6 - most are fine |
| WiFi backend (iwd / wpa_supplicant) | Switches between the default `iwd` and the older `wpa_supplicant`. Some devices are more stable with wpa_supplicant across sleep/wake and 5 GHz. | Only available when the system backend switch tool is present; some networks (certain WPA3, enterprise setups) behave differently between the two |

## Hardware support

| Device | WiFi Chip | Driver | Notes |
|---|---|---|---|
| Steam Deck LCD | WiFi 5 (RTL8822CE) | rtw88 | Full support |
| Steam Deck OLED | WiFi 6E (QCA206X) | ath11k_pci | Full support |
| Legion Go (all models) | WiFi 6E (MT7922) | mt7921e | Full support |
| ROG Ally (all models) | WiFi 6E (MT7922) | mt7921e | Full support |
| Other PC handhelds | Varies | iwlwifi, etc. | Detected automatically; driver-specific fixes applied when available |

The plugin detects your WiFi hardware at startup and applies the right optimizations for your chip. Devices with unrecognized hardware still get universal optimizations (power save, buffer tuning, BSSID lock, etc.).

## How it works

The plugin has two parts:

1. **The Decky plugin** runs in the Quick Access Menu. It applies optimizations when you toggle them and shows live status (signal, speed, frequency, channel). It detects when settings have drifted after wake and lets you fix them with one tap.

2. **A NetworkManager dispatcher script** runs independently of Decky, outside of Steam. Every time your WiFi reconnects (including after sleep), it automatically reapplies the volatile settings (power save, PCIe power states, buffers). If you uninstall the plugin, the script removes itself.

No background processes, no polling, no battery impact.

## Uninstall

**Before uninstalling:** tap **Reset Settings** in the plugin's Actions section. This reverts the runtime optimizations (power save, buffer tuning, PCIe ASPM) and deletes the plugin's own config files. Per-connection NetworkManager profile changes (BSSID lock, band preference, custom DNS, IPv6) stay on your saved WiFi network - to remove those, forget and rejoin the network from Steam's WiFi settings. The WiFi backend choice (iwd vs wpa_supplicant) is a system-wide SteamOS setting and isn't touched by the plugin on uninstall.

Then uninstall from Decky's plugin manager (Decky settings > WiFi Optimizer > Uninstall), or manually:

```bash
rm -rf ~/homebrew/plugins/WiFi\ Optimizer
sudo rm -f /etc/NetworkManager/dispatcher.d/99-wifi-optimizer
sudo rm -f /etc/NetworkManager/conf.d/99-wifi-optimizer.conf
sudo rm -f /etc/modprobe.d/99-wifi-optimizer.conf
sudo systemctl restart plugin_loader
```

## Building from source

Requires Node.js and pnpm v9.

```bash
git clone https://github.com/ArcadaLabs-Jason/WifiOptimizer.git
cd WifiOptimizer
pnpm i
pnpm run build
```

To deploy to your Deck, copy `.vscode/defsettings.json` to `.vscode/settings.json`, fill in your Deck's IP and password, then use the VS Code **builddeploy** task (Terminal > Run Task).

## Contact

Follow development on [Bluesky](https://bsky.app/profile/thefanciestpeanut.bsky.social).

## License

BSD 3-Clause. See [LICENSE](LICENSE).
