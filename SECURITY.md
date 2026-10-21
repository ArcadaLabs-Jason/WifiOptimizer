# Security

## Reporting a vulnerability

Please report security problems privately rather than in a public issue. Reach me through the contact in the [README](README.md#contact). If GitHub's private vulnerability reporting is enabled for this repository, you can also use the "Report a vulnerability" button on the Security tab.

Include what you found, the plugin version, your device and OS, and the steps to reproduce it if you have them.

## Why it matters here

The plugin's backend runs as root. Decky only does that for plugins that ask for it (the `root` flag in `plugin.json`), and this one does, because it changes NetworkManager profiles, kernel settings and system files. It also reads a settings file that lives in a directory your desktop user can write to. A bug in how it handles that input can be a way to run something as root. The plugin's own folder is owned by root, so only the settings and log folders are writable by the desktop user.

The desktop account itself is trusted. Anything already running as that user can capture the sudo password through the user's own shell, so the install script is not hardened against it. Which local programs can call the plugin's functions is decided by Decky Loader, not by the plugin.

## Signed releases

From v0.21.0-beta on, every release, stable and beta, carries the source tarball `wifi-optimizer-v<version>.tar.gz`, a `SHA256SUMS` file listing its checksum, and `SHA256SUMS.minisig`, a [minisign](https://jedisct1.github.io/minisign/) signature of that list. The signature's trusted comment names the release, `wifi-optimizer v<version>`. From v0.24.3 on, releases also carry `wifi-optimizer.zip`, the plugin packaged for Decky's Install Plugin from ZIP or URL, and `SHA256SUMS` lists it too.

The in-app updater checks the signature against the keys below, which are built into the plugin, and checks that the comment names the version it is installing. It then checks the downloaded tarball against the signed checksum before unpacking it. If any of that fails, nothing is installed. A release without these files is not installed either. This checking starts with v0.21.0-beta. Earlier versions install what they download without checking, including the update that brings in v0.21.0-beta.

The install script (`install.sh`) does not check signatures. It downloads the source archive of the latest release from GitHub over HTTPS, and if it cannot look up the latest release it downloads the `main` branch instead. Decky's Install Plugin from ZIP or URL doesn't check them either. If you want to check a release before installing it, use the steps below.

Release key, id `FE0DCD913B7044FA`:

```
RWT6RHA7kc0N/vvpQCVTRBrSXNZpv7hBFPZpSRX9m5v55Mrl15BB1heh
```

Rescue key, id `0DB3B91752AFFB43`:

```
RWRD+69SF7mzDbaCBVP3Pj5/5sOngkf/rVfOxCocbSTf+H8OP115i+8g
```

The release key signs releases. The rescue key is kept apart from the release key, and is only used to sign a release that replaces the release key if that key is ever lost or leaked. The updater accepts a release signed with either key.

To check a release by hand, download `SHA256SUMS`, `SHA256SUMS.minisig` and the file you want (the tarball or the zip) into one directory and run:

```bash
minisign -Vm SHA256SUMS -P RWT6RHA7kc0N/vvpQCVTRBrSXNZpv7hBFPZpSRX9m5v55Mrl15BB1heh
sha256sum -c --ignore-missing SHA256SUMS
```

The first should print the trusted comment with the version you downloaded, and the second should report the file you downloaded as OK. A release signed with the rescue key verifies with the rescue key instead: use `-P RWRD+69SF7mzDbaCBVP3Pj5/5sOngkf/rVfOxCocbSTf+H8OP115i+8g`.

## In scope

- The plugin backend (`main.py`)
- The NetworkManager dispatcher script (`defaults/dispatcher.sh.tmpl`, installed to `/etc/NetworkManager/dispatcher.d/`)
- The updater (the update script in `main.py`, and how it downloads, checks and installs a release)
- The install script (`install.sh`)

Problems in Decky Loader itself, SteamOS, NetworkManager or the kernel are best reported to those projects, though I am happy to hear about anything this plugin makes worse.
