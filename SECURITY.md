# Security

## Reporting a vulnerability

Please report security problems privately rather than in a public issue. Reach me through the contact in the [README](README.md#contact). If GitHub's private vulnerability reporting is enabled for this repository, you can also use the "Report a vulnerability" button on the Security tab.

Include what you found, the plugin version, your device and OS, and the steps to reproduce it if you have them.

## Why it matters here

The plugin's backend runs as root, as every Decky plugin backend does. It changes NetworkManager profiles, kernel settings and system files, and it reads a settings file that lives in a directory your desktop user can write to. A bug in how it handles that input can be a way to run something as root.

## In scope

- The plugin backend (`main.py`)
- The NetworkManager dispatcher script (`defaults/dispatcher.sh.tmpl`, installed to `/etc/NetworkManager/dispatcher.d/`)
- The updater (the update script in `main.py`, and how it downloads and installs a release)
- The install script (`install.sh`)

Problems in Decky Loader itself, SteamOS, NetworkManager or the kernel are best reported to those projects, though I am happy to hear about anything this plugin makes worse.
