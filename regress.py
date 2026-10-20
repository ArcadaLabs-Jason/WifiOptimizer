"""Regression checks for WiFi Optimizer.

Run from the repo root:  python3 regress.py

These run against main.py directly, with NetworkManager, iw and the rest
stubbed, so they need no hardware and no Decky install. Where a check needs
the system to behave rather than merely answer - the access point lock reads
the link, writes a pin, cycles the radio and reads the link again - the stub
models that behaviour instead of replaying canned output, because a stub that
cannot reassociate proves nothing about what happens after the write.

Two habits worth keeping when adding to this file:

  * Run a new check against the code from BEFORE the fix and watch it fail.
  * Use getattr for anything that may not exist yet, so a missing function
    fails one check instead of raising and hiding every check after it.
"""
import importlib.util, asyncio, os, re, tempfile, time, json, sys
spec = importlib.util.spec_from_file_location("wifiopt", "main.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.SETTINGS_FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
# Never read the host's own network devices: a Linux machine running this
# would otherwise answer from its real sysfs and skip every nmcli stub.
m.SYSFS_NET = os.path.join(tempfile.mkdtemp(), "no-sysfs")
# Nor the host's /run, where the update script reports its progress.
m.UPDATE_STATE_FILE = os.path.join(tempfile.mkdtemp(), "update-state")
FAILS = []
def ok(c, l):
    print(("PASS  " if c else "FAIL  ")+l)
    if not c: FAILS.append(l)

def section(t): print(f"\n--- {t} ---")

section("security: driver profile validation")
p = m.Plugin()
for name, prof in m.DRIVER_PROFILES.items():
    ok(p._safe_sysfs_fixes(prof) == prof["sysfs_power_fixes"], f"{name} sysfs preserved")
    ok(p._safe_modprobe_options(name, prof) == prof["modprobe_options"], f"{name} modprobe preserved")
for prof, label in [
    ({"modprobe_options":["install x /bin/sh"]}, "install directive"),
    ({"modprobe_options":["alias a b"]}, "alias directive"),
    ({"modprobe_options":["softdep x pre: y"]}, "softdep directive"),
    ({"sysfs_power_fixes":["/etc/passwd"]}, "sysfs outside /sys/module"),
    ({"sysfs_power_fixes":["/sys/module/../../etc/shadow"]}, "sysfs traversal"),
]:
    ok(not p._safe_sysfs_fixes(prof) and not p._safe_modprobe_options("ath11k_pci", prof), f"rejected {label}")

section("security: symlink-safe write")
d = tempfile.mkdtemp(); victim = os.path.join(d,"victim"); target = os.path.join(d,"t")
open(victim,"w").write("PRECIOUS"); os.symlink(victim, target)
ok(m._write_no_follow(target, "x") is True, "write succeeds")
ok(open(victim).read() == "PRECIOUS", "victim not truncated")

section("runtime: what Decky's own build lets the backend do")
import ast, shlex
_tree = ast.parse(open("main.py").read())
# The backend runs inside Decky's PyInstaller build, which carries only the
# standard library modules Decky itself imports. A module missing from it
# fails the import and the whole backend with it. Decky 3.3.0 dropped shlex,
# which broke every beta from 0.12.1 on. Each name here was checked against
# the module list of the 3.3.0 and 3.2.10 PluginLoader binaries; check any
# addition the same way before relying on it (os, re, time and pwd live in
# base_library.zip or are built in, so they do not show in that list).
BUNDLED = {"os", "re", "copy", "pwd", "json", "tempfile", "time",
           "asyncio", "subprocess", "decky"}
_imported = set()
for node in ast.walk(_tree):
    if isinstance(node, ast.Import):
        _imported.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.level == 0:
        _imported.add(node.module.split(".")[0])
ok(_imported <= BUNDLED,
   f"main.py imports only modules Decky bundles (extra: {sorted(_imported - BUNDLED)})")

for v in ["", "/home/deck/homebrew/settings/WiFi Optimizer/settings.json",
          "plain", "it's", "$(id)", "a\"b", "back`tick", "new\nline", "tab\t",
          "--opt=val", "@%+=:,./-", "naïve", "*?[]"]:
    ok(m._sh_quote(v) == shlex.quote(v), f"_sh_quote matches shlex.quote for {v!r}")

_saved = dict(os.environ)
try:
    os.environ["LD_LIBRARY_PATH"] = "/tmp/_MEIabc"
    os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
    ok("LD_LIBRARY_PATH" not in m._child_env(),
       "a child does not inherit the path to Decky's bundled libraries")
    os.environ["LD_LIBRARY_PATH_ORIG"] = "/usr/lib/custom"
    env = m._child_env()
    ok(env.get("LD_LIBRARY_PATH") == "/usr/lib/custom" and "LD_LIBRARY_PATH_ORIG" not in env,
       "a path the system had before Decky started is given back instead")
    ok(os.environ["LD_LIBRARY_PATH"] == "/tmp/_MEIabc",
       "and building the child's environment leaves the plugin's own alone")
finally:
    os.environ.clear(); os.environ.update(_saved)

# Which commands Decky's libraries break depends on what each Decky release
# bundles, so no call may opt out: every process we start gets _child_env().
_spawns = [n for n in ast.walk(_tree) if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute)
           and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"
           and n.func.attr in ("run", "Popen", "call", "check_call", "check_output")]
ok(len(_spawns) >= 2, f"the process-start calls are found at all ({len(_spawns)})")
_bare = [n.lineno for n in _spawns
         if not any(k.arg == "env" and isinstance(k.value, ast.Call)
                    and getattr(k.value.func, "id", "") == "_child_env" for k in n.keywords)]
ok(not _bare, f"every process start passes env=_child_env() (missing at lines {_bare})")
ok(not any(isinstance(n, ast.Attribute) and n.attr in ("system", "popen")
           and getattr(n.value, "id", "") == "os" for n in ast.walk(_tree)),
   "and nothing starts a process around it with os.system or os.popen")

section("detection: which interface and which driver")
class NmcliSays(m.Plugin):
    def __init__(self, devs):
        super().__init__(); self.devs = devs; self.nmcli_calls = 0
    def _run_cmd(self, cmd, timeout=5):
        if cmd[:2] == ["/usr/bin/nmcli", "-t"] and cmd[-2:] == ["dev", "status"]:
            self.nmcli_calls += 1
            return {"success": True, "stdout": self.devs, "stderr": "", "returncode": 0}
        return {"success": False, "stdout": "", "stderr": "", "returncode": 1}

def fake_sysfs(ifaces):
    """ifaces: name -> (has phy80211, has device link, driver module or None)"""
    root = tempfile.mkdtemp()
    for name, (phy, dev, module) in ifaces.items():
        os.makedirs(os.path.join(root, name))
        if phy:
            os.makedirs(os.path.join(root, name, "phy80211"))
        if dev:
            devdir = os.path.join(root, f".dev-{name}")
            os.makedirs(devdir)
            if module:
                os.makedirs(os.path.join(root, ".mods", module), exist_ok=True)
                os.makedirs(os.path.join(devdir, "driver"))
                os.symlink(os.path.join(root, ".mods", module),
                           os.path.join(devdir, "driver", "module"))
            os.symlink(devdir, os.path.join(root, name, "device"))
    return root

_real_sysfs = m.SYSFS_NET
try:
    m.SYSFS_NET = fake_sysfs({"lo": (False, False, None),
                              "wlan0": (True, True, "ath11k_pci")})
    d = NmcliSays("wlan0:wifi")
    ok(d._get_wifi_interface() == "wlan0" and d.nmcli_calls == 0,
       "one physical wireless netdev is read from sysfs without asking nmcli")
    ok(d._detect_wifi_driver() == "ath11k_pci", "and its driver is read from the module link")

    m.SYSFS_NET = fake_sysfs({"wlan0": (True, True, "x"), "wlan1": (True, True, "y")})
    d = NmcliSays("wlan1:wifi\nwlan0:wifi")
    ok(d._get_wifi_interface() == "wlan1" and d.nmcli_calls == 1,
       "two candidates are not decided by directory order: NetworkManager picks")

    m.SYSFS_NET = fake_sysfs({"wlan0": (True, False, None)})
    d = NmcliSays("p2p-dev-wlan0:wifi-p2p\nwlan0:wifi")
    ok(d._get_wifi_interface() == "wlan0" and d.nmcli_calls == 1,
       "a wireless netdev with no device behind it is not taken as the radio")

    m.SYSFS_NET = fake_sysfs({"lo": (False, False, None)})
    d = NmcliSays("")
    ok(d._get_wifi_interface() is None,
       "no radio anywhere is None, which is what wlan0 recovery waits on")

    for module, key in [("rtw89_8852be", "rtw89"), ("rtw89_8852ce", "rtw89"),
                        ("ath12k", "ath12k"), ("rtw88_8822ce", "rtw88"),
                        ("mt7921e", "mt7921e"), ("brcmfmac", "brcmfmac")]:
        m.SYSFS_NET = fake_sysfs({"wlan0": (True, True, module)})
        ok(NmcliSays("")._detect_wifi_driver() == key,
           f"driver module {module} resolves to profile {key}")
finally:
    m.SYSFS_NET = _real_sysfs

for name in ("rtw89", "ath12k"):
    prof = m.DRIVER_PROFILES[name]
    ok(p._safe_modprobe_options(name, prof) == prof["modprobe_options"]
       and p._safe_sysfs_fixes(prof) == prof["sysfs_power_fixes"],
       f"the {name} profile passes the same validation as the shipped ones")
for product, family in [("Claw A1M", "msi_claw"), ("AYANEO 2S", "ayaneo"),
                        ("G1618-04", "gpd_win"), ("Galileo", "deck_oled")]:
    hit = next((i["family"] for pre, i in m.DMI_SUBSTRING_DEVICES if product.startswith(pre)),
               m.DMI_DEVICES.get(product, {}).get("family"))
    ok(hit == family, f"product name {product!r} is recognised as {family}")

section("timestamps: re-applying is not a change")
m.ENFORCED_FILE = os.path.join(tempfile.mkdtemp(), "last_enforced")
class Volatile(m.Plugin):
    """The three volatile setters, each stamping as the real ones do."""
    def __init__(self):
        super().__init__(); self.calls = []
    async def _stamp(self, name):
        self.calls.append(name)
        m._save_settings_with_timestamp(m._load_settings())
        return {"success": True}
    async def set_power_save(self, disabled): return await self._stamp("power_save")
    async def set_buffer_tuning(self, enabled): return await self._stamp("buffer")
    async def set_cake(self, enabled): return await self._stamp("cake")

m._save_settings({"power_save_disabled": True, "buffer_tuning_enabled": True,
                  "cake_enabled": True, "last_applied": 1700000000})
if os.path.exists(m.ENFORCED_FILE): os.remove(m.ENFORCED_FILE)
v = Volatile()
res = asyncio.run(v.reapply_volatile())
ok(res.get("applied") == 3 and v.calls == ["power_save", "buffer", "cake"],
   "a game-launch reapply runs all three volatile setters")
ok(m._load_settings().get("last_applied") == 1700000000,
   "and leaves Last changed where the user's own change put it")
ok(os.path.exists(m.ENFORCED_FILE)
   and abs(int(open(m.ENFORCED_FILE).read()) - time.time()) < 60,
   "recording itself as Auto-applied instead")

m._save_settings({"power_save_disabled": False, "buffer_tuning_enabled": False,
                  "cake_enabled": False, "last_applied": 1700000000})
os.remove(m.ENFORCED_FILE)
asyncio.run(Volatile().reapply_volatile())
ok(not os.path.exists(m.ENFORCED_FILE),
   "nothing enabled means nothing re-applied and nothing recorded")

class RealSetter(m.Plugin):
    def _run_cmd(self, cmd, timeout=5):
        return {"success": True, "stdout": "", "stderr": "", "returncode": 0}
    def _get_wifi_interface(self): return "wlan0"
_real_nm_conf = m.NM_CONF_PATH
m.NM_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
try:
    m._save_settings({"last_applied": 1700000000})
    res = asyncio.run(RealSetter().set_power_save(True))
    ok(res.get("success") and m._load_settings().get("last_applied") != 1700000000,
       "a toggle the user flips still moves Last changed")
finally:
    m.NM_CONF_PATH = _real_nm_conf

_main_src = open("main.py").read()
_main_body2 = _main_src[_main_src.index("    async def _main(self):"):_main_src.index("    async def _unload(self):")]
ok("self._record_reapplied(" in _main_body2,
   "the startup re-application records itself the same way")

section("address conflict: the router's fault, said plainly")
# Verbatim from a Deck's journal the night a router handed out in-use addresses.
NM_LOG = (
    "dhcp4 (wlan0): state changed new lease, address=192.168.1.9, acd conflict\n"
    "device (wlan0): conflict detected for IP address 192.168.1.9 with host 24:E8:53:90:64:8A\n"
    "manager: NetworkManager state is now CONNECTED_GLOBAL\n"
)
class Journal(m.Plugin):
    def __init__(self, log, ok_=True):
        super().__init__(); self.log = log; self.ok_ = ok_; self.reads = 0
    def _run_cmd(self, cmd, timeout=5):
        if cmd[0] == "/usr/bin/journalctl":
            self.reads += 1
            return {"success": self.ok_, "stdout": self.log, "stderr": "", "returncode": 0}
        return {"success": False, "stdout": "", "stderr": "", "returncode": 1}

j = Journal(NM_LOG)
j._recheck_address_conflict("192.168.1.9")
ok(j._address_conflict == {"ip": "192.168.1.9", "other": "24:E8:53:90:64:8A"},
   "a conflict logged for the address we hold is reported, with the other device")
j = Journal(NM_LOG)
j._recheck_address_conflict("192.168.1.36")
ok(j._address_conflict is None, "a conflict over an address we no longer hold is not")
j = Journal("dhcp4 (wlan0): state changed new lease, address=192.168.1.11, acd conflict\n")
j._recheck_address_conflict("192.168.1.11")
ok(j._address_conflict == {"ip": "192.168.1.11", "other": ""},
   "the DHCP form alone counts, which is how one clash that night was logged")
j = Journal("device (wlan0): conflict detected for IP address 192.168.1.90 with host 24:E8:53:90:64:8A\n"
            "dhcp4 (wlan0): state changed new lease, address=192.168.1.90, acd conflict\n")
j._recheck_address_conflict("192.168.1.9")
ok(j._address_conflict is None, "an address that merely starts the same is not ours")
j = Journal("", ok_=False)
j._recheck_address_conflict("192.168.1.9")
ok(j._address_conflict is None, "an unreadable journal reports nothing")
j = Journal(NM_LOG)
j._recheck_address_conflict("")
ok(j._address_conflict is None and j.reads == 0, "no address, no journal read")
j = Journal(NM_LOG)
j._recheck_address_conflict("192.168.1.9"); j._recheck_address_conflict("192.168.1.9")
ok(j.reads == 1, "throttled: one journal read per minute for the same address")
j._recheck_address_conflict("192.168.1.11")
ok(j.reads == 2, "but a new address is looked at straight away")

section("reach: a stronger access point the saved security cannot use")
WU = "aa0fd3f5-5fba-4291-8cf0-3b38838338d3"
JASONS_ROOM = [  # measured on a Deck: the office radio is WPA2 only
    ("D8:07:B6:90:C7:D1", 100, 5180, "WPA2"),
    ("D8:07:B6:90:C7:D0", 100, 2462, "WPA2"),
    ("62:E9:31:0D:23:5F", 62, 5220, "WPA2 WPA3"),
    ("62:E9:31:0D:1F:E7", 52, 5745, "WPA2 WPA3"),
]
class Reach(m.Plugin):
    def __init__(self, km, aps):
        super().__init__(); self.km = km; self.aps = aps
    def _profile_key_mgmt(self, uuid): return self.km
    def _visible_access_points(self, iface, ssid, rescan=False, cached_only=False):
        self.cached_only = cached_only; return list(self.aps)

r = Reach("sae", JASONS_ROOM)
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked == {"signal": 100, "current_signal": 52},
   "WPA3-only profile, WPA2-only radio far stronger: reported with both signals")
r = Reach("wpa-psk", JASONS_ROOM)
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked is None, "a profile that already allows WPA2 has nothing to report")
r = Reach("sae", [("D8:07:B6:90:C7:D1", 60, 5180, "WPA2"),
                  ("62:E9:31:0D:1F:E7", 52, 5745, "WPA2 WPA3")])
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked is None, "inside the lock's margin, a WPA2-only radio is not worth a notice")
r = Reach("sae", [("62:E9:31:0D:1F:E7", 52, 5745, "WPA2 WPA3"),
                  ("AA:AA:AA:AA:AA:AA", 100, 5180, "WPA3")])
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked is None, "a stronger radio the profile CAN join is the lock's business, not this")
r = Reach("sae", [("AA:AA:AA:AA:AA:AA", 100, 5180, "")])
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked is None, "an open radio of the same name is never suggested")
r = Reach("sae", JASONS_ROOM)
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
r.aps = []
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked is not None, "throttled: a second poll within the window reuses the finding")
r._reach_checked_at = 0.0
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked is None, "and past it the finding is re-read, so it clears when the radio goes")

class Allow(m.Plugin):
    """Profile security as NetworkManager holds it, and whether a reconnect takes."""
    def __init__(self, km="sae", connects=True, lock=False):
        super().__init__(); self.km = km; self.connects = connects
        self.writes = []; self.lock_calls = 0
        m._save_settings({"bssid_lock_enabled": lock})
    def _resolve_uuid(self, msg=None): return WU, None
    def _profile_key_mgmt(self, uuid): return self.km
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.writes.append((key, value)); self.km = value
        return {"success": True, "stdout": "", "stderr": "", "returncode": 0}
    def _hard_reconnect(self, uuid=None): return True
    def _get_active_connection_uuid(self):
        return WU if (self.connects or self.km == "sae") else None
    def _connection_activated(self, uuid):
        return uuid == self._get_active_connection_uuid()
    async def set_bssid_lock(self, enabled):
        self.lock_calls += 1
        self.busy_at_lock = self._profile_change_in_flight()
        return {"success": True}

_real_sleep = asyncio.sleep
async def _no_sleep(*_a, **_k): return None
asyncio.sleep = _no_sleep
try:
    a = Allow()
    res = asyncio.run(a.allow_wpa2())
    ok(res.get("success") and a.writes == [("802-11-wireless-security.key-mgmt", "wpa-psk")],
       "allowing WPA2 writes wpa-psk, which NetworkManager offers as WPA2 and WPA3")
    ok(a.lock_calls == 0, "with the lock off, nothing else is touched")
    ok(getattr(a, "_profile_change_depth", 0) == 0, "and the in-flight count is released")

    a = Allow(lock=True)
    asyncio.run(a.allow_wpa2())
    ok(a.lock_calls == 1, "with the lock on, it chooses again now the stronger radio is eligible")
    ok(a.busy_at_lock is False and getattr(a, "_profile_change_depth", 0) == 0,
       "and hands the in-flight guard over, since the lock setter refuses while it is held")

    a = Allow(connects=False)
    res = asyncio.run(a.allow_wpa2())
    ok(not res.get("success") and a.km == "sae",
       "if the profile will not come back, it is put back to WPA3 only")
    ok(a.writes[-1] == ("802-11-wireless-security.key-mgmt", "sae"),
       "by writing sae back, not by leaving it half-changed")

    a = Allow(km="wpa-psk")
    res = asyncio.run(a.allow_wpa2())
    ok(not res.get("success") and res.get("error") == "not_needed" and not a.writes,
       "a profile that already allows WPA2 is not rewritten")
finally:
    asyncio.sleep = _real_sleep

section("security: version validation")
for v, good in [("0.12.0",True),("0.12.0-beta",True),("1.2.3.4",True),
                ("0.12.0; rm -rf /",False),("$(id)",False),("`id`",False),("v1.0",False)]:
    ok(bool(m.VERSION_RE.match(v)) == good, f"version {v!r} {'accepted' if good else 'rejected'}")

section("security: input validation")
ok(m.IFACE_RE.match("wlan0") and not m.IFACE_RE.match("../x"), "iface regex")
ok(m.DNS_SERVER_RE.match("1.1.1.1") and not m.DNS_SERVER_RE.match("1.1.1.1;x"), "dns regex")
open(m.SETTINGS_FILE,"w").write(json.dumps({"driver":[], "cake_enabled":"yes"}))
s = m._load_settings()
ok(isinstance(s["driver"], str) and isinstance(s["cake_enabled"], bool), "settings type coercion")

# This file belongs to the desktop user and root parses it on every poll, so
# every value that reaches a root subprocess is checked on the way in, not
# only at the point of use.
open(m.SETTINGS_FILE, "w").write(json.dumps({
    "bssid_lock_uuids": ["aa0fd3f5-5fba-4291-8cf0-3b38838338d3", "../../etc", 7],
    "band_preference_uuids": ["not-a-uuid"],
    "ipv6_uuids": ["aa0fd3f5-5fba-4291-8cf0-3b38838338d3", "; rm -rf /", None],
    "regdomain": "US; id",
    "regdomain_previous": "TOOLONG",
}))
s = m._load_settings()
ok(s["bssid_lock_uuids"] == ["aa0fd3f5-5fba-4291-8cf0-3b38838338d3"],
   "junk is dropped from the tracked lock uuids")
ok(s["band_preference_uuids"] == [], "and from the tracked band uuids")
ok(s["ipv6_uuids"] == ["aa0fd3f5-5fba-4291-8cf0-3b38838338d3"],
   "and from the tracked IPv6 uuids, which reach nmcli the same way")
ok(s["regdomain"] == "" and s["regdomain_previous"] == "",
   "a region that cannot be valid does not survive a load")

section("concurrency: reconciliation guards")
calls=[]
class P(m.Plugin):
    def _nmcli_modify(self,u,k,v,timeout=5):
        calls.append((k,v)); return {"success":True,"stdout":"","stderr":"","returncode":0}
    def _run_cmd(self, cmd, timeout=5):
        calls.append((cmd[0],)); return {"success":True,"stdout":"","stderr":"","returncode":0}
q = P()
act = [{"kind":"bssid_repoint","uuid":"u1","value":"AA:BB:CC:DD:EE:01"}]
m._save_settings({**m.DEFAULT_SETTINGS,"bssid_lock_enabled":False})
calls.clear(); q._apply_status_actions({"live":{},"drift":{"bssid_lock":True}}, {}, list(act))
ok(not calls, "re-point dropped when lock disabled mid-poll")
m._save_settings({**m.DEFAULT_SETTINGS,"bssid_lock_enabled":True})
q._profile_change_depth = 1
calls.clear(); q._apply_status_actions({"live":{},"drift":{}}, {}, list(act))
ok(not calls, "re-point suppressed during a deliberate profile change")
q._profile_change_depth = 0
calls.clear(); st={"live":{},"drift":{"bssid_lock":True}}; pend={}
q._apply_status_actions(st, pend, list(act))
ok(calls and "bssid_lock" not in st["drift"], "normal re-point applies and clears drift")

section("purity: collector must not mutate")
import ast
tree = ast.parse(open("main.py").read())

# Follow the calls, not just the one body. The collector runs in a worker
# thread and promises to PROPOSE writes rather than make them, and that
# promise is what lets get_status apply them on the event loop where they are
# serialized against the setters. Checking only _collect_status's own body
# made the check weaker every time a block moved into a helper: the property
# would still be asserted and no longer be tested.
_FUNCS = {}
for n in ast.walk(tree):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _FUNCS.setdefault(n.name, n)

def _callees(node):
    """Everything this function calls as self._x(...) or as a bare name."""
    out = set()
    for c in ast.walk(node):
        if not isinstance(c, ast.Call):
            continue
        f = c.func
        if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id == "self"):
            out.add(f.attr)
        elif isinstance(f, ast.Name):
            out.add(f.id)
    return out

def _reaches(root, targets):
    """The call chain from root to any forbidden name, or [] if there is none."""
    seen, queue = set(), [(root, [root])]
    while queue:
        name, path = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        if name in targets:
            return path
        node = _FUNCS.get(name)
        if node is None:
            continue
        for callee in sorted(_callees(node)):
            queue.append((callee, path + [callee]))
    return []

for targets, label in [
    (("_save_settings", "_save_settings_with_timestamp"), "save settings"),
    (("_nmcli_modify",), "modify NM"),
]:
    chain = _reaches("_collect_status", targets)
    ok(not chain, f"_collect_status does not {label}"
                  + (f" (via {' -> '.join(chain)})" if chain else ""))

section("lifecycle: nothing raises")
async def life():
    pl = m.Plugin()
    await pl._main(); await pl._unload(); await pl._migration()
    for c in [pl.set_power_save(True), pl.set_cake(False), pl.reapply_all(),
              pl.optimize_safe(), pl.get_status(), pl.reset_settings()]:
        r = await c
        ok(isinstance(r, dict), "returns dict")
asyncio.run(life())

section("collector runs end-to-end against a connected device")
# This exercises _collect_status through the realistic paths. Its absence is
# why a NameError on the ordinary lock-on-active-profile path went unnoticed:
# the catch-all turns it into a plausible-looking error status.
U = "aa0fd3f5-5fba-4291-8cf0-3b38838338d3"
REAL = {
 ("nmcli","-t","-f","DEVICE,TYPE","dev","status"): "wlan0:wifi",
 ("nmcli","-t","-f","UUID,TYPE","con","show","--active"): U + ":802-11-wireless",
 ("iw","dev","wlan0","get","power_save"): "Power save: off",
 ("iw","dev","wlan0","link"): "Connected to 02:00:00:00:00:11 (on wlan0)\n\tfreq: 5180",
 ("iw","dev","wlan0","info"): "Interface wlan0\n\tchannel 36 (5180 MHz), width: 80 MHz",
 ("sysctl","-n","net.core.rmem_max"): "16777216",
 ("tc","qdisc","show","dev","wlan0"): "qdisc cake 8002: root",
}
class Connected(m.Plugin):
    def _run_cmd(self, cmd, timeout=5):
        key = tuple(c.split("/")[-1] if c.startswith("/usr") else c for c in cmd)
        out = REAL.get(key); j = " ".join(cmd)
        if out is None:
            if "802-11-wireless.ssid" in j: out = "802-11-wireless.ssid:TestNet"
            elif "802-11-wireless.bssid" in j: out = "802-11-wireless.bssid:"
            elif "ipv6.method" in j: out = "ipv6.method:auto"
            elif "802-11-wireless.band" in j: out = "802-11-wireless.band:"
            else: out = ""
        return {"success":True,"stdout":out,"stderr":"","returncode":0}
    def _nmcli_modify(self,*a,**k): return {"success":True,"stdout":"","stderr":"","returncode":0}

for label, extra in [
    ("plain",              {}),
    ("lock on active",     {"bssid_lock_enabled":True,"bssid_lock_connection_uuid":U}),
    ("lock on another",    {"bssid_lock_enabled":True,"bssid_lock_connection_uuid":"other-uuid"}),
    ("band scoped here",   {"band_preference_enabled":True,"band_preference":"a","band_preference_ssid":"TestNet"}),
    ("band scoped away",   {"band_preference_enabled":True,"band_preference":"a","band_preference_ssid":"Elsewhere"}),
    ("lock + band",        {"bssid_lock_enabled":True,"bssid_lock_connection_uuid":U,
                            "band_preference_enabled":True,"band_preference":"a","band_preference_ssid":"TestNet"}),
    ("ipv6 + cake drift",  {"ipv6_disabled":True,"cake_enabled":True}),
    ("stale pins, lock off",{"bssid_lock_enabled":False,
                            "bssid_lock_uuids":["11111111-1111-1111-1111-111111111111"]}),
]:
    m._save_settings({**m.DEFAULT_SETTINGS, "driver":"ath11k_pci", "distro_id":"steamos", **extra})
    st = asyncio.run(Connected().get_status())
    ok(st.get("success") is True and st.get("error") is None,
       f"collector clean: {label}" + ("" if st.get("success") else f" -> {st.get('message')}"))
 

section("access point lock: waiting for an association")

CONNECTED = "Connected to 02:00:00:00:00:11 (on wlan0)\n\tfreq: 5745.0\n"

class LateAssoc(m.Plugin):
    """iw reports nothing until the Nth poll, the way a reassociating link does."""
    def __init__(self, polls_before_up, fail=False):
        super().__init__()
        self.polls = 0
        self.polls_before_up = polls_before_up
        self.fail = fail
    def _get_wifi_interface(self): return "wlan0"
    def _get_active_connection_uuid(self): return "u1"
    def _get_profile_ssid(self, uuid, timeout=5): return "net"
    def _hard_reconnect(self, uuid): return True
    def _nmcli_modify(self, u, k, v, timeout=5):
        return {"success":True,"stdout":"","stderr":"","returncode":0}
    def _run_cmd(self, cmd, timeout=5):
        if "link" in cmd:
            self.polls += 1
            if self.fail:
                return {"success":False,"stdout":"","stderr":self.fail,"returncode":-1}
            if self.polls < self.polls_before_up:
                return {"success":True,"stdout":"","stderr":"","returncode":0}
            return {"success":True,"stdout":CONNECTED,"stderr":"","returncode":0}
        return {"success":True,"stdout":"","stderr":"","returncode":0}

m._save_settings(dict(m.DEFAULT_SETTINGS))

# The reported failure: the link is mid-reassociation for several polls. The old
# code read once and gave up; a 6s budget was still shorter than the 13-24s
# this hardware actually takes.
late = LateAssoc(polls_before_up=5)
r = asyncio.run(late.set_bssid_lock(True))
ok(r.get("success") is True, "locks once a late association arrives")
ok(late.polls >= 5, "kept polling instead of giving up on the first read")

# The budget must actually cover the measured hardware, not just be non-zero.
ok(getattr(m.Plugin, "_ASSOCIATION_WAIT_SECONDS", 0) >= 25,
   "association budget covers a measured 13-24s reassociation")

# The discriminating case: a link slower than the old six-second budget.
# 07fbe57 polled 12 times at 0.5s and gave up at 6s, which was shorter than
# every reassociation measured on the hardware this was written for.
slow = LateAssoc(polls_before_up=16)
r = asyncio.run(slow.set_bssid_lock(True))
ok(r.get("success") is True,
   "still locks when the association takes longer than six seconds")

# A command that failed must not be reported as "still reconnecting".
broken = LateAssoc(polls_before_up=1, fail="Command not found: /usr/bin/iw")
t0 = time.monotonic()
r = asyncio.run(broken.set_bssid_lock(True))
elapsed = time.monotonic() - t0
ok(r.get("success") is False, "a failed link read does not report success")
ok("reconnecting" not in r.get("message", "").lower(),
   "a failed command is not dressed up as a slow reconnect")
ok(r.get("detail", "").startswith("Command not found"),
   "the real error reaches the caller")
ok(elapsed < 5, "an unrecoverable failure reports at once, not after the budget")

# A link that never associates is still an honest "not yet", not a lock.
never = LateAssoc(polls_before_up=10**9)
never._ASSOCIATION_WAIT_SECONDS = 1.0
r = asyncio.run(never.set_bssid_lock(True))
ok(r.get("success") is False and r.get("error") == "no_wifi",
   "never associating stays a no_wifi refusal")



section("untracked pins on the active profile")

# Reproduces a real Deck: the profile carries 802-11-wireless.band=a while the
# plugin reports the band preference OFF and tracks no profiles at all, so the
# tracked-list walk has nothing to walk and the device stays pinned to 5 GHz
# with no sign of it in the panel.
class Pinned(Connected):
    def __init__(self, band="", bssid=""):
        super().__init__()
        self.pin_band, self.pin_bssid = band, bssid
        self.mods = []
    def _run_cmd(self, cmd, timeout=5):
        j = " ".join(cmd)
        if "802-11-wireless.band" in j and "con" in cmd:
            return {"success":True,"stdout":f"802-11-wireless.band:{self.pin_band}",
                    "stderr":"","returncode":0}
        if "802-11-wireless.bssid" in j and "con" in cmd:
            return {"success":True,"stdout":f"802-11-wireless.bssid:{self.pin_bssid}",
                    "stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout)
    def _nmcli_modify(self, u, k, v, timeout=5):
        self.mods.append((u, k, v))
        return {"success":True,"stdout":"","stderr":"","returncode":0}

base = {**m.DEFAULT_SETTINGS, "driver":"ath11k_pci", "distro_id":"steamos"}

m._save_settings({**base, "band_preference_enabled":False, "band_preference_uuids":[],
                  "pins_owned":["802-11-wireless.band"]})
pin = Pinned(band="a")
asyncio.run(pin.get_status())
ok(("802-11-wireless.band", "") in [(k, v) for _, k, v in pin.mods],
   "an untracked band pin on the active profile is cleared")

m._save_settings({**base, "bssid_lock_enabled":False, "bssid_lock_uuids":[],
                  "pins_owned":["802-11-wireless.bssid"]})
pin = Pinned(bssid="02\\:00\\:00\\:00\\:00\\:11")
asyncio.run(pin.get_status())
ok(("802-11-wireless.bssid", "") in [(k, v) for _, k, v in pin.mods],
   "an untracked access point pin on the active profile is cleared")

# Without evidence the plugin ever wrote that kind of pin, it was set by hand
# (nmcli, KDE's editor) and turning our feature off must not erase it. It is
# reported instead, so the panel can still say why the device is pinned.
m._save_settings({**base, "band_preference_enabled":False, "band_preference_uuids":[],
                  "pins_owned":[]})
pin = Pinned(band="a")
st = asyncio.run(pin.get_status())
ok(("802-11-wireless.band", "") not in [(k, v) for _, k, v in pin.mods]
   and st.get("external", {}).get("band"),
   "a band pin the plugin never wrote is reported, not cleared")

m._save_settings({**base, "bssid_lock_enabled":False, "bssid_lock_uuids":[],
                  "pins_owned":[]})
pin = Pinned(bssid="02\\:00\\:00\\:00\\:00\\:11")
st = asyncio.run(pin.get_status())
ok(("802-11-wireless.bssid", "") not in [(k, v) for _, k, v in pin.mods]
   and st.get("external", {}).get("bssid_lock"),
   "an access point pin the plugin never wrote is reported, not cleared")

# The other half: a preference that is ON must never be cleaned up, or the
# cleanup would undo the feature on every poll.
m._save_settings({**base, "band_preference_enabled":True, "band_preference":"a",
                  "band_preference_ssid":"TestNet", "band_preference_uuids":[]})
pin = Pinned(band="a")
asyncio.run(pin.get_status())
ok(("802-11-wireless.band", "") not in [(k, v) for _, k, v in pin.mods],
   "an enabled band preference is left alone")

m._save_settings({**base, "band_preference_enabled":False, "band_preference_uuids":[]})
pin = Pinned(band="")
asyncio.run(pin.get_status())
ok(("802-11-wireless.band", "") not in [(k, v) for _, k, v in pin.mods],
   "a profile with no pin is not written to needlessly")

# Cleanup is a second writer to the same property a setter owns, so it must
# stand down while one is mid-change rather than clear what it just wrote.
m._save_settings({**base, "band_preference_enabled":False, "band_preference_uuids":[]})
pin = Pinned(band="a")
pin._profile_change_depth = 1
asyncio.run(pin.get_status())
ok(("802-11-wireless.band", "") not in [(k, v) for _, k, v in pin.mods],
   "cleanup stands down while a setter is mid-change")



section("access point lock: choosing which access point to pin")

# A mesh advertising one network on several nodes: a strong pair on the
# router and weaker nodes further away.
MESH = "\n".join([
    "Net:02\\:00\\:00\\:00\\:00\\:51:100:5180 MHz:WPA2 WPA3",
    "Net:02\\:00\\:00\\:00\\:00\\:24:100:2462 MHz:WPA2 WPA3",
    "Net:02\\:00\\:00\\:00\\:00\\:52:54:5220 MHz:WPA2 WPA3",
    "Net:02\\:00\\:00\\:00\\:00\\:11:45:5745 MHz:WPA2 WPA3",
    "Other:AA\\:BB\\:CC\\:DD\\:EE\\:FF:99:5180 MHz:WPA2 WPA3",
])

# One SSID, two generations of hardware: a strong pair that only speaks WPA2
# beside weaker nodes that also offer WPA3. Taken from a real network.
MIXED = "\n".join([
    "Net:02\\:00\\:00\\:00\\:00\\:51:100:5180 MHz:WPA2",
    "Net:02\\:00\\:00\\:00\\:00\\:24:95:2462 MHz:WPA2",
    "Net:02\\:00\\:00\\:00\\:00\\:52:80:5220 MHz:WPA2 WPA3",
    "Net:02\\:00\\:00\\:00\\:00\\:11:45:5745 MHz:WPA2 WPA3",
])

def _ok(out=""):
    return {"success":True,"stdout":out,"stderr":"","returncode":0}

def _fields(line):
    """Split an `nmcli -t` line on unescaped colons. The harness keeps its own
    copy on purpose: borrowing the plugin's turns "the feature is missing"
    into a crash that aborts the run and hides every later check."""
    out, cur, esc = [], "", False
    for ch in line:
        if esc: cur += ch; esc = False
        elif ch == "\\": esc = True
        elif ch == ":": out.append(cur); cur = ""
        else: cur += ch
    out.append(cur)
    return out

class Mesh(m.Plugin):
    """A small model of NetworkManager on a mesh.

    It has to be a model rather than a set of canned replies: the lock reads
    the link, writes a pin, cycles the radio and reads the link again, so a
    stub that cannot re-associate tests nothing about what happens after the
    write.
    """
    def __init__(self, scan, on, reachable=None, band="", signal_dbm=None,
                 key_mgmt="wpa-psk"):
        super().__init__()
        self.scan, self.on, self.reachable = scan, on, reachable
        self.pinned, self.band, self.mods = "", band, []
        self.signal_dbm, self.scans, self.key_mgmt = signal_dbm, 0, key_mgmt
        self.aps = [
            (f[1], int(f[2]), int(re.match(r"\s*(\d+)", f[3]).group(1)))
            for f in (_fields(l) for l in scan.split("\n"))
            if len(f) >= 5 and f[0] == "Net"
        ]
    def _profile_key_mgmt(self, uuid): return self.key_mgmt
    def _freq_of(self, bssid):
        for b, _, f in self.aps:
            if b.upper() == (bssid or "").upper(): return f
        return 5745
    def _get_wifi_interface(self): return "wlan0"
    def _get_active_connection_uuid(self): return "u1"
    def _get_profile_ssid(self, uuid, timeout=5): return "Net"
    def _nmcli_modify(self, u, k, v, timeout=5):
        self.mods.append((k, v))
        if k == "802-11-wireless.bssid": self.pinned = v
        if k == "802-11-wireless.band": self.band = v
        return _ok()
    def _hard_reconnect(self, uuid):
        want = (self.pinned or "").upper()
        if want:
            self.on = want.lower() if (
                self.reachable is None or want in self.reachable
            ) else ""
            return True
        # No pin: NM picks the strongest access point the profile allows.
        allowed = [
            ap for ap in self.aps
            if not self.band
            or (ap[2] >= 5000) == (self.band == "a")
        ]
        allowed.sort(key=lambda ap: ap[1], reverse=True)
        self.on = allowed[0][0].lower() if allowed else ""
        return True
    def _run_cmd(self, cmd, timeout=5):
        if "wifi" in cmd and "list" in cmd:
            self.scans += 1
            # Answer the columns actually asked for. _band_is_reachable wants
            # SSID,FREQ while the lock wants SSID,BSSID,SIGNAL,FREQ; handing
            # both the same four makes the former read a BSSID as a frequency,
            # which would be a defect in the stub, not in the plugin.
            fields = cmd[cmd.index("-f") + 1].split(",") if "-f" in cmd else []
            rows = []
            for line in self.scan.split("\n"):
                got = _fields(line)
                if len(got) < 5: continue
                by = {"SSID": got[0], "BSSID": got[1].replace(":", "\\:"),
                      "SIGNAL": got[2], "FREQ": got[3], "SECURITY": got[4]}
                rows.append(":".join(by.get(f, "") for f in fields))
            return _ok("\n".join(rows))
        if "link" in cmd:
            if not self.on: return _ok("")
            sig = ("" if self.signal_dbm is None
                   else f"\tsignal: {self.signal_dbm} dBm\n")
            return _ok(
                f"Connected to {self.on} (on wlan0)\n"
                f"\tfreq: {self._freq_of(self.on)}.0\n{sig}"
            )
        return _ok()

base_ap = {**m.DEFAULT_SETTINGS, "driver":"ath11k_pci", "distro_id":"steamos"}

# The case that prompted this: sitting on a distant node at 45 with the
# router at 100 in range. Pinning what we are on would make that permanent.
m._save_settings(dict(base_ap))
g = Mesh(MESH, "02:00:00:00:00:11")
r = asyncio.run(g.set_bssid_lock(True))
ok(r.get("success") is True, "locking succeeds on a mesh")
ok(g.pinned.upper() == "02:00:00:00:00:51",
   "pins the strong access point, not the weak one it was sitting on")

# Equal signal on both bands of one node: prefer 5 GHz, because this plugin
# is for streaming.
m._save_settings(dict(base_ap))
g = Mesh(MESH, "02:00:00:00:00:11")
aps = getattr(g, "_visible_access_points", lambda *a: [])("wlan0", "Net")
ok(bool(aps) and aps[0][0] == "02:00:00:00:00:51" and aps[0][2] >= 5000,
   "at equal signal the 5 GHz radio is preferred")
ok(bool(aps) and all(ap[0] != "AA:BB:CC:DD:EE:FF" for ap in aps),
   "another network's access points are not considered")

# A small difference is not worth a reconnect.
NEAR = "\n".join([
    "Net:02\\:00\\:00\\:00\\:00\\:51:60:5180 MHz:WPA2 WPA3",
    "Net:02\\:00\\:00\\:00\\:00\\:11:50:5745 MHz:WPA2 WPA3",
])
m._save_settings(dict(base_ap))
g = Mesh(NEAR, "02:00:00:00:00:11")
asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "02:00:00:00:00:11",
   "a difference below the margin leaves the lock where it is")

# A band preference is a constraint: picking the other band would write a
# profile whose band and address contradict.
# A coherent state: the preference is on in settings AND written to the
# profile, which is what set_band_preference leaves behind.
m._save_settings({**base_ap, "band_preference_enabled":True,
                  "band_preference":"bg", "band_preference_ssid":"Net"})
g = Mesh(MESH, "02:00:00:00:00:11", band="bg")
asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() in ("02:00:00:00:00:24", "02:00:00:00:00:11"),
   "a 2.4 GHz preference never pins a 5 GHz access point")
ok(g.pinned.upper() != "02:00:00:00:00:51",
   "specifically, it does not pin the strongest 5 GHz one")

# The safety property: a stronger access point that cannot actually be
# reached must not leave the user pinned to somewhere unreachable.
m._save_settings(dict(base_ap))
g = Mesh(MESH, "02:00:00:00:00:11", reachable={"02:00:00:00:00:11"})
r = asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "02:00:00:00:00:11",
   "an unreachable access point is rolled back to the one that worked")
ok(r.get("success") is True and "could not be reached" in r.get("message",""),
   "and the user is told why it stayed put")

# The failure this was written for: one SSID served by a strong pair that
# only speaks WPA2 and weaker nodes that also offer WPA3. A profile saved as
# WPA3-only cannot associate with the strong ones however loud they are, and
# choosing one produced a reconnect that could only ever fail.
m._save_settings(dict(base_ap))
g = Mesh(MIXED, "02:00:00:00:00:11", key_mgmt="sae")
asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "02:00:00:00:00:52",
   "a WPA3-only profile moves to the best access point that offers WPA3")
ok(g.pinned.upper() != "02:00:00:00:00:51",
   "and never to the stronger one it could not authenticate with")

# The same network from a profile that CAN use WPA2 should take the strong one.
m._save_settings(dict(base_ap))
g = Mesh(MIXED, "02:00:00:00:00:11", key_mgmt="wpa-psk")
asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "02:00:00:00:00:51",
   "a WPA2 profile is free to take the strongest access point")

# Unreadable key management must not filter everything away.
m._save_settings(dict(base_ap))
g = Mesh(MIXED, "02:00:00:00:00:11", key_mgmt="")
asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "02:00:00:00:00:51",
   "unknown key management leaves the choice as it was")

accepts = getattr(m.Plugin, "_ap_accepts", None)
for sec, km, want, label in [
    ("WPA2", "sae", False, "WPA2 only cannot take a WPA3 profile"),
    ("WPA2 WPA3", "sae", True, "WPA2/WPA3 can"),
    ("WPA2", "wpa-psk", True, "WPA2 takes a WPA2 profile"),
    ("", "none", True, "an open network takes an open profile"),
    ("WPA2", "none", False, "an open profile does not take a secured access point"),
]:
    ok((accepts(sec, km) if accepts else None) is want, label)
# Scanning takes the radio off-channel, so a link that is already healthy
# should not be disturbed to look for a marginally better one.
m._save_settings(dict(base_ap))
g = Mesh(MESH, "02:00:00:00:00:11", signal_dbm=-45)
asyncio.run(g.set_bssid_lock(True))
ok(g.scans == 0, "a strong link is not scanned around")
ok(g.pinned.upper() == "02:00:00:00:00:11",
   "and it locks to the access point already in use")

m._save_settings(dict(base_ap))
g = Mesh(MESH, "02:00:00:00:00:11", signal_dbm=-82)
asyncio.run(g.set_bssid_lock(True))
ok(g.scans > 0, "a weak link is still looked at properly")
ok(g.pinned.upper() == "02:00:00:00:00:51",
   "and still moves to the stronger access point")

# An unreadable signal must fall through to looking, not silently skip it.
m._save_settings(dict(base_ap))
g = Mesh(MESH, "02:00:00:00:00:11", signal_dbm=None)
asyncio.run(g.set_bssid_lock(True))
ok(g.scans > 0, "an unreadable signal falls through to scanning")

# Nothing to compare against: if the scan cannot see what we are on, staying
# put is the only defensible choice.
m._save_settings(dict(base_ap))
g = Mesh(MESH, "11:22:33:44:55:66")
asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "11:22:33:44:55:66",
   "an access point the scan cannot see is left alone")



section("IPv6: undo what we did, report what we did not")

class V6(Connected):
    def __init__(self, live="disabled"):
        super().__init__()
        self.live, self.mods = live, []
    def _run_cmd(self, cmd, timeout=5):
        if "ipv6.method" in " ".join(cmd) and "con" in cmd:
            return {"success":True,"stdout":f"ipv6.method:{self.live}",
                    "stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout)
    def _nmcli_modify(self, u, k, v, timeout=5):
        self.mods.append((u, k, v))
        return {"success":True,"stdout":"","stderr":"","returncode":0}
    def _hard_reconnect(self, uuid): return True

U6 = "aa0fd3f5-5fba-4291-8cf0-3b38838338d3"

# Turning it off must record WHERE, or the only profile we can ever undo is
# whichever happens to be active later.
m._save_settings(dict(base_ap))
v = V6()
asyncio.run(v.set_ipv6(True))
ok(U6 in m._load_settings().get("ipv6_uuids", []),
   "disabling IPv6 records the profile it was written to")

asyncio.run(v.set_ipv6(False))
ok(U6 not in m._load_settings().get("ipv6_uuids", []),
   "re-enabling drops it from the record")

# A profile we know we disabled, with the toggle now off, is put back.
m._save_settings({**base_ap, "ipv6_disabled":False, "ipv6_uuids":[U6]})
v = V6(live="disabled")
asyncio.run(v.get_status())
ok((U6, "ipv6.method", "auto") in v.mods,
   "a profile we disabled is returned to auto once the toggle is off")

# A profile we have no record of is REPORTED, never rewritten. Switching
# IPv6 off is an ordinary thing to have done deliberately elsewhere.
m._save_settings({**base_ap, "ipv6_disabled":False, "ipv6_uuids":[]})
v = V6(live="disabled")
st = asyncio.run(v.get_status())
ok(not any(k == "ipv6.method" for _, k, _ in v.mods),
   "an IPv6 setting we did not make is left alone")
ok(st.get("external", {}).get("ipv6") is True,
   "but the disagreement is surfaced rather than hidden")
ok(not st.get("drift", {}).get("ipv6"),
   "and it is not called drift, which would blame us for it")

# And no false alarm when they agree.
m._save_settings({**base_ap, "ipv6_disabled":False, "ipv6_uuids":[]})
v = V6(live="auto")
st = asyncio.run(v.get_status())
ok(not st.get("drift", {}).get("ipv6") and not st.get("external", {}).get("ipv6"),
   "nothing reported when the connection and the toggle agree")



section("updates: which direction does this move me")

older = getattr(m.Plugin, "_is_older", None)
cases = [
    ("0.11.6", "0.12.0-beta", True,  "the published stable is older than an installed beta"),
    ("0.12.2-beta", "0.12.2", True,  "a prerelease is older than its own release"),
    ("0.12.2", "0.12.2-beta", False, "and that release is newer than the prerelease"),
    ("0.12.1-beta", "0.12.2-beta", True, "betas order by number"),
    ("0.13.0", "0.12.9", False,      "a higher minor is newer"),
    ("0.12.0", "0.12.0", False,      "the same version is not older than itself"),
]
for cand, than, want, label in cases:
    got = older(m.Plugin, cand, than) if older else None
    ok(got is want, label)

# The live case that prompted this: beta serving an older build than the one
# installed, offered as though it were an upgrade.
class Chan(m.Plugin):
    def __init__(self, latest): super().__init__(); self.latest = latest
    def _run_cmd(self, cmd, timeout=5):
        return {"success":True,"stdout":json.dumps({"version":self.latest}),
                "stderr":"","returncode":0}
# The installed version comes from decky at runtime; without setting it the
# harness compares against 0.0.0 and nothing is ever a downgrade.
m.decky.DECKY_PLUGIN_VERSION = "0.12.0-beta"
m._save_settings({**base_ap, "update_channel":"beta"})
r = asyncio.run(Chan("0.11.6").check_for_update())
ok(r.get("update_available") is True, "an older published build is still offered")
ok(r.get("is_downgrade") is True, "but it is labelled as going backwards")

m._save_settings({**base_ap, "update_channel":"beta"})
r = asyncio.run(Chan("99.0.0").check_for_update())
ok(r.get("update_available") is True and not r.get("is_downgrade"),
   "a genuinely newer build is not labelled a downgrade")

section("access point lock: one scan is not evidence")

# A real Deck read the SAME access point at 35, 39, 45 and 61 across
# consecutive scans. A decision taken on one sample is a decision taken on
# noise, so the signal is averaged over several.
class Noisy(Mesh):
    def __init__(self, rounds, on):
        super().__init__(rounds[0], on)
        self.rounds, self.round_at, self.rescans = rounds, 0, 0
    def _run_cmd(self, cmd, timeout=5):
        if "wifi" in cmd and "list" in cmd:
            if "--rescan" in cmd: self.rescans += 1
            self.scan = self.rounds[min(self.round_at, len(self.rounds) - 1)]
            self.round_at += 1
        return super()._run_cmd(cmd, timeout)

LOW  = "Net:02\\:00\\:00\\:00\\:00\\:52:40:5220 MHz:WPA2 WPA3\nNet:02\\:00\\:00\\:00\\:00\\:11:45:5745 MHz:WPA2 WPA3"
HIGH = "Net:02\\:00\\:00\\:00\\:00\\:52:90:5220 MHz:WPA2 WPA3\nNet:02\\:00\\:00\\:00\\:00\\:11:45:5745 MHz:WPA2 WPA3"

m._save_settings(dict(base_ap))
g = Noisy([LOW, HIGH], "02:00:00:00:00:11")
aps = getattr(g, "_sampled_access_points", lambda *a: [])("wlan0", "Net")
by = {a[0]: a[1] for a in aps}
ok(by.get("02:00:00:00:00:52") == 65,
   "a noisy access point is averaged across samples, not taken at its best")
ok(by.get("02:00:00:00:00:11") == 45,
   "a steady one keeps its value")
ok(g.rescans >= 2,
   "each sample forces a real scan rather than re-reading one cache")

# Averaging has to be able to change the outcome, or it is decoration.
m._save_settings(dict(base_ap))
g = Noisy([LOW, LOW], "02:00:00:00:00:11")
asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "02:00:00:00:00:11",
   "two low samples leave the lock where it is")

# An access point seen in only one sample is still judged on what was seen.
ONLY_ONCE = "Net:02\\:00\\:00\\:00\\:00\\:11:45:5745 MHz:WPA2 WPA3"
m._save_settings(dict(base_ap))
g = Noisy([HIGH, ONLY_ONCE], "02:00:00:00:00:11")
aps = getattr(g, "_sampled_access_points", lambda *a: [])("wlan0", "Net")
ok(any(a[0] == "02:00:00:00:00:52" and a[1] == 90 for a in aps),
   "an access point missing from a sample is judged on the samples it appeared in")

section("access point lock: do not keep retrying one that refuses us")

# Measured on hardware: a mesh node visible at signal 56 answered no
# authentication at all. A loud beacon says the radio can hear the access
# point, not that it can be heard back, and a scan cannot tell the two apart.
m._save_settings(dict(base_ap))
g = Mesh(MESH, "02:00:00:00:00:11", reachable={"02:00:00:00:00:11"})
r = asyncio.run(g.set_bssid_lock(True))
ok(g.pinned.upper() == "02:00:00:00:00:11", "it falls back when refused")
failed = m._load_settings().get("ap_move_failures", {})
ok("02:00:00:00:00:51" in failed,
   "the access point that refused us is remembered")

# The whole point: the next attempt must not repeat the same failed reconnect.
g2 = Mesh(MESH, "02:00:00:00:00:11", reachable={"02:00:00:00:00:11"})
asyncio.run(g2.set_bssid_lock(True))
ok(g2.pinned.upper() == "02:00:00:00:00:11", "still lands somewhere usable")
ok(not any("Locking to" in str(c) for c in g2.mods),
   "and does not write the refused address again")
chose = getattr(g2, "_preferred_access_point")(
    "wlan0", "Net", "02:00:00:00:00:11", m._load_settings(), "", "u1")
ok(chose is None or chose[0] != "02:00:00:00:00:51",
   "the refused access point is not chosen again while it is in cooldown")

# A stale or malformed record must not silently veto an access point.
m._save_settings({**base_ap, "ap_move_failures": {
    "02:00:00:00:00:51": 1,               # long expired
    "not-a-bssid": 99999999999,
    "02:00:00:00:00:24": "yesterday",
}})
g3 = Mesh(MESH, "02:00:00:00:00:11")
refused = g3._recent_ap_failures(m._load_settings())
ok(refused == set(),
   "expired and malformed failure records are ignored")
asyncio.run(g3.set_bssid_lock(True))
ok(g3.pinned.upper() == "02:00:00:00:00:51",
   "so a once-refused access point can be tried again later")

section("one network saved twice, disagreeing about security")

# NetworkManager keeps several profiles for one SSID happily, and with equal
# priority has no basis to prefer any. When they differ in security each can
# reach a DIFFERENT set of access points, so which one it picks decides what
# is even reachable. Measured on a real device: sae and wpa-psk side by side.
class Dup(Connected):
    def __init__(self, rivals):
        super().__init__()
        self.rivals = rivals   # {uuid: (name, ssid, key_mgmt)}
        self.listings = 0
    def _run_cmd(self, cmd, timeout=5):
        j = " ".join(cmd)
        if "UUID,NAME,TYPE" in j:
            self.listings += 1
            rows = [f"{U}:Net:802-11-wireless"]
            rows += [f"{u}:{v[0]}:802-11-wireless" for u, v in self.rivals.items()]
            return {"success":True,"stdout":"\n".join(rows),"stderr":"","returncode":0}
        if "802-11-wireless-security.key-mgmt" in j:
            for u, v in self.rivals.items():
                if u in cmd: return {"success":True,
                    "stdout":f"802-11-wireless-security.key-mgmt:{v[2]}",
                    "stderr":"","returncode":0}
            return {"success":True,"stdout":"802-11-wireless-security.key-mgmt:sae",
                    "stderr":"","returncode":0}
        if "802-11-wireless.ssid" in j:
            for u, v in self.rivals.items():
                if u in cmd: return {"success":True,
                    "stdout":f"802-11-wireless.ssid:{v[1]}","stderr":"","returncode":0}
            return {"success":True,"stdout":"802-11-wireless.ssid:Net",
                    "stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout)
    def _nmcli_modify(self, *a, **k):
        return {"success":True,"stdout":"","stderr":"","returncode":0}

m._save_settings(dict(base_ap))
d = Dup({"bbbbbbbb-0000-0000-0000-000000000001": ("Net", "Net", "wpa-psk")})
st = asyncio.run(d.get_status())
ok(st.get("external", {}).get("duplicate_profiles") is True,
   "a second copy with different security is reported")

# A duplicate that AGREES is not this problem and must not be cried wolf over.
m._save_settings(dict(base_ap))
d = Dup({"bbbbbbbb-0000-0000-0000-000000000001": ("Net", "Net", "sae")})
st = asyncio.run(d.get_status())
ok(not st.get("external", {}).get("duplicate_profiles"),
   "a copy that agrees about security is not reported")

# A different network that happens to share nothing must not count.
m._save_settings(dict(base_ap))
d = Dup({"bbbbbbbb-0000-0000-0000-000000000001": ("Elsewhere", "Elsewhere", "wpa-psk")})
st = asyncio.run(d.get_status())
ok(not st.get("external", {}).get("duplicate_profiles"),
   "another network is not mistaken for a duplicate")

# It must not re-enumerate every poll.
m._save_settings(dict(base_ap))
d = Dup({"bbbbbbbb-0000-0000-0000-000000000001": ("Net", "Net", "wpa-psk")})
asyncio.run(d.get_status()); first = d.listings
asyncio.run(d.get_status()); asyncio.run(d.get_status())
ok(d.listings == first, "the check is throttled rather than run every poll")

section("pins left on another saved copy of the same network")

# NetworkManager is free to autoconnect with any copy of an SSID. A pin left
# on a copy we are not using is invisible twice over - the panel reports the
# feature off, and the profile being read is not the profile it was written
# to - yet NetworkManager honours it the moment that copy is chosen.
RIVAL = "bbbbbbbb-0000-0000-0000-000000000001"

class RivalPins(Dup):
    def __init__(self, rivals, pins):
        super().__init__(rivals)
        self.pins = {u: dict(v) for u, v in pins.items()}
        self.modifies = []
    def _run_cmd(self, cmd, timeout=5):
        j = " ".join(cmd)
        if "802-11-wireless.band,802-11-wireless.bssid" in j:
            for u, props in self.pins.items():
                if u in cmd:
                    return {"success":True,
                            "stdout":"\n".join(f"{k}:{v}" for k, v in props.items()),
                            "stderr":"","returncode":0}
            return {"success":True,"stdout":"","stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout)
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.modifies.append((uuid, key, value))
        return {"success":True,"stdout":"","stderr":"","returncode":0}

def cleared(d, uuid, prop):
    return any(u == uuid and k == prop and v == "" for u, k, v in d.modifies)

ONE = {RIVAL: ("Net", "Net", "wpa-psk")}

# The measured case: a rival copy carrying band=a that nothing tracks.
OWNED = ["802-11-wireless.band", "802-11-wireless.bssid"]
m._save_settings({**base_ap, "pins_owned":OWNED})
d = RivalPins(ONE, {RIVAL: {"802-11-wireless.band": "a"}})
asyncio.run(d.get_status())
ok(cleared(d, RIVAL, "802-11-wireless.band"),
   "a band pin left on another copy of the same network is cleared")

# The same for an address pin.
m._save_settings({**base_ap, "pins_owned":OWNED})
d = RivalPins(ONE, {RIVAL: {"802-11-wireless.bssid": "02:00:00:00:00:11"}})
asyncio.run(d.get_status())
ok(cleared(d, RIVAL, "802-11-wireless.bssid"),
   "an address pin left on another copy is cleared")

# And neither, when nothing shows the plugin ever wrote that kind of pin.
m._save_settings({**base_ap, "pins_owned":[]})
d = RivalPins(ONE, {RIVAL: {"802-11-wireless.band": "a",
                            "802-11-wireless.bssid": "02:00:00:00:00:11"}})
asyncio.run(d.get_status())
ok(not cleared(d, RIVAL, "802-11-wireless.band")
   and not cleared(d, RIVAL, "802-11-wireless.bssid"),
   "a rival copy's hand-set pins are left alone")

# While the preference is ON that value is the user's, not litter.
m._save_settings({**base_ap, "band_preference_enabled":True,
                  "band_preference":"a", "band_preference_ssid":"Net"})
d = RivalPins(ONE, {RIVAL: {"802-11-wireless.band": "a"}})
asyncio.run(d.get_status())
ok(not cleared(d, RIVAL, "802-11-wireless.band"),
   "a rival pin is left alone while the preference is still on")

# The elimination boundary: IPv6 is an ordinary thing to have switched off
# deliberately elsewhere, so a copy we cannot prove we wrote is never
# overridden - unlike a band or an address, which nobody sets by hand.
m._save_settings(dict(base_ap))
d = RivalPins(ONE, {RIVAL: {"802-11-wireless.band": "a",
                            "ipv6.method": "disabled"}})
asyncio.run(d.get_status())
ok(not any(k == "ipv6.method" for _u, k, _v in d.modifies),
   "IPv6 on another copy is never overridden")

# A copy of a DIFFERENT network is inert until the device goes there.
m._save_settings(dict(base_ap))
d = RivalPins({RIVAL: ("Elsewhere", "Elsewhere", "wpa-psk")},
              {RIVAL: {"802-11-wireless.band": "a"}})
asyncio.run(d.get_status())
ok(not any(u == RIVAL for u, _k, _v in d.modifies),
   "a copy of another network is not touched")

# One write, not one per poll: the cache is consumed optimistically and the
# throttled duplicate check re-reads the truth.
m._save_settings(dict(base_ap))
d = RivalPins(ONE, {RIVAL: {"802-11-wireless.band": "a"}})
asyncio.run(d.get_status()); first = len(d.modifies)
asyncio.run(d.get_status()); asyncio.run(d.get_status())
ok(len(d.modifies) == first, "the rival cleanup is not re-attempted every poll")

section("a pin that is keeping the device offline is released")

# GitHub issue #6: "Optimize Safe" pins the access point in use, the access
# point later stops being reachable, and nothing reconsiders it. The ordinary
# cleanup runs while disconnected on purpose but skips any property whose
# feature is still ENABLED - which is exactly when the pin exists. The user is
# left re-entering credentials, which makes a second copy of the network.
OFFU = "cccccccc-0000-0000-0000-000000000001"

# The network is in range on an access point no pin below names, on 2.4 GHz,
# so both an address pin and a 5 GHz band pin rule it out.
IN_RANGE = "TestNet:AA\\:BB\\:CC\\:00\\:00\\:01:70:2437 MHz"

class Offline(Connected):
    """No active connection, over a saved profile carrying the given pins."""
    def __init__(self, pins, state="disconnected", radio="enabled", scan=IN_RANGE,
                 active=None):
        super().__init__()
        self.pins = dict(pins)
        self.modifies = []
        self.state, self.radio, self.scan, self.active = state, radio, scan, active
    def _get_active_connection_uuid(self):
        return self.active
    def _run_cmd(self, cmd, timeout=5):
        j = " ".join(cmd)
        if "802-11-wireless.band,802-11-wireless.bssid" in j:
            return {"success":True,
                    "stdout":"\n".join(f"{k}:{v}" for k,v in self.pins.items()),
                    "stderr":"","returncode":0}
        if "STATE,WIFI general" in j:
            return {"success":True,"stdout":f"{self.state}:{self.radio}",
                    "stderr":"","returncode":0}
        if "SSID,BSSID,SIGNAL,FREQ dev wifi list" in j:
            return {"success":True,"stdout":self.scan,"stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout)
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.modifies.append((uuid, key, value))
        return {"success":True,"stdout":"","stderr":"","returncode":0}

def off_settings(**extra):
    base = {**base_ap, "bssid_lock_enabled": True,
            "bssid_lock_connection_uuid": OFFU, "bssid_lock_uuids": [OFFU],
            "last_connection_uuid": OFFU}
    base.update(extra)
    return base

def rel(d, prop):
    return any(u == OFFU and k == prop and v == "" for u, k, v in d.modifies)

def strand(d):
    """Run one poll, then age the offline clock past the threshold and re-poll."""
    asyncio.run(d.get_status())
    d._offline_since = time.monotonic() - (d._STRANDED_SECONDS + 5)
    return asyncio.run(d.get_status())

# A brief outage must not cost anyone their lock.
m._save_settings(off_settings())
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
asyncio.run(d.get_status())
ok(not rel(d, "802-11-wireless.bssid"),
   "a pin is not released the moment the device goes offline")

# Past the threshold it is, and the user is told why.
m._save_settings(off_settings())
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
st = strand(d)
ok(rel(d, "802-11-wireless.bssid"),
   "a pin we wrote is released once it is the likely reason we are offline")
ok(st.get("external", {}).get("pin_released") is True,
   "and the panel is told, so the change is not silent")
ok(getattr(d, "_repoll_after_release", False) is True,
   "and the background watch is asked to re-settle the lock once back online")

# A band preference strands a device the same way.
m._save_settings(off_settings(band_preference_enabled=True, band_preference="a",
                              band_preference_ssid="TestNet",
                              band_preference_uuids=[OFFU]))
d = Offline({"802-11-wireless.band": "a"})
strand(d)
ok(rel(d, "802-11-wireless.band"), "a stranding band preference is released too")

# Only what we recorded writing.
m._save_settings(off_settings(bssid_lock_uuids=[]))
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
strand(d)
ok(not rel(d, "802-11-wireless.bssid"),
   "a pin on a profile we never recorded writing is left alone")

# The elimination boundary holds here too.
m._save_settings(off_settings(ipv6_disabled=True, ipv6_uuids=[OFFU]))
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11",
             "ipv6.method": "disabled"})
strand(d)
ok(not any(k == "ipv6.method" for _u, k, _v in d.modifies),
   "IPv6 is never released this way")

# Never fight a setter that is mid-change - its own radio cycle looks exactly
# like being offline.
m._save_settings(off_settings())
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
d._profile_change_in_flight = lambda: True
strand(d)
ok(not rel(d, "802-11-wireless.bssid"),
   "nothing is released while a setter is mid-change")

# Nothing written when there is no pin to release.
m._save_settings(off_settings())
d = Offline({})
strand(d)
ok(not d.modifies, "a profile with no pin is not written to needlessly")

# Every profile we pinned, not just the last one in use. The profile a device
# fails to rejoin is often not the one it last held.
OTHER = "cccccccc-0000-0000-0000-000000000002"
m._save_settings(off_settings(bssid_lock_uuids=[OTHER, OFFU],
                              last_connection_uuid=OTHER))
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
strand(d)
ok(rel(d, "802-11-wireless.bssid"),
   "a pinned profile that is not the last one used is released too")

# An upgrade from 0.11 records only the lock's own profile, in a field the
# tracking list never saw. With evidence the plugin wrote locks, it counts.
m._save_settings(off_settings(bssid_lock_uuids=[], last_connection_uuid="",
                              pins_owned=["802-11-wireless.bssid"]))
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
strand(d)
ok(rel(d, "802-11-wireless.bssid"),
   "the lock's own profile is covered when the plugin is known to write locks")

# A profile waiting on a password is ACTIVATING, which is exactly where a pin
# that cannot associate leaves it. Treating that as online reset the clock
# for as long as the prompt sat there.
m._save_settings(off_settings())
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"},
            state="connecting", active=OFFU)
strand(d)
ok(rel(d, "802-11-wireless.bssid"),
   "a profile stuck activating counts as offline, so the clock runs")

# The pin must be what is keeping it off. Each of these is offline for some
# other reason, and releasing then re-pointed the lock to whatever came back.
for label, kw in [
    ("the radio is off",          {"radio": "disabled"}),
    ("the network is not in range", {"scan": "Elsewhere:AA\\:BB\\:CC\\:00\\:00\\:09:70:2437 MHz"}),
    ("only the pinned access point is in range",
                                  {"scan": "TestNet:02\\:00\\:00\\:00\\:00\\:11:70:5180 MHz"}),
    ("NetworkManager cannot answer", {"state": "", "radio": ""}),
    ("the system is asleep",      {"state": "asleep"}),
]:
    m._save_settings(off_settings())
    d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"}, **kw)
    strand(d)
    ok(not rel(d, "802-11-wireless.bssid"), f"nothing is released when {label}")

m._save_settings(off_settings(bssid_lock_enabled=False, band_preference_enabled=True,
                              band_preference="a", band_preference_ssid="TestNet",
                              band_preference_uuids=[OFFU]))
d = Offline({"802-11-wireless.band": "a"},
            scan="TestNet:AA\\:BB\\:CC\\:00\\:00\\:01:70:5180 MHz")
strand(d)
ok(not rel(d, "802-11-wireless.band"),
   "a band pin is kept while the network is in range on that band")

# A router lists its other band and a mesh its other nodes, so "some access
# point the pin excludes is visible" is true on nearly every network, and
# released the lock on any outage at all (a changed password, DHCP failing).
DUAL = ("TestNet:02\\:00\\:00\\:00\\:00\\:11:70:5180 MHz\n"
        "TestNet:02\\:00\\:00\\:00\\:00\\:12:65:2437 MHz")
m._save_settings(off_settings())
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"}, scan=DUAL)
strand(d)
ok(not rel(d, "802-11-wireless.bssid"),
   "a pinned access point still in range at full strength is not released")
FAR = ("TestNet:02\\:00\\:00\\:00\\:00\\:11:20:5180 MHz\n"
       "TestNet:02\\:00\\:00\\:00\\:00\\:12:80:5745 MHz")
m._save_settings(off_settings())
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"}, scan=FAR)
strand(d)
ok(rel(d, "802-11-wireless.bssid"),
   "but one far weaker than another node of the network is")
m._save_settings(off_settings(bssid_lock_enabled=False, band_preference_enabled=True,
                              band_preference="a", band_preference_ssid="TestNet",
                              band_preference_uuids=[OFFU]))
d = Offline({"802-11-wireless.band": "a"}, scan=DUAL)
strand(d)
ok(not rel(d, "802-11-wireless.band"),
   "a band pin on a dual-band network with that band in range is kept")

# A backend switch or an update takes WiFi down on purpose.
for label, setup in [
    ("a backend switch", lambda d: setattr(d, "_backend_switch", {"in_progress": True})),
    ("an update", lambda d: setattr(d, "_update_starting", True)),
]:
    m._save_settings(off_settings())
    d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
    setup(d)
    strand(d)
    ok(not rel(d, "802-11-wireless.bssid") and d._offline_since is None,
       f"{label} neither releases nor runs the offline clock")

# Reconnecting restarts the clock, so outages do not accumulate.
m._save_settings(off_settings())
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"})
d = Offline({"802-11-wireless.bssid": "02:00:00:00:00:11"}, state="connected")
d._offline_since = time.monotonic() - 999
out = d._propose_stranded_pin_release(m._load_settings(), True)
ok(out == [] and getattr(d, "_offline_since", "unset") is None,
   "reconnecting resets the offline clock")

section("stranded pins: released with the panel closed")

# The release lived only inside a status poll, and only the panel polls. The
# device it is for is one that woke unable to reach its pinned access point,
# where nobody opens the panel. The watch makes the same poll on its own.
class Watched(m.Plugin):
    def __init__(self, state, ok_=True):
        super().__init__(); self.state = state; self.ok_ = ok_
        self.nm_calls = 0; self.polls = 0
    def _run_cmd(self, cmd, timeout=5):
        if cmd == ["/usr/bin/nmcli", "-t", "-f", "STATE,WIFI", "general"]:
            self.nm_calls += 1
            out = self.state if ":" in self.state else f"{self.state}:enabled"
            return {"success": self.ok_, "stdout": out, "stderr": "", "returncode": 0}
        return {"success": False, "stdout": "", "stderr": "", "returncode": 1}
    async def get_status(self):
        self.polls += 1
        return {}

m._save_settings(off_settings(bssid_lock_enabled=False, band_preference_enabled=False))
w = Watched("disconnected")
asyncio.run(w._stranded_watch_once())
ok(w.nm_calls == 0 and w.polls == 0,
   "with no pin feature on, the watch costs nothing at all")

for flag in ("bssid_lock_enabled", "band_preference_enabled"):
    m._save_settings(off_settings(**{"bssid_lock_enabled": False,
                                     "band_preference_enabled": False, flag: True}))
    w = Watched("disconnected")
    asyncio.run(w._stranded_watch_once())
    ok(w.polls == 1, f"offline with {flag} on runs the same poll the panel would")

m._save_settings(off_settings(ipv6_disabled=True, bssid_lock_enabled=False,
                               band_preference_enabled=False))
w = Watched("disconnected")
asyncio.run(w._stranded_watch_once())
ok(w.polls == 0, "IPv6 alone does not wake the watch, since it is never released")

m._save_settings(off_settings())
for state in ("connected", "connected (site only)", "connected (local only)", "asleep"):
    w = Watched(state)
    w._offline_since = time.monotonic() - 999
    asyncio.run(w._stranded_watch_once())
    ok(w.polls == 0 and w._offline_since is None,
       f"{state!r} resets the offline clock and polls nothing")

for state in ("connecting", "disconnecting"):
    w = Watched(state)
    asyncio.run(w._stranded_watch_once())
    ok(w.polls == 1, f"{state!r} counts as offline, so a pin failing auth in a loop is caught")

w = Watched("connected")
w._repoll_after_release = True
asyncio.run(w._stranded_watch_once())
asyncio.run(w._stranded_watch_once())
ok(w.polls == 1 and not w._repoll_after_release,
   "back online after a release, one poll lets the lock settle on what works")
w = Watched("asleep")
w._repoll_after_release = True
asyncio.run(w._stranded_watch_once())
ok(w.polls == 0 and w._repoll_after_release,
   "but not while asleep, which is not back online")

w = Watched("disconnected:disabled")
w._offline_since = 123.0
asyncio.run(w._stranded_watch_once())
ok(w.polls == 0 and w._offline_since is None,
   "the radio switched off resets the clock and polls nothing")

w = Watched("", ok_=False)
w._offline_since = 123.0
asyncio.run(w._stranded_watch_once())
ok(w.polls == 0 and w._offline_since == 123.0,
   "an nmcli that cannot answer changes nothing either way")

# Wired to the plugin's life: started by _main, cancelled by _unload.
_src = open("main.py").read()
_main_body = _src[_src.index("    async def _main(self):"):_src.index("    async def _unload(self):")]
_unload_body = _src[_src.index("    async def _unload(self):"):_src.index("    async def _uninstall(self):")]
ok("self._stranded_task = asyncio.create_task(self._stranded_watch())" in _main_body,
   "_main starts the watch")
ok('"_stranded_task"' in _unload_body, "and _unload cancels it")

section("one network saved twice: making NetworkManager's choice deterministic")

# Writing a fixed priority on whichever profile is active looks like it
# establishes precedence and does the opposite: each copy is active at some
# point, each gets the same number, and NM is left deciding on last-used
# timestamp - which flips. Measured on a real device: both copies at 100.
RIV = "bbbbbbbb-0000-0000-0000-000000000002"

class Prio(Dup):
    def __init__(self, rivals, priorities):
        super().__init__(rivals)
        self.priorities = dict(priorities)
        self.modifies = []
    def _run_cmd(self, cmd, timeout=5):
        j = " ".join(cmd)
        if "connection.autoconnect-priority" in j and "show" in j:
            for u, v in self.priorities.items():
                if u in cmd:
                    return {"success":True,
                            "stdout":f"connection.autoconnect-priority:{v}",
                            "stderr":"","returncode":0}
            return {"success":True,"stdout":"connection.autoconnect-priority:0",
                    "stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout)
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.modifies.append((uuid, key, value))
        return {"success":True,"stdout":"","stderr":"","returncode":0}

def prio_writes(d):
    return [(u, v) for u, k, v in d.modifies
            if k == "connection.autoconnect-priority"]

ONE_RIVAL = {RIV: ("Net", "Net", "wpa-psk")}

m._save_settings({**base_ap, "priority_set": False})
d = Prio(ONE_RIVAL, {U: 100, RIV: 100})
asyncio.run(d.get_status())
ok(prio_writes(d) == [(U, "101")],
   "the active profile is raised above a duplicate sharing its priority")
ok(not any(u == RIV for u, _v in prio_writes(d)),
   "and the duplicate is read but never written to")

m._save_settings({**base_ap, "priority_set": False})
d = Prio(ONE_RIVAL, {U: 150, RIV: 100})
asyncio.run(d.get_status())
ok(prio_writes(d) == [],
   "a profile that already outranks its duplicate is left alone")

m._save_settings({**base_ap, "priority_set": False})
d = Prio({}, {U: 0})
asyncio.run(d.get_status())
ok(prio_writes(d) == [(U, "100")],
   "a network with a single saved copy gets the base priority")

m._save_settings({**base_ap, "priority_set": False})
d = Prio({}, {U: 100})
asyncio.run(d.get_status())
ok(prio_writes(d) == [], "and is not rewritten once it already has it")

for mine in (-10, 500):
    m._save_settings({**base_ap, "priority_set": False})
    d = Prio({}, {U: mine})
    asyncio.run(d.get_status())
    ok(prio_writes(d) == [],
       f"a single copy whose priority was set deliberately ({mine}) is left alone")

# Every existing install is already marked done, ties and all, so the one-shot
# flag must not be allowed to freeze the tie in place forever.
# last_connection_uuid must match, or the collector resets the flag and the
# early path handles it - which is not the state a real install is in.
m._save_settings({**base_ap, "priority_set": True, "last_connection_uuid": U})
d = Prio(ONE_RIVAL, {U: 100, RIV: 100})
asyncio.run(d.get_status())
ok(prio_writes(d) == [(U, "101")],
   "an install already marked done still gets a settled tie corrected")

m._save_settings({**base_ap, "priority_set": True, "last_connection_uuid": U})
d = Prio(ONE_RIVAL, {U: 150, RIV: 100})
asyncio.run(d.get_status())
ok(prio_writes(d) == [],
   "and one that already outranks is still not written to")

m._save_settings({**base_ap, "priority_set": False})
d = Prio(ONE_RIVAL, {U: 0, RIV: 999})
asyncio.run(d.get_status())
ok(prio_writes(d) == [(U, "999")],
   "the priority never exceeds NetworkManager's ceiling")

section("wireless region: reading it honestly, and setting it safely")

# Verbatim shape of a Steam Deck OLED, measured 2026-09-12. The global block
# is printed first and says country 00 with everything PASSIVE-SCAN, while the
# phy carries its own US domain with the full 6 GHz range. Reading the first
# block and stopping is exactly the misreading this parser exists to prevent.
SELF_MANAGED = """global
country 00: DFS-UNSET
	(2402 - 2472 @ 40), (N/A, 20), (N/A)
	(5170 - 5250 @ 80), (N/A, 20), (N/A), AUTO-BW, PASSIVE-SCAN

phy#0 (self-managed)
country US: DFS-FCC
	(5170 - 5250 @ 80), (6, 24), (N/A), AUTO-BW
	(5925 - 7125 @ 160), (N/A, 24), (N/A), NO-OUTDOOR, AUTO-BW
"""

# A radio that does NOT manage its own domain: the global entry governs, and
# this is the device the region selector exists for.
PLAIN = """global
country 00: DFS-UNSET
	(2402 - 2472 @ 40), (N/A, 20), (N/A)

phy#1
country 00: DFS-UNSET
	(2402 - 2472 @ 40), (N/A, 20), (N/A)
"""

r = m.Plugin()._parse_reg(SELF_MANAGED)
ok(r["global"] == "00", "the global domain is read")
ok(r["self_managed"] is True, "a self-managed phy is recognised")
ok(r["governing"] == "US",
   "and its own domain is reported as the one that governs, not the global 00")

r2 = m.Plugin()._parse_reg(PLAIN)
ok(r2["self_managed"] is False, "a phy that manages nothing is not mistaken for one")
ok(r2["governing"] == "00", "and the global domain governs it")

ok(m.Plugin()._parse_reg("")["governing"] == "",
   "unreadable output yields no claim about the region")

class Reg(m.Plugin):
    def __init__(self, reg_text, ok_set=True):
        super().__init__()
        self.reg_text = reg_text
        self.ok_set = ok_set
        self.sets = []
    def _run_cmd(self, cmd, timeout=5):
        if cmd[:3] == ["/usr/bin/iw", "reg", "get"]:
            return {"success":True,"stdout":self.reg_text,"stderr":"","returncode":0}
        if cmd[:3] == ["/usr/bin/iw", "reg", "set"]:
            self.sets.append(cmd[3])
            return {"success":self.ok_set,"stdout":"","stderr":"nope","returncode":0}
        return {"success":True,"stdout":"","stderr":"","returncode":0}

# A regulatory domain is a legal constraint and the value reaches a
# subprocess, so anything that is not a country is refused outright.
for bad in ["US; rm -rf /", "$(id)", "`id`", "USA", "u", "../../etc", "US US"]:
    m._save_settings(dict(base_ap))
    d = Reg(PLAIN)
    res = asyncio.run(d.set_regdomain(bad))
    ok(res.get("success") is False and not d.sets,
       f"region {bad!r} is refused and never reaches iw")

m._save_settings(dict(base_ap))
d = Reg(PLAIN)
res = asyncio.run(d.set_regdomain("de"))
ok(res.get("success") is True and d.sets == ["DE"],
   "a lowercase region is accepted and applied uppercase")
ok(m._load_settings().get("regdomain") == "DE", "and remembered")

# Turning it off restores what was there before we touched it.
m._save_settings(dict(base_ap))
d = Reg(PLAIN)
asyncio.run(d.set_regdomain("DE"))
ok(m._load_settings().get("regdomain_previous") == "00",
   "the region in place before our first change is recorded")
d.sets.clear()
asyncio.run(d.set_regdomain(""))
ok(d.sets == ["00"], "and is restored when the setting is turned off")
ok(m._load_settings().get("regdomain") == "", "with nothing left claiming to manage it")

# The silent no-op is the failure shape to avoid: on a self-managed radio the
# command succeeds and changes nothing, so the result has to say so.
m._save_settings(dict(base_ap))
d = Reg(SELF_MANAGED)
res = asyncio.run(d.set_regdomain("DE"))
ok(res.get("self_managed") is True and bool(res.get("message")),
   "setting a region on a self-managed radio explains that nothing changed")

m._save_settings(dict(base_ap))
d = Reg(SELF_MANAGED)
info = asyncio.run(d.get_regdomain())
ok(info.get("changeable") is False,
   "and the panel is told the setting cannot change anything here")
m._save_settings(dict(base_ap))
ok(asyncio.run(Reg(PLAIN).get_regdomain()).get("changeable") is True,
   "while a radio that does not manage its own domain can be changed")

section("is the wanted band actually reachable")

# Checked before cycling the radio, because a cycle that cannot get back
# leaves the user disconnected, which is worse than the state it was meant to
# improve. It had no coverage, and it parses nmcli terse output by hand.
class Scan(m.Plugin):
    def __init__(self, rows, ssid="Net", scan_ok=True):
        super().__init__()
        self.rows = rows
        self.ssid = ssid
        self.scan_ok = scan_ok
    def _run_cmd(self, cmd, timeout=5):
        j = " ".join(cmd)
        if "SSID,FREQ" in j:
            return {"success": self.scan_ok, "stdout": "\n".join(self.rows),
                    "stderr": "", "returncode": 0}
        if "802-11-wireless.ssid" in j:
            return {"success": True, "stdout": f"802-11-wireless.ssid:{self.ssid}",
                    "stderr": "", "returncode": 0}
        return {"success": True, "stdout": "", "stderr": "", "returncode": 0}

FIVE = "Net:5745 MHz"
TWO  = "Net:2437 MHz"
OTHER_FIVE = "Elsewhere:5745 MHz"

ok(Scan([TWO, FIVE])._band_is_reachable("wlan0", "u", "a") is True,
   "a 5 GHz access point for this network makes the 5 GHz preference reachable")
ok(Scan([TWO])._band_is_reachable("wlan0", "u", "a") is False,
   "a network offering only 2.4 GHz does not")
ok(Scan([TWO])._band_is_reachable("wlan0", "u", "bg") is True,
   "and 2.4 GHz is reachable when that is what was asked for")
ok(Scan([FIVE])._band_is_reachable("wlan0", "u", "bg") is False,
   "while a 5 GHz only network cannot satisfy a 2.4 GHz preference")
ok(Scan([OTHER_FIVE])._band_is_reachable("wlan0", "u", "a") is False,
   "another network's access points do not count")
ok(Scan([FIVE], scan_ok=False)._band_is_reachable("wlan0", "u", "a") is False,
   "a scan that failed is not evidence the band is reachable")
ok(Scan([FIVE], ssid="")._band_is_reachable("wlan0", "u", "a") is False,
   "and neither is a profile whose SSID cannot be read")

# A BSSID is nothing but colons and an SSID may contain one, which is why
# nmcli escapes them. The band check does its own unescaping.
ok(Scan(["My\\:Net:5745 MHz"], ssid="My\\:Net")._band_is_reachable("wlan0", "u", "a") is True,
   "an SSID containing a colon still matches its own access point")
ok(Scan(["My\\:Net:5745 MHz"], ssid="Other")._band_is_reachable("wlan0", "u", "a") is False,
   "and is not confused with a different network")

# The boundaries are NetworkManager's own: band "a" hands wpa_supplicant its
# 5 GHz table, 4915 to 5825 MHz, and nothing on 6 GHz. Counting 6 GHz as "a"
# let the plugin write a band that pushes a 6 GHz device back to 5 GHz.
ok(Scan(["Net:5825 MHz"])._band_is_reachable("wlan0", "u", "a") is True
   and Scan(["Net:4980 MHz"])._band_is_reachable("wlan0", "u", "a") is True,
   "the 5 GHz band runs to the top of NetworkManager's 5 GHz table")
ok(Scan(["Net:5955 MHz"])._band_is_reachable("wlan0", "u", "a") is False
   and Scan(["Net:6115 MHz"])._band_is_reachable("wlan0", "u", "bg") is False,
   "a 6 GHz access point satisfies neither band")
_fb = getattr(m.Plugin, "_freq_band", lambda f: None)
for freq, want in [("2412", "bg"), ("2484 MHz", "bg"), ("5180.0", "a"),
                   ("5825", "a"), ("5955", ""), ("6115 MHz", ""), ("", ""), ("junk", "")]:
    ok(_fb(freq) == want, f"{freq!r} is band {want!r}")
_six = {**m.DEFAULT_SETTINGS, "band_preference_enabled": True,
        "band_preference": "a", "band_preference_ssid": "Net"}
ok(m.Plugin()._band_conflicts(_six, "6115.0", "Net") is True,
   "a 6 GHz association conflicts with a 5 GHz preference, so no address is pinned over it")
ok(m.Plugin()._band_conflicts(_six, "5180.0", "Net") is False,
   "while a 5 GHz one does not")

section("turning the lock off clears it from every profile that carries it")

# This whole branch had NO coverage. It is the undo for a feature that writes
# to more than one profile: the lock follows whichever copy NetworkManager
# actually uses, so over time the address may have been written to several.
# A copy left pinned to an access point nobody is honouring will fail to
# associate if NM ever picks it again, and no control would clear it - which
# is the stranding shape of issue #6, arrived at from the other direction.
LIVE_U  = "dddddddd-0000-0000-0000-00000000000a"
STALE_U = "dddddddd-0000-0000-0000-00000000000b"
GONE_U  = "dddddddd-0000-0000-0000-00000000000c"

class Unlock(m.Plugin):
    """The lock being turned off, over profiles that may refuse the write."""
    def __init__(self, refuse=(), missing=()):
        super().__init__()
        self.refuse = set(refuse)
        self.missing = set(missing)
        self.modifies = []
    def _get_wifi_interface(self): return "wlan0"
    def _get_active_connection_uuid(self): return LIVE_U
    def _hard_reconnect(self, uuid=None): return True
    def _run_cmd(self, cmd, timeout=5):
        return {"success":True,"stdout":"","stderr":"","returncode":0}
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.modifies.append((uuid, key, value))
        if uuid in self.missing:
            return {"success":False,"stdout":"",
                    "stderr":"Error: unknown connection '%s'." % uuid,
                    "returncode":10}
        if uuid in self.refuse:
            return {"success":False,"stdout":"","stderr":"permission denied",
                    "returncode":1}
        return {"success":True,"stdout":"","stderr":"","returncode":0}

def locked_on(*uuids, **extra):
    s = {**base_ap, "bssid_lock_enabled": True, "bssid_lock_value": "02:00:00:00:00:11",
         "bssid_lock_connection_uuid": LIVE_U, "bssid_lock_uuids": list(uuids),
         "last_connection_uuid": LIVE_U}
    s.update(extra)
    return s

def unpinned(d, uuid):
    return (uuid, "802-11-wireless.bssid", "") in d.modifies

m._save_settings(locked_on(LIVE_U))
d = Unlock()
res = asyncio.run(d.set_bssid_lock(False))
ok(res.get("success") is True, "turning the lock off succeeds")
ok(unpinned(d, LIVE_U), "the address is cleared from the profile in use")
after = m._load_settings()
ok(after.get("bssid_lock_enabled") is False and after.get("bssid_lock_value") == "",
   "and the setting stops claiming a lock is held")
ok(after.get("bssid_lock_connection_uuid") == "",
   "and stops naming a profile it is held on")

# The point of the branch: the copies we are NOT on are cleared too.
m._save_settings(locked_on(LIVE_U, STALE_U))
d = Unlock()
asyncio.run(d.set_bssid_lock(False))
ok(unpinned(d, STALE_U), "a stale copy is cleared as well")
ok(m._load_settings().get("bssid_lock_uuids") == [],
   "and nothing is left on the list once every copy is clear")

# The profile the lock was last held on is cleared even when the list has lost
# it - it is the one reference to a profile that would otherwise stay pinned.
m._save_settings(locked_on(LIVE_U, bssid_lock_connection_uuid=STALE_U))
d = Unlock()
asyncio.run(d.set_bssid_lock(False))
ok(unpinned(d, STALE_U),
   "the profile last recorded as locked is cleared even if the list forgot it")

# A profile that is merely gone is resolved, not failed. Keeping it on the
# list would retry a write against a profile that no longer exists forever.
m._save_settings(locked_on(LIVE_U, GONE_U))
d = Unlock(missing=[GONE_U])
asyncio.run(d.set_bssid_lock(False))
ok(m._load_settings().get("bssid_lock_uuids") == [],
   "a profile that no longer exists is dropped from the list")

# Anything else is still pinned, so the record of it has to survive - drop it
# and nothing can ever clear that profile again.
m._save_settings(locked_on(LIVE_U, STALE_U))
d = Unlock(refuse=[STALE_U])
asyncio.run(d.set_bssid_lock(False))
ok(m._load_settings().get("bssid_lock_uuids") == [STALE_U],
   "a copy that refused the write is kept, because it is still pinned")

# Disabling works with no active connection: the pin lives on a saved profile
# and being unable to associate is the likeliest reason to be turning it off.
m._save_settings(locked_on(LIVE_U))
class UnlockOffline(Unlock):
    def _get_active_connection_uuid(self): return None
d = UnlockOffline()
res = asyncio.run(d.set_bssid_lock(False))
ok(res.get("success") is True and unpinned(d, LIVE_U),
   "the lock can be turned off while disconnected")

section("CAKE is reasserted when it drifts")

# The applier had NO coverage. It is the one action that loads a kernel module
# and runs three subprocesses, on the event loop, and the reason it gives up
# after a few failures is that a kernel without sch_cake would otherwise
# block every other call every few seconds forever.
class Cake(m.Plugin):
    def __init__(self, tc_ok=True):
        super().__init__()
        self.tc_ok = tc_ok
        self.ran = []
    def _run_cmd(self, cmd, timeout=5):
        self.ran.append(list(cmd))
        if "qdisc" in cmd and "replace" in cmd:
            return {"success":self.tc_ok,"stdout":"","stderr":"no such qdisc",
                    "returncode":0 if self.tc_ok else 2}
        return {"success":True,"stdout":"","stderr":"","returncode":0}

def cake_action():
    return [{"kind":"cake","uuid":"u1","iface":"wlan0"}]

def ran_any(d, *needles):
    return any(all(n in cmd for n in needles) for cmd in d.ran)

m._save_settings({**m.DEFAULT_SETTINGS, "cake_enabled":True})
d = Cake()
st = {"live":{},"drift":{"cake":True}}
d._apply_status_actions(st, {}, cake_action())
ok(ran_any(d, "qdisc", "replace", "cake"), "the qdisc is replaced")
ok(ran_any(d, "txqueuelen"), "and the queue length is raised with it")
ok(st["live"].get("cake_applied") is True and "cake" not in st["drift"],
   "and the panel is told it is back rather than still drifting")

# sch_cake is loaded once per process, not once per poll. modprobe is looked
# up by absolute path and does not exist on every host this suite runs on, so
# the lookup is stubbed: without it the load is never attempted here and the
# check silently tests nothing.
_real_first_existing = m._first_existing
m._first_existing = lambda paths: "/usr/bin/modprobe"
try:
    m._save_settings({**m.DEFAULT_SETTINGS, "cake_enabled":True})
    d = Cake()
    d._apply_status_actions({"live":{},"drift":{"cake":True}}, {}, cake_action())
    first = sum(1 for c in d.ran if "sch_cake" in c)
    d._apply_status_actions({"live":{},"drift":{"cake":True}}, {}, cake_action())
    ok(first == 1 and sum(1 for c in d.ran if "sch_cake" in c) == 1,
       "the module is loaded once, not on every reassertion")
finally:
    m._first_existing = _real_first_existing

# A kernel that cannot do CAKE: the drift flag stands, which is the honest
# report, and the attempt stops after the limit rather than running forever.
m._save_settings({**m.DEFAULT_SETTINGS, "cake_enabled":True})
d = Cake(tc_ok=False)
for _ in range(d._MAX_REASSERT_FAILURES + 3):
    st = {"live":{},"drift":{"cake":True}}
    d._apply_status_actions(st, {}, cake_action())
ok(st["drift"].get("cake") is True,
   "a kernel without CAKE keeps the drift flag rather than claiming success")
ok(sum(1 for c in d.ran if "replace" in c) <= d._MAX_REASSERT_FAILURES,
   "and the reassertion gives up instead of retrying every poll forever")

# Switched off mid-poll: the drift is dropped, not acted on.
m._save_settings({**m.DEFAULT_SETTINGS, "cake_enabled":False})
d = Cake()
st = {"live":{},"drift":{"cake":True}}
d._apply_status_actions(st, {}, cake_action())
ok(not ran_any(d, "qdisc", "replace") and "cake" not in st["drift"],
   "CAKE turned off mid-poll drops the drift instead of reasserting it")

section("a failed status read still answers with a complete status")

# Nearly uncovered, and the comment on it records a real bug: the panel reads
# settings, live, drift, connected and version unconditionally, so a bare
# error dict renders as "not connected" with every toggle off - telling the
# user their optimizations are gone rather than that the read failed. The
# support tier was once hardcoded here, which made the panel announce
# unrecognised hardware because an unrelated read had failed.
class Broken(m.Plugin):
    def _get_wifi_interface(self):
        raise RuntimeError("nmcli went away")

m._save_settings({**base_ap, "power_save_disabled":True, "cake_enabled":True})
st, pend, acts = Broken()._collect_status()
ok(st.get("success") is False and st.get("error") == "unexpected",
   "the failure is reported as a failure")
for key, kind in [("settings",dict),("live",dict),("drift",dict),
                  ("external",dict),("connected",bool),("version",str)]:
    ok(isinstance(st.get(key), kind), f"the panel's {key} is still present")
ok(st["settings"].get("power_save_disabled") is True,
   "the user's settings are returned, not defaults - the toggles keep their state")
ok(st.get("support_tier") == Broken()._get_support_tier(),
   "the support tier is read rather than guessed, so the hardware is not misreported")
ok(pend == {} and acts == [],
   "and nothing is proposed off the back of a read that failed")

# Settings that cannot be read either: still a complete shape, not an
# exception escaping into the panel.
class Broken2(Broken):
    pass
saved = m.SETTINGS_FILE
m.SETTINGS_FILE = "/nonexistent/dir/settings.json"
try:
    st2, _p, _a = Broken2()._collect_status()
    ok(st2.get("success") is False and isinstance(st2.get("settings"), dict),
       "an unreadable settings file still yields a complete status shape")
finally:
    m.SETTINGS_FILE = saved


section("one profile field, read the same way everywhere")

# Terse nmcli output is `field:value`, and a BSSID is nothing but colons, so
# the split has to be on the first one only. Only some fields arrive escaped,
# which is why unescaping is the caller's job and is tested here rather than
# reasoned about.
class Field(m.Plugin):
    def __init__(self, out):
        super().__init__()
        self.out = out
        self.asked = []
    def _run_cmd(self, cmd, timeout=5):
        self.asked.append(list(cmd))
        return {"success":True,"stdout":self.out,"stderr":"","returncode":0}

d = Field("802-11-wireless.bssid:AA\\:BB\\:CC\\:DD\\:EE\\:FF")
raw = d._read_profile_field("u", "802-11-wireless.bssid")
ok(raw == "AA\\:BB\\:CC\\:DD\\:EE\\:FF", "everything after the first colon is the value")
ok(raw.replace("\\", "") == "AA:BB:CC:DD:EE:FF",
   "and an escaped address unescapes to the address itself")
ok(d.asked == [["/usr/bin/nmcli","-t","-f","802-11-wireless.bssid",
                "con","show","uuid","u"]],
   "read with one nmcli call and no extra arguments")
ok(Field("ipv6.method:auto")._read_profile_field("u","ipv6.method") == "auto",
   "an unescaped field comes back as itself")
ok(Field("802-11-wireless.band:")._read_profile_field("u","802-11-wireless.band") == "",
   "a field that is set to nothing reads as empty")
ok(Field("")._read_profile_field("u","802-11-wireless.band") == "",
   "and so does a profile that answered nothing at all")
ok(Field("ipv6.method:  auto  ")._read_profile_field("u","ipv6.method") == "auto",
   "surrounding whitespace is not part of the value")

# The collector publishes the unescaped address, which is what the panel shows
# and what every comparison against a live BSSID is made against.
class EscapedPin(Connected):
    def _run_cmd(self, cmd, timeout=5):
        if "802-11-wireless.bssid" in " ".join(cmd) and "con" in cmd:
            return {"success":True,
                    "stdout":"802-11-wireless.bssid:02\\:00\\:00\\:00\\:00\\:11",
                    "stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout)

m._save_settings(dict(base_ap))
st, _p, _a = EscapedPin()._collect_status()
ok(st["live"].get("bssid_lock") == "02:00:00:00:00:11",
   "the collector reports the address unescaped, as the panel and the link do")


section("reading what iw says about the link")

P0 = m.Plugin
LINK = ("Connected to 02:00:00:00:00:11 (on wlan0)\n"
        "\tSSID: TestNet\n\tfreq: 5180\n\tsignal: -44 dBm\n"
        "\ttx bitrate: 866.7 MBit/s VHT-MCS 9 80MHz VHT-NSS 2")
live = P0._parse_link(LINK)
ok(live.get("connected_bssid") == "02:00:00:00:00:11", "the access point in use")
ok(live.get("frequency") == "5180", "the frequency")
ok(live.get("signal_dbm") == "-44 dBm", "the signal")
ok(live.get("tx_bitrate").startswith("866.7 MBit/s"), "the transmit rate")

# An absent field is absent, not empty: the panel can tell a missing reading
# from a reading of nothing, and every consumer of connected_bssid treats the
# empty string as "we are not associated".
ok("signal_dbm" not in P0._parse_link("Not connected."),
   "a link with no signal line reports no signal rather than an empty one")
ok(P0._parse_link("") == {}, "and nothing at all is read from no output")
ok("connected_bssid" not in P0._parse_link("Connected to"),
   "a truncated association line is not read as an address")

# Whichever line comes last wins, which is what a single-interface `iw link`
# gives and what the collector has always done.
ok(P0._parse_link("\tsignal: -70 dBm\n\tsignal: -44 dBm")["signal_dbm"] == "-44 dBm",
   "the last reading in the output is the current one")

section("reading the channel the way the panel shows it")

ok(P0._parse_channel("\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz")
   == "36 (80 MHz)", "channel and width are joined into one label")
ok(P0._parse_channel("\tchannel 36 (5180 MHz)") == "36",
   "a channel with no width is still the channel")
ok(P0._parse_channel("Interface wlan0\n\ttype managed") is None,
   "output with no channel line reports nothing rather than an empty channel")
ok(P0._parse_channel("") is None, "and so does no output")

# An unfamiliar format is worth showing rather than dropping: it tells whoever
# reads the panel what the radio actually said.
ok(P0._parse_channel("channel") == "channel",
   "a channel line that cannot be taken apart is passed through whole")
ok(P0._parse_channel("\tchannel 1 (2412 MHz), width: 20 MHz") == "1 (20 MHz)",
   "2.4 GHz reads the same way")


section("is the address pin ours to enforce on the network we are on")

# The pin follows whichever profile NetworkManager actually uses, so the
# profile it was written to is often not the one in use. Getting this wrong in
# one direction reports drift nothing can clear; in the other it pins the user
# to the first access point of a network they never asked to lock.
class Scoped(m.Plugin):
    def __init__(self, ssids):
        super().__init__()
        self.ssids = dict(ssids)
        self.reads = []
    def _get_profile_ssid(self, uuid, timeout=5):
        self.reads.append(uuid)
        return self.ssids.get(uuid)

HERE, TWIN, AWAY = "u-here", "u-twin", "u-away"
NETS = {HERE:"Home", TWIN:"Home", AWAY:"Cafe"}

def scope(locked, uuid, ssid, ssids=NETS):
    d = Scoped(ssids)
    return d._lock_is_for_this_network(
        {"bssid_lock_connection_uuid": locked}, uuid, ssid), d

held, d = scope(HERE, HERE, "Home")
ok(held is True, "the profile the lock was written to is the locked network")
ok(d.reads == [], "and answering that needs no further reads")

ok(scope(TWIN, HERE, "Home")[0] is True,
   "a second saved copy of the same network counts as the locked network")
ok(scope(AWAY, HERE, "Home")[0] is False,
   "a different network does not, so leaving home does not pin anyone")
ok(scope("", HERE, "Home")[0] is False,
   "a lock with no profile recorded is not enforced anywhere")

# "Could not tell" has to answer no: the cost of a wrong yes is a pin the user
# never asked for on a network they are merely passing through.
ok(scope(TWIN, HERE, None)[0] is False,
   "an unreadable active SSID holds off rather than guessing")
ok(scope(TWIN, HERE, "Home", {TWIN: None})[0] is False,
   "and so does an unreadable locked profile")
ok(scope(TWIN, HERE, "", {TWIN: ""})[0] is False,
   "two empty SSIDs are not a match")


section("no function grows back past what anyone will read")

# A ceiling, not a target. The three functions that used to sit above it -
# _collect_status at 450 lines, set_bssid_lock at 307, _apply_status_actions at
# 278 - got there a few lines at a time, with every single addition reasonable
# on its own. The limit is what makes the next few lines a decision rather than
# a drift.
FUNCTION_LINE_LIMIT = 200
_tree = ast.parse(open("main.py").read())

def _named_functions(node, prefix=""):
    for n in ast.iter_child_nodes(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield prefix + n.name, n.end_lineno - n.lineno + 1
            yield from _named_functions(n, prefix + n.name + ".")
        elif isinstance(n, ast.ClassDef):
            yield from _named_functions(n, prefix + n.name + ".")
        else:
            yield from _named_functions(n, prefix)

_sizes = dict(_named_functions(_tree))
_over = {n: L for n, L in _sizes.items() if L > FUNCTION_LINE_LIMIT}
ok(not _over, "no function in main.py is over "
   f"{FUNCTION_LINE_LIMIT} lines" +
   (f" (over: {', '.join(f'{n} at {L}' for n, L in sorted(_over.items(), key=lambda kv: -kv[1]))})"
    if _over else ""))

# The collector is the one that matters most: it decides every drift flag and
# every write the plugin proposes, and it is the function a poll runs.
ok(_sizes.get("Plugin._collect_status", 999) <= FUNCTION_LINE_LIMIT,
   "and the status collector in particular stays inside it")


section("setting the band preference")

# 196 lines, 38 branches and NO coverage before this section, which made it the
# largest untested function in the file - and it is a setter that writes a band
# into a NetworkManager profile, clears the address pin, cycles the radio and
# re-pins afterwards. A profile left demanding a band its network does not offer
# will not associate, so every failure path here can strand the device.
BU   = "eeeeeeee-0000-0000-0000-00000000000a"   # the profile in use
BOLD = "eeeeeeee-0000-0000-0000-00000000000b"   # one we wrote to previously
BX   = "eeeeeeee-0000-0000-0000-00000000000c"   # one that no longer exists

class Band(m.Plugin):
    """The band setter, over a profile whose writes can be made to fail."""
    def __init__(self, ssid="Home", active=BU, refuse=(), missing=(),
                 reconnect=True, link="Connected to 02:00:00:00:00:11\n\tfreq: 5180",
                 landed=None):
        super().__init__()
        self.ssid = ssid
        self.active = active
        self.refuse = set(refuse)          # (uuid, key) pairs that fail hard
        self.missing = set(missing)        # uuids nmcli says it does not know
        self.reconnect = reconnect
        self.link = link
        self.landed = landed               # who we came back on, if not `active`
        self.mods = []
        self.reconnects = 0
        self.depth_seen = []
    def _get_wifi_interface(self): return "wlan0"
    def _get_active_connection_uuid(self):
        if self.landed is not None and self.reconnects:
            return self.landed
        return self.active
    def _get_profile_ssid(self, uuid, timeout=5): return self.ssid or None
    def _hard_reconnect(self, uuid=None):
        self.reconnects += 1
        # the guard must be held across the reconnect, or a poll lands in the
        # middle of the change and writes the old address back
        self.depth_seen.append(getattr(self, "_profile_change_depth", 0))
        return self.reconnect
    # The network is visible on both bands unless a check says otherwise, so
    # the reachability check before the write passes.
    bands_in_range = (5180, 2462)
    def _run_cmd(self, cmd, timeout=5):
        if cmd[:2] == ["/usr/bin/iw", "dev"] and "link" in cmd:
            return {"success":True,"stdout":self.link,"stderr":"","returncode":0}
        if "wifi" in cmd and "list" in cmd:
            rows = "\n".join(f"{self.ssid}:{f} MHz" for f in self.bands_in_range)
            return {"success":True,"stdout":rows,"stderr":"","returncode":0}
        return {"success":True,"stdout":"","stderr":"","returncode":0}
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.mods.append((uuid, key, value))
        if uuid in self.missing:
            return {"success":False,"stdout":"",
                    "stderr":f"Error: unknown connection '{uuid}'.","returncode":10}
        if (uuid, key) in self.refuse:
            return {"success":False,"stdout":"","stderr":"permission denied","returncode":1}
        return {"success":True,"stdout":"","stderr":"","returncode":0}

def band_settings(**extra):
    s = {**base_ap, "last_connection_uuid": BU}
    s.update(extra)
    return s

def wrote(d, uuid, value):
    return (uuid, "802-11-wireless.band", value) in d.mods

# A blocking 3s wait sits on the re-lock path. Skipping it keeps this section
# quick; the suite already takes over two minutes.
_real_sleep = asyncio.sleep
async def _no_sleep(_s, *a, **k): return None

# --- refusing before anything is written ------------------------------------

# The band reaches a subprocess and a wrong one silently reduces the channels a
# device may use, so anything that is not a band it knows is refused outright.
for bad in ["ac", "", "5", "a; rm -rf /", "A", "bg2"]:
    m._save_settings(band_settings())
    d = Band()
    res = asyncio.run(d.set_band_preference(True, bad))
    ok(res.get("success") is False and not d.mods,
       f"band {bad!r} is refused and never reaches nmcli")
ok(m._load_settings().get("band_preference_enabled") is not True,
   "and a refused band is not recorded as enabled")

# Both bands it does know are accepted.
for good in ("a", "bg"):
    m._save_settings(band_settings())
    d = Band()
    res = asyncio.run(d.set_band_preference(True, good))
    ok(res.get("success") is True and wrote(d, BU, good), f"band {good!r} is applied")

# ⭐ The network is established BEFORE anything is written, so a failure here
# needs no rollback. Recording the preference as on with no network attached
# would leave it enforced nowhere while the toggle reads on, and would let
# Force Reapply write the band into an unrelated network.
m._save_settings(band_settings())
d = Band(ssid="")
res = asyncio.run(d.set_band_preference(True, "a"))
ok(res.get("success") is False, "an unreadable network refuses the enable")
ok(not d.mods, "and nothing at all was written first")
after = m._load_settings()
ok(after.get("band_preference_enabled") is not True
   and not after.get("band_preference_ssid"),
   "so no half-enabled state is left behind")

# Disabling needs no network - the band lives on a saved profile and being
# unable to associate is a likely reason to be turning it off.
m._save_settings(band_settings(band_preference_enabled=True, band_preference="a",
                               band_preference_ssid="Home",
                               band_preference_uuids=[BU]))
d = Band(ssid="")
res = asyncio.run(d.set_band_preference(False))
ok(res.get("success") is True and wrote(d, BU, ""),
   "the preference can be turned off without a readable network")

# --- what the enable records -------------------------------------------------

m._save_settings(band_settings())
d = Band()
asyncio.run(d.set_band_preference(True, "bg"))
s = m._load_settings()
ok(s.get("band_preference_enabled") is True and s.get("band_preference") == "bg",
   "the enable is recorded with the band asked for")
ok(s.get("band_preference_ssid") == "Home",
   "and the network it was set on is recorded, which is what scopes it")
ok(BU in s.get("band_preference_uuids", []),
   "and the profile is tracked, so it can be cleared later")

# ⭐ THE SUBTLE ONE: reapply_all calls this setter too, so re-claiming the
# network on every call would move a preference set at home onto whatever
# network Force Reapply happened to be pressed on.
m._save_settings(band_settings(band_preference_enabled=True, band_preference="a",
                               band_preference_ssid="Home",
                               band_preference_uuids=[BU]))
d = Band(ssid="Cafe")
asyncio.run(d.set_band_preference(True, "a"))
ok(m._load_settings().get("band_preference_ssid") == "Home",
   "an already-claimed network is not re-claimed on a later enable")

# ...while a fresh enable does claim whatever it is on.
m._save_settings(band_settings(band_preference_ssid=""))
d = Band(ssid="Cafe")
asyncio.run(d.set_band_preference(True, "a"))
ok(m._load_settings().get("band_preference_ssid") == "Cafe",
   "but a fresh enable claims the network it is on")

# The tracked list is bounded, like the lock's.
many = [f"eeeeeeee-0000-0000-0000-{i:012d}" for i in range(40)]
m._save_settings(band_settings(band_preference_uuids=list(many)))
d = Band()
asyncio.run(d.set_band_preference(True, "a"))
ok(len(m._load_settings().get("band_preference_uuids", [])) <= d._MAX_TRACKED_LOCK_UUIDS,
   "the tracked profile list stays bounded")

# --- turning it off reaches every profile ------------------------------------

m._save_settings(band_settings(band_preference_enabled=True, band_preference="a",
                               band_preference_ssid="Home",
                               band_preference_uuids=[BU, BOLD]))
d = Band()
asyncio.run(d.set_band_preference(False))
ok(wrote(d, BU, "") and wrote(d, BOLD, ""),
   "turning it off clears the band from every profile it was written to")
s = m._load_settings()
ok(s.get("band_preference_uuids") == [] and s.get("band_preference_ssid") == "",
   "and stops claiming a network once they are all clear")
ok(s.get("band_preference_enabled") is False, "and records the preference as off")

# A profile that no longer exists is resolved, not failed - keeping it would
# retry a write against nothing forever.
m._save_settings(band_settings(band_preference_enabled=True,
                               band_preference_uuids=[BU, BX]))
d = Band(missing=[BX])
asyncio.run(d.set_band_preference(False))
ok(m._load_settings().get("band_preference_uuids") == [],
   "a profile nmcli no longer knows is dropped from the list")

# Anything else is still demanding a band, so the record has to survive.
m._save_settings(band_settings(band_preference_enabled=True,
                               band_preference_uuids=[BU, BOLD]))
d = Band(refuse=[(BOLD, "802-11-wireless.band")])
asyncio.run(d.set_band_preference(False))
ok(m._load_settings().get("band_preference_uuids") == [BOLD],
   "a profile that refused the clear is kept, because it is still pinned to a band")

# --- the address pin, which is the path that can strand the device -----------

# The pin is released first so NM is free to find an access point on the new
# band; a profile demanding this band and an address on the other one is
# satisfiable by nothing.
m._save_settings(band_settings(bssid_lock_enabled=True,
                               bssid_lock_value="02:00:00:00:00:99",
                               bssid_lock_connection_uuid=BU))
d = Band()
asyncio.run(d.set_band_preference(True, "a"))
ok((BU, "802-11-wireless.bssid", "") in d.mods,
   "the address pin is released so NM can move to the new band")
ok(d.mods.index((BU, "802-11-wireless.band", "a"))
   < d.mods.index((BU, "802-11-wireless.bssid", "")),
   "the band is written before the pin is released, so no window has neither")

# ⭐ AND IF THE RELEASE FAILS, the band write is undone rather than left beside
# an address from the other band - the combination nothing can satisfy.
m._save_settings(band_settings(bssid_lock_enabled=True,
                               bssid_lock_value="02:00:00:00:00:99",
                               bssid_lock_connection_uuid=BU,
                               band_preference_enabled=False))
d = Band(refuse=[(BU, "802-11-wireless.bssid")])
res = asyncio.run(d.set_band_preference(True, "a"))
ok(res.get("success") is False, "a pin that will not release fails the band change")
ok(d.mods.count((BU, "802-11-wireless.band", "")) == 1,
   "and the band write is rolled back, not left demanding a band with a foreign pin")
ok(d.reconnects == 0, "and the radio is never cycled onto that state")

# --- coming back, and who we came back as ------------------------------------

asyncio.sleep = _no_sleep
try:
    # Re-pinned to whatever access point NM chose on the new band.
    m._save_settings(band_settings(bssid_lock_enabled=True,
                                   bssid_lock_connection_uuid=BU))
    d = Band(link="Connected to 02:00:00:00:00:42\n\tfreq: 5180")
    asyncio.run(d.set_band_preference(True, "a"))
    ok((BU, "802-11-wireless.bssid", "02:00:00:00:00:42") in d.mods,
       "the pin is restored to the access point NM landed on")
    ok(m._load_settings().get("bssid_lock_value") == "02:00:00:00:00:42",
       "and the recorded pin matches what was written")

    # ⭐ A reconnect can land on a DIFFERENT profile - a duplicate for the same
    # network, which is the situation this plugin exists to handle. Pinning what
    # we see onto the profile we meant to change would write a foreign address,
    # possibly from the other band, into a profile that now demands this one.
    m._save_settings(band_settings(bssid_lock_enabled=True,
                                   bssid_lock_connection_uuid=BU))
    d = Band(landed=BOLD, link="Connected to 02:00:00:00:00:77\n\tfreq: 5180")
    asyncio.run(d.set_band_preference(True, "a"))
    ok(not any(k == "802-11-wireless.bssid" and v for _u, k, v in d.mods),
       "nothing is re-pinned when the reconnect landed on another profile")

    # Nor when the radio came back on the band this network no longer wants.
    m._save_settings(band_settings(bssid_lock_enabled=True,
                                   bssid_lock_connection_uuid=BU))
    d = Band(link="Connected to 02:00:00:00:00:55\n\tfreq: 2437")
    asyncio.run(d.set_band_preference(True, "a"))
    ok(not any(k == "802-11-wireless.bssid" and v for _u, k, v in d.mods),
       "and nothing is re-pinned while the radio is on the wrong band for it")

    # An unreadable link is not fatal: reconciliation re-points the pin later.
    m._save_settings(band_settings(bssid_lock_enabled=True,
                                   bssid_lock_connection_uuid=BU))
    d = Band(link="")
    res = asyncio.run(d.set_band_preference(True, "a"))
    ok(res.get("success") is True,
       "a link that cannot be read still leaves the band change successful")

    # With no pin in play, none is written back.
    m._save_settings(band_settings(bssid_lock_enabled=False))
    d = Band()
    asyncio.run(d.set_band_preference(True, "a"))
    ok(not any(k == "802-11-wireless.bssid" for _u, k, _v in d.mods),
       "no address pin is touched when the lock is off")
finally:
    asyncio.sleep = _real_sleep

# --- failures the user has to be told about ----------------------------------

m._save_settings(band_settings())
d = Band(refuse=[(BU, "802-11-wireless.band")])
res = asyncio.run(d.set_band_preference(True, "a"))
ok(res.get("success") is False and res.get("error") == "nmcli_failed",
   "a band write that fails is reported")
ok(m._load_settings().get("band_preference_enabled") is not True,
   "and is not recorded as enabled")
ok(d.reconnects == 0, "and the radio is not cycled after it")

# A radio that does not come back is the one failure the user cannot diagnose
# from the panel, so it has its own message rather than a generic one.
m._save_settings(band_settings())
d = Band(reconnect=False)
res = asyncio.run(d.set_band_preference(True, "a"))
ok(res.get("success") is False and res.get("error") == "iw_failed",
   "a radio that does not come back is reported as such")

# --- the reconciliation guard ------------------------------------------------

# This setter clears the pin, cycles the radio and re-pins across an await, so
# a status poll can land in the middle. The guard is what stops reconciliation
# writing the old address back onto a profile that now demands the other band.
m._save_settings(band_settings())
d = Band()
asyncio.run(d.set_band_preference(True, "a"))
ok(d.depth_seen and all(n >= 1 for n in d.depth_seen),
   "the profile-change guard is held across the radio cycle")
ok(getattr(d, "_profile_change_depth", 0) == 0,
   "and released once the change is done")

# Released on the way out of a failure too, or every later poll is suppressed
# for the life of the process.
m._save_settings(band_settings())
d = Band()
d._resolve_uuid = lambda msg=None: (_ for _ in ()).throw(RuntimeError("boom"))
res = asyncio.run(d.set_band_preference(True, "a"))
ok(res.get("success") is False, "an unexpected failure is reported, not raised")
ok(getattr(d, "_profile_change_depth", 0) == 0,
   "and the guard is released even then, so later polls are not suppressed forever")


# The last four statements in this setter are a refusal and three error logs on
# nested-failure paths, where the log is the ONLY thing that tells anyone the
# profile may be left in a state that cannot associate. Silence there is the
# failure shape the comments around them were written about.

class NoProfile(Band):
    """Never connected: no active connection and nothing saved either."""
    def _get_active_connection_uuid(self): return None
    def _get_saved_connection_uuid(self): return None

m._save_settings(band_settings(last_connection_uuid="", bssid_lock_connection_uuid=""))
res = asyncio.run(NoProfile().set_band_preference(True, "a"))
ok(res.get("success") is False and res.get("error") == "no_wifi",
   "enabling with no WiFi at all asks the user to connect first")

m._save_settings(band_settings(last_connection_uuid="", bssid_lock_connection_uuid=""))
d = NoProfile()
res = asyncio.run(d.set_band_preference(False))
ok(res.get("success") is False and not d.mods,
   "and turning it off with no profile to turn it off on writes nothing")

class NthFails(Band):
    """Fails the Nth write of a given property, counting from 1.

    The same property is written more than once on these paths - the band is
    written and then rolled back, the pin released and then restored - so a
    blanket refusal aborts the run before it reaches the branch under test.
    """
    def __init__(self, nth, **kw):
        super().__init__(**kw)
        self.nth = dict(nth)
        self.seen = {}
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.mods.append((uuid, key, value))
        self.seen[key] = self.seen.get(key, 0) + 1
        if self.seen[key] in self.nth.get(key, ()):
            return {"success":False,"stdout":"","stderr":"permission denied",
                    "returncode":1}
        return {"success":True,"stdout":"","stderr":"","returncode":0}

asyncio.sleep = _no_sleep
try:
    # ⭐ THE WORST CASE: the pin will not release, so the band write is rolled
    # back - and the rollback fails too. The profile is then left demanding a
    # band with an address from possibly the other band beside it, which is the
    # combination nothing satisfies. Nothing here can fix it; the log line is
    # the only record that it happened.
    m._save_settings(band_settings(bssid_lock_enabled=True,
                                   bssid_lock_connection_uuid=BU,
                                   band_preference_enabled=True,
                                   band_preference="bg"))
    # band: the first write succeeds, the ROLLBACK (second) fails.
    # bssid: the release (first) fails, which is what triggers the rollback.
    d = NthFails({"802-11-wireless.band": (2,), "802-11-wireless.bssid": (1,)})
    res = asyncio.run(d.set_band_preference(True, "a"))
    ok(res.get("success") is False,
       "a pin release that fails and a rollback that also fails is still reported")
    ok(d.mods.count((BU, "802-11-wireless.band", "bg")) == 1,
       "the rollback was attempted, back to the band that was in force")
    ok(d.reconnects == 0,
       "and the radio is not cycled onto a profile that may not associate")

    # The link cannot be read at all when restoring the pin. Not fatal - a later
    # poll re-points it - but it must not pass silently, or the lock reads as
    # enabled in settings while being absent from the profile.
    class DeadLink(Band):
        def _run_cmd(self, cmd, timeout=5):
            if cmd[:2] == ["/usr/bin/iw", "dev"] and "link" in cmd:
                return {"success":False,"stdout":"","stderr":"No such device",
                        "returncode":237}
            return super()._run_cmd(cmd, timeout)
    m._save_settings(band_settings(bssid_lock_enabled=True,
                                   bssid_lock_connection_uuid=BU))
    d = DeadLink()
    res = asyncio.run(d.set_band_preference(True, "a"))
    ok(res.get("success") is True,
       "a link that cannot be read leaves the band change successful")
    ok(not any(k == "802-11-wireless.bssid" and v for _u, k, v in d.mods),
       "and no address is invented to pin when the link could not be read")

    # The re-lock write itself fails: the release (first bssid write) succeeds,
    # the restore (second) does not. Recording a pin that was not applied would
    # report it as present forever, so the recorded value must not move.
    m._save_settings(band_settings(bssid_lock_enabled=True,
                                   bssid_lock_connection_uuid=BU,
                                   bssid_lock_value="02:00:00:00:00:99"))
    d = NthFails({"802-11-wireless.bssid": (2,)},
                 link="Connected to 02:00:00:00:00:42\n\tfreq: 5180")
    res = asyncio.run(d.set_band_preference(True, "a"))
    ok((BU, "802-11-wireless.bssid", "02:00:00:00:00:42") in d.mods,
       "the re-lock is attempted after the reconnect")
    ok(m._load_settings().get("bssid_lock_value") != "02:00:00:00:00:42",
       "but a re-lock that failed is not recorded as the pin in force")
finally:
    asyncio.sleep = _real_sleep


section("buffer tuning: putting back what the system had")

class Sysctl(m.Plugin):
    """sysctl with a live table, so a restore can be read back."""
    def __init__(self, live, cake=False):
        super().__init__(); self.live = dict(live); self.cmds = []; self.cake = cake
    def _get_wifi_interface(self): return "wlan0"
    def _run_cmd(self, cmd, timeout=5):
        self.cmds.append(cmd)
        if cmd[:2] == ["/usr/bin/sysctl", "-n"]:
            return _ok(self.live.get(cmd[2], ""))
        if cmd[:2] == ["/usr/bin/sysctl", "-w"]:
            k, v = cmd[2].split("=", 1); self.live[k] = v
            return _ok()
        if cmd[:3] == ["/usr/bin/tc", "qdisc", "show"]:
            return _ok("qdisc cake 8001: root" if self.cake else "qdisc noqueue 0: root")
        return _ok()

STOCK_618 = {**m.SYSCTL_DEFAULTS, "net.core.rmem_max": "4194304",
             "net.core.wmem_max": "4194304"}
_real_release = getattr(m, "_kernel_release", None)
try:
    m._kernel_release = lambda: "6.18.50-valve2-1-neptune-618"
    ok(m._stock_sysctl_defaults("6.18.50")["net.core.rmem_max"] == "4194304",
       "kernel 6.18 and later default rmem_max to 4 MiB")
    ok(m._stock_sysctl_defaults("6.17.9")["net.core.rmem_max"] == "212992",
       "older kernels keep 212992")
    ok(m._stock_sysctl_defaults("7.0.1")["net.core.wmem_max"] == "4194304",
       "a major version past 6 counts as newer")

    m._save_settings(dict(m.DEFAULT_SETTINGS))
    k = Sysctl({**STOCK_618, "net.core.netdev_max_backlog": "4096"})
    asyncio.run(k.set_buffer_tuning(True))
    ok(k.live["net.core.rmem_max"] == m.TUNED_RMEM_MAX, "turning tuning on applies it")
    asyncio.run(k.set_buffer_tuning(False))
    ok(k.live["net.core.rmem_max"] == "4194304",
       "turning it off puts back 4 MiB on 6.18, not the older 212992")
    ok(k.live["net.core.netdev_max_backlog"] == "4096",
       "and a distro's own value comes back rather than the kernel default")

    # Tuning already on when this version arrives: the live values are ours,
    # so nothing is recorded and the kernel's default is used.
    m._save_settings({**m.DEFAULT_SETTINGS, "buffer_tuning_enabled": True})
    k = Sysctl(m.SYSCTL_PARAMS)
    asyncio.run(k.set_buffer_tuning(True))
    ok(m._load_settings().get("sysctl_baseline") == {},
       "values we set ourselves are never recorded as the system's")
    asyncio.run(k.set_buffer_tuning(False))
    ok(k.live == STOCK_618, "so turning it off falls back to this kernel's defaults")

    # A baseline from another kernel is stale after an OS update.
    m._save_settings({**m.DEFAULT_SETTINGS, "buffer_tuning_enabled": True,
                      "sysctl_baseline": {"kernel": "6.16.1", "values":
                                          {"net.core.rmem_max": "212992"}}})
    k = Sysctl(m.SYSCTL_PARAMS)
    asyncio.run(k.set_buffer_tuning(False))
    ok(k.live["net.core.rmem_max"] == "4194304",
       "a baseline recorded on another kernel is not put back")

    # The baseline reaches sysctl -w as root from a user-editable file.
    m._save_settings({**m.DEFAULT_SETTINGS, "sysctl_baseline": {
        "kernel": "6.18.50-valve2-1-neptune-618",
        "values": {"net.core.rmem_max": "1; reboot", "kernel.panic": "1",
                   "net.core.wmem_max": "\u00b2", "net.core.rmem_default": "300000"}}})
    ok(m._load_settings().get("sysctl_baseline").get("values") == {"net.core.rmem_default": "300000"},
       "only known keys with plain numbers survive a load")

    # Reset and uninstall leave the system alone when tuning was never on.
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    k = Sysctl(STOCK_618)
    asyncio.run(k.reset_settings())
    ok(not any(c[:2] == ["/usr/bin/sysctl", "-w"] for c in k.cmds),
       "reset writes no sysctl when tuning was never applied")
    ok(not any(c[:3] == ["/usr/bin/tc", "qdisc", "del"] for c in k.cmds),
       "and deletes no queueing discipline that is not ours")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    k = Sysctl(m.SYSCTL_PARAMS, cake=True)
    asyncio.run(k._uninstall())
    ok(k.live == STOCK_618, "uninstall restores tuning that is live even with the setting off")
    ok(any(c[:3] == ["/usr/bin/tc", "qdisc", "del"] for c in k.cmds),
       "and removes CAKE that is still attached")

    # A partial apply is said out loud.
    class Partial(Sysctl):
        def _run_cmd(self, cmd, timeout=5):
            if cmd[:2] == ["/usr/bin/sysctl", "-w"] and "netdev_budget=" in cmd[2]:
                return {"success": False, "stdout": "", "stderr": "nope", "returncode": 1}
            return super()._run_cmd(cmd, timeout)
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    r = asyncio.run(Partial(STOCK_618).set_buffer_tuning(True))
    ok(r.get("success") is True and "netdev_budget" in (r.get("message") or ""),
       "a partly applied tuning names what did not take")
except Exception as e:
    ok(False, f"buffer tuning checks raised {e!r}")
finally:
    m._kernel_release = _real_release


section("PCIe power states: each one written and read back")

def pcie_device(files, power="auto", odd=()):
    """A fake WiFi PCI device. Names in `odd` are directories, which no
    write can succeed on and which raise something other than the two
    errors the old loop caught."""
    root = tempfile.mkdtemp()
    dev = os.path.join(root, "dev"); os.makedirs(os.path.join(dev, "link"))
    os.makedirs(os.path.join(dev, "power"))
    for name in files:
        path = os.path.join(dev, "link", name)
        if name in odd: os.makedirs(path)
        else: open(path, "w").write("1")
    open(os.path.join(dev, "power", "control"), "w").write(power)
    net = os.path.join(root, "net"); os.makedirs(os.path.join(net, "wlan0"))
    os.symlink(dev, os.path.join(net, "wlan0", "device"))
    return net, dev

class Pcie(m.Plugin):
    def _get_wifi_interface(self): return "wlan0"

_real_sysfs = m.SYSFS_NET
try:
    OLED = ["l1_aspm", "l1_1_aspm", "l1_2_aspm", "l1_1_pcipm", "l1_2_pcipm"]
    m.SYSFS_NET, dev = pcie_device(OLED)
    ok(Pcie()._apply_pcie_aspm_fix(True) == [],
       "every state the device has is disabled, and a missing one is not a failure")
    ok(all(open(os.path.join(dev, "link", f)).read() == "0" for f in OLED)
       and open(os.path.join(dev, "power", "control")).read() == "on",
       "and each was actually written")

    m.SYSFS_NET, dev = pcie_device(OLED, odd=("l1_aspm",))
    missed = Pcie()._apply_pcie_aspm_fix(True)
    ok(missed == ["l1_aspm"], "a state that would not take is named")
    ok(open(os.path.join(dev, "link", "l1_2_pcipm")).read() == "0"
       and open(os.path.join(dev, "power", "control")).read() == "on",
       "and an odd error on one file no longer stops the rest")
    ok(Pcie()._apply_pcie_aspm_fix(False) == [],
       "restoring is not judged by what firmware chooses to keep off")
finally:
    m.SYSFS_NET = _real_sysfs


section("IPv6: only on the networks it was turned off for")

A6 = "aaaaaaaa-0000-0000-0000-0000000000a6"
B6 = "bbbbbbbb-0000-0000-0000-0000000000b6"
def classify(settings, uuid, live):
    st = {"drift": {}, "external": {}, "live": {}}; acts = []
    m.Plugin()._classify_ipv6(settings, uuid, live, st, acts)
    return st, acts

st, acts = classify({"ipv6_disabled": True, "ipv6_uuids": [A6]}, B6, "auto")
ok(acts == [] and not st["drift"], "another network is not written to by a status poll")
ok(st["live"].get("ipv6_other_network") is True, "and the panel is told it belongs elsewhere")
st, acts = classify({"ipv6_disabled": True, "ipv6_uuids": [A6]}, A6, "auto")
ok(st["drift"].get("ipv6") and acts and acts[0]["kind"] == "ipv6",
   "the network it was set on is still held")
st, acts = classify({"ipv6_disabled": True, "ipv6_uuids": []}, A6, "disabled")
ok(acts == [{"kind": "ipv6_adopt", "uuid": A6}],
   "a setting from before tracking adopts the profile where it already holds")
st, acts = classify({"ipv6_disabled": True, "ipv6_uuids": []}, B6, "auto")
ok(acts == [] and st["live"].get("ipv6_other_network"),
   "and does not spread to a profile where it does not")

section("custom DNS: every network it was set on is undone")

class Dns(m.Plugin):
    def __init__(self, active, missing=()):
        super().__init__(); self.active = active; self.mods = []; self.missing = set(missing)
    def _resolve_uuid(self, msg=None): return self.active, None
    def _read_profile_field(self, uuid, field, timeout=5): return "192.168.1.1"
    def _hard_reconnect(self, uuid=None): return True
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.mods.append((uuid, key, value))
        if uuid in self.missing:
            return {"success": False, "stdout": "", "returncode": 10,
                    "stderr": f"Error: unknown connection '{uuid}'."}
        return _ok()

m._save_settings(dict(m.DEFAULT_SETTINGS))
d = Dns(A6)
asyncio.run(d.set_dns(True, "cloudflare", ""))
ok(m._load_settings().get("dns_uuids") == [A6], "where DNS is set is recorded")
d = Dns(B6)
asyncio.run(d.set_dns(False))
ok((A6, "ipv4.ignore-auto-dns", "no") in d.mods and (A6, "ipv4.dns", "") in d.mods,
   "turning it off on another network still gives the first one its own DNS back")
ok(m._load_settings().get("dns_uuids") == [], "and nothing is left tracked")

m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": True, "dns_uuids": [A6, B6]})
d = Dns(A6, missing=(B6,))
asyncio.run(d.set_dns(False))
ok(m._load_settings().get("dns_uuids") == [], "a profile that no longer exists is let go")

m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": True, "dns_uuids": [A6]})
d = Dns(B6)
d._clear_profile_pins()
ok((A6, "ipv4.dns", "") in d.mods, "reset and uninstall clear DNS too")

class HalfDns(Dns):
    def _nmcli_modify(self, uuid, key, value, timeout=5):
        self.mods.append((uuid, key, value))
        if key == "ipv4.ignore-auto-dns":
            return {"success": False, "stdout": "", "stderr": "no", "returncode": 1}
        return _ok()
m._save_settings(dict(m.DEFAULT_SETTINGS))
d = HalfDns(A6)
r = asyncio.run(d.set_dns(True, "google", ""))
ok(r.get("success") is False and d.mods[-1] == (A6, "ipv4.dns", "192.168.1.1"),
   "a half-applied DNS change puts the profile's servers back")

section("force 5 GHz: refused where it would disconnect")
m._save_settings(band_settings())
b = Band(); b.bands_in_range = (2462,)
r = asyncio.run(b.set_band_preference(True, "a"))
ok(r.get("success") is False and r.get("error") == "band_unavailable",
   "no 5 GHz access point in range means no write and no reconnect")
ok(not b.mods and b.reconnects == 0, "and nothing was touched")

class SlowScan(Band):
    def __init__(self):
        super().__init__(); self.timeouts = []
    def _run_cmd(self, cmd, timeout=5):
        if "wifi" in cmd and "list" in cmd:
            self.timeouts.append(timeout)
            return {"success":False,"stdout":"","stderr":"Command timed out","returncode":-1}
        return super()._run_cmd(cmd, timeout)
m._save_settings(band_settings())
b = SlowScan()
r = asyncio.run(b.set_band_preference(True, "a"))
ok(r.get("error") != "band_unavailable",
   "a scan that could not be read is not taken as nothing in range")
ok(b.timeouts and min(b.timeouts) >= 15,
   "and the scan gets the same budget as the access point list (a cold scan is slow)")

section("Fix now: repairs only what drifted")

class Drifted(m.Plugin):
    def __init__(self, drift):
        super().__init__(); self.drift = drift; self.calls = []
    async def get_status(self): return {"success": True, "drift": dict(self.drift)}
    async def _call(self, name):
        self.calls.append(name); return {"success": True}
    async def set_power_save(self, v): return await self._call("power_save")
    async def set_buffer_tuning(self, v): return await self._call("buffer_tuning")
    async def set_cake(self, v): return await self._call("cake")
    async def set_bssid_lock(self, v): return await self._call("bssid_lock")
    async def set_band_preference(self, v, b="a"): return await self._call("band")
    async def set_ipv6(self, v): return await self._call("ipv6")
    async def set_auto_fix(self, v): return await self._call("auto_fix")

m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                  "cake_enabled": True, "bssid_lock_enabled": False})
if not hasattr(m.Plugin, "repair_drifted"):
    ok(False, "there is a repair that touches only what drifted")
else:
    d = Drifted({"cake": True, "power_save": True})
    r = asyncio.run(d.repair_drifted())
    ok(d.calls == ["cake", "power_save"], "only the drifted settings are re-run")
    ok("bssid_lock" not in d.calls and "auto_fix" not in d.calls,
       "and nothing the user left off is turned on")
    ok(r.get("total") == 2 and r.get("applied") == 2, "with per-setting counts")
    d = Drifted({})
    r = asyncio.run(d.repair_drifted())
    ok(r.get("total") == 0 and r.get("message"), "no drift reports that rather than all applied")

section("backend switch: one at a time, and never during an update")

class Switch(m.Plugin):
    def _has_backend_tool(self): return True
    def _get_current_backend(self): return "iwd"
    def _get_backend_method(self): return "steamos"
    async def _backend_switch_worker(self, target): await asyncio.sleep(0)

async def two_at_once():
    sw = Switch()
    return await asyncio.gather(sw.start_backend_switch("wpa_supplicant"),
                                sw.start_backend_switch("wpa_supplicant")), sw
(first, second), sw = asyncio.run(two_at_once())
ok([first.get("accepted"), second.get("accepted")].count(True) == 1,
   "two overlapping requests start one switch")

sw = Switch()
r = asyncio.run(sw.start_backend_switch("iwd"))
ok(r.get("reason") == "already_set" and sw._backend_switch["in_progress"] is False,
   "a refused request gives the reservation back")

sw = Switch(); sw._update_dir = tempfile.mkdtemp()
r = asyncio.run(sw.start_backend_switch("wpa_supplicant"))
ok(r.get("accepted") is False and r.get("reason") == "update_in_progress",
   "no switch while an update is being installed")

class Updating(m.Plugin):
    async def check_for_update(self):
        raise AssertionError("should not get this far")
u = Updating(); u._ensure_backend_switch_state(); u._backend_switch["in_progress"] = True
r = asyncio.run(u.apply_update())
ok(r.get("success") is False, "and no update while a switch is running")

class TwoUpdates(m.Plugin):
    def __init__(self):
        super().__init__(); self.checks = 0
    async def check_for_update(self):
        self.checks += 1
        await asyncio.sleep(0)
        return {"update_available": False}
async def two_updates():
    u = TwoUpdates()
    return await asyncio.gather(u.apply_update(), u.apply_update()), u
(r1, r2), u = asyncio.run(two_updates())
ok(u.checks == 1, "two overlapping update requests run one update")
class HeldUpdate(TwoUpdates):
    async def check_for_update(self):
        self.checks += 1
        await asyncio.sleep(0.05)
        return {"update_available": False}
async def refused_mid_check():
    u = HeldUpdate()
    first = asyncio.ensure_future(u.apply_update())
    await asyncio.sleep(0.01)
    await u.apply_update()
    held = u._update_running()
    await first
    return held
ok(asyncio.run(refused_mid_check()) is True,
   "a refused request does not release the claim of the one still running")
ok(u._update_running() is False, "and the claim is released when it ends")

class GenericStartFails(m.Plugin):
    def __init__(self):
        super().__init__(); self.cmds = []
        self._ensure_backend_switch_state()
    def _run_cmd(self, cmd, timeout=5):
        self.cmds.append(cmd)
        if cmd[:3] == ["/usr/bin/systemctl", "start", "iwd"]:
            return {"success": False, "stdout": "", "stderr": "failed", "returncode": 1}
        return _ok()
_real_conf = m.GENERIC_BACKEND_CONF
try:
    m.GENERIC_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "conf.d", "99-backend.conf")
    g = GenericStartFails()
    asyncio.run(g._generic_backend_switch_worker("iwd"))
    res = g._backend_switch["result"]
    ok(res and res["success"] is False, "a backend that will not start fails the switch")
    ok(["/usr/bin/systemctl", "start", "wpa_supplicant"] in g.cmds
       and "wifi.backend=wpa_supplicant" in open(m.GENERIC_BACKEND_CONF).read(),
       "and the previous backend is put back")
finally:
    m.GENERIC_BACKEND_CONF = _real_conf

section("auto-fix and CAKE: off only once actually off")

class StuckDispatcher(m.Plugin):
    def _remove_dispatcher(self): return False
m._save_settings({**m.DEFAULT_SETTINGS, "auto_fix_on_wake": True})
r = asyncio.run(StuckDispatcher().set_auto_fix(False))
ok(r.get("success") is False and m._load_settings()["auto_fix_on_wake"] is True,
   "a script that could not be removed keeps the toggle on")

class StuckCake(m.Plugin):
    def _get_wifi_interface(self): return "wlan0"
    def _run_cmd(self, cmd, timeout=5):
        if cmd[:3] == ["/usr/bin/tc", "qdisc", "del"]:
            return {"success": False, "stdout": "", "stderr": "busy", "returncode": 2}
        if cmd[:3] == ["/usr/bin/tc", "qdisc", "show"]:
            return _ok("qdisc cake 8001: root")
        return _ok()
m._save_settings({**m.DEFAULT_SETTINGS, "cake_enabled": True})
r = asyncio.run(StuckCake().set_cake(False))
ok(r.get("success") is False and m._load_settings()["cake_enabled"] is True,
   "CAKE that is still attached is not reported as removed")

section("update script: staged, versioned, one at a time")
_src = open("main.py").read()
_script = m.UPDATE_SCRIPT
ok("flock -n" in _script, "a second update exits instead of running alongside")
ok("--max-time" in _script and "--fail" in _script, "the download is bounded and fails on HTTP errors")
ok(_script.index("wifiopt-new\" ||") < _script.index('mv -f "$PLUGIN_DIR'),
   "everything is copied beside the live files before anything is replaced")
ok(_script.rindex("package.json") > _script.index('FILES="') and
   _script[_script.index('FILES="'):].split("\n")[0].rstrip('"').endswith("package.json"),
   "package.json, which carries the version, is moved into place last")
ok('"$GOT" != "$EXPECTED"' in _script, "a payload of the wrong version is refused")
ok("if systemctl restart plugin_loader" in _script, "a failed restart is not reported as success")

section("update: reload only the plugin, restart Decky only as the fallback")
try:
    _last_mv = _script.rindex('mv -f "$PLUGIN_DIR/$f.wifiopt-new"')
    ok(_last_mv < _script.index("set_state swapped") < _script.index("systemctl restart plugin_loader"),
       "the panel is told to reload only after every file is in place, and the restart comes after that")
    _loop = _script[_script.index("set_state swapped"):_script.index("set_state restarting")]
    ok('"loaded $EXPECTED"' in _loop and "seq 1 45" in _loop and "exit 0" in _loop,
       "the script waits for the new version to answer, and stops there when it does")
    ok("set_state failed" in _script[_script.index("cleanup() {"):_script.index("trap cleanup EXIT")],
       "an update that stops early reports failed")
    import subprocess as _sp
    _sh = os.path.join(tempfile.mkdtemp(), "update.sh"); open(_sh, "w").write(_script)
    _syn = _sp.run(["/bin/bash", "-n", _sh], capture_output=True, text=True)
    ok(_syn.returncode == 0, f"the update script parses ({_syn.stderr.strip()[:120]})")
    ok("trap 'exit 1' TERM INT HUP" in _script, "and so does one that is killed, through the exit trap")
    ok('[ "$OWN_LOCK" = 1 ] || return 0' in _script,
       "a second copy refused the lock writes no progress over the running one's")
    ok(_script.index("set_state downloading") > _script.index("OWN_LOCK=1"),
       "progress is only written once the lock is held")
    ok("printf '%s %s\\n' \"$1\" \"$EXPECTED\" > \"$STATE.tmp\"" in _script
       and 'mv -f "$STATE.tmp" "$STATE"' in _script,
       "each state is one line, written whole and moved into place")

    def _state(text, age=0):
        if os.path.lexists(m.UPDATE_STATE_FILE): os.remove(m.UPDATE_STATE_FILE)
        if text is not None:
            open(m.UPDATE_STATE_FILE, "w").write(text)
            t = time.time() - age; os.utime(m.UPDATE_STATE_FILE, (t, t))
    _state("swapped 0.19.0-beta\n")
    ph, ver, _ = m._read_update_state()
    ok((ph, ver) == ("swapped", "0.19.0-beta"), "a state the script writes is read back")
    for bad in ("rebooting 0.19.0", "swapped 0.19;rm", "swapped", "swapped 1.0 extra", ""):
        _state(bad)
        ok(m._read_update_state()[0] == "", f"a state it would not write is ignored: {bad!r}")
    _state(None); _victim = os.path.join(tempfile.mkdtemp(), "v"); open(_victim, "w").write("swapped 1.0")
    os.symlink(_victim, m.UPDATE_STATE_FILE)
    ok(m._read_update_state()[0] == "", "the state is not read through a link")
    os.remove(m.UPDATE_STATE_FILE); os.mkfifo(m.UPDATE_STATE_FILE)
    ok(m._read_update_state()[0] == "", "nor from a FIFO")
    os.remove(m.UPDATE_STATE_FILE)

    _state("downloading 0.19.0-beta\n")
    ok(m.Plugin()._update_running() is True,
       "a backend that did not start the script still sees it running, as after a reload")
    _state("downloading 0.19.0-beta\n", age=m.UPDATE_STATE_MAX_AGE + 5)
    ok(m.Plugin()._update_running() is False, "but not one older than the script's own time limit")
    ok(asyncio.run(m.Plugin().get_update_progress())["phase"] == "failed",
       "which the panel is told failed")
    for done in ("done 0.19.0-beta", "failed 0.19.0-beta"):
        _state(done)
        ok(m.Plugin()._update_running() is False, f"a finished update is not running: {done}")

    _real_ver = m.decky.DECKY_PLUGIN_VERSION
    try:
        m.decky.DECKY_PLUGIN_VERSION = "0.19.0-beta"
        _state("swapped 0.19.0-beta\n"); m.Plugin()._acknowledge_update()
        ok(m._read_update_state()[:2] == ("loaded", "0.19.0-beta"),
           "the new backend tells the waiting script it is the version it installed")
        _state("swapped 0.19.1-beta\n"); m.Plugin()._acknowledge_update()
        ok(m._read_update_state()[0] == "swapped", "the old one, still running, does not")
        _state("restarting 0.19.0-beta\n"); m.Plugin()._acknowledge_update()
        ok(m._read_update_state()[0] == "restarting", "and nothing is acknowledged once the script gave up waiting")
    finally:
        m.decky.DECKY_PLUGIN_VERSION = _real_ver
    _main_src = _src[_src.index("    async def _main(self):"):]
    ok(_main_src.index("self._acknowledge_update()") < _main_src.index("self._rotate_logs()"),
       "startup acknowledges the update before anything slow")

    class Launch(m.Plugin):
        def __init__(self, rc):
            super().__init__(); self.rc = rc; self.cmds = []
        def _run_cmd(self, cmd, timeout=5):
            self.cmds.append(cmd)
            return {"success": self.rc == 0, "stdout": "", "stderr": "nope", "returncode": self.rc}
    _real_sr, _real_popen = m.SYSTEMD_RUN, m.subprocess.Popen
    _popened = []
    try:
        m.SYSTEMD_RUN = os.path.join(tempfile.mkdtemp(), "systemd-run"); open(m.SYSTEMD_RUN, "w").close()
        m.subprocess.Popen = lambda argv, **k: _popened.append((argv, k))
        lu = Launch(0); lu._launch_updater(["/bin/bash", "/x/update.sh", "a"])
        c = lu.cmds[0] if lu.cmds else []
        ok(c[:1] == [m.SYSTEMD_RUN] and "--collect" in c and c[c.index("--") + 1:] == ["/bin/bash", "/x/update.sh", "a"]
           and not _popened, "the update script runs in a systemd unit of its own, outside plugin_loader's")
        ok(any(a.startswith("--property=RuntimeMaxSec=") for a in c), "with a time limit")
        lu = Launch(1); lu._launch_updater(["/bin/bash", "/x/update.sh", "a"])
        ok(_popened and _popened[-1][0] == ["/bin/bash", "/x/update.sh", "a"]
           and _popened[-1][1].get("start_new_session") is True,
           "and starts directly when systemd-run refuses")
        os.remove(m.SYSTEMD_RUN); _popened.clear()
        lu = Launch(0); lu._launch_updater(["/bin/bash", "/x/update.sh"])
        ok(not lu.cmds and _popened, "or is not there at all")

        class Starts(Launch):
            async def check_for_update(self):
                return {"update_available": True, "channel": "beta", "latest_version": "0.19.0-beta"}
        _state("failed 0.19.0-beta\n")
        st = Starts(0); r = asyncio.run(st.apply_update())
        ok(r.get("success") is True and r.get("version") == "0.19.0-beta",
           "a started update tells the panel which version to follow")
        ok(not os.path.lexists(m.UPDATE_STATE_FILE),
           "and an earlier update's outcome is cleared, so it is not read as this one's")
        if st._update_dir and os.path.isdir(st._update_dir):
            import shutil; shutil.rmtree(st._update_dir)
    finally:
        m.SYSTEMD_RUN, m.subprocess.Popen = _real_sr, _real_popen

    _watch = open("src/updateWatch.ts").read()
    _name = json.load(open("plugin.json"))["name"]
    ok(f'PLUGIN_NAME = "{_name}"' in _watch and '"loader/reload_plugin", PLUGIN_NAME' in _watch,
       "the panel asks Decky to reload the plugin by the name in plugin.json")
    ok("typeof deckyBackend?.call !== \"function\"" in _watch,
       "and copes with a Decky that has no such call, leaving the restart to the script")
    _idx = open("src/index.tsx").read()
    ok("disposeUpdateWatch()" in _idx[_idx.index("onDismount()"):],
       "the watch stops when Decky dismounts the plugin, which a reload can do twice")
    ok("watchUpdate(version)" in _idx, "the panel follows an update it started")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("files the desktop user owns: read and written without trusting them")
try:

    _real_settings = m.SETTINGS_FILE
    try:
        # A FIFO planted at the settings path used to hang the plugin's first read.
        _d = tempfile.mkdtemp(); m.SETTINGS_FILE = os.path.join(_d, "settings.json")
        os.mkfifo(m.SETTINGS_FILE)
        ok(m._load_settings() == m.DEFAULT_SETTINGS,
           "a FIFO in place of the settings file is refused, not waited on")
        os.remove(m.SETTINGS_FILE)
        # The saved file keeps its 0644 whatever mkstemp made it.
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        ok(os.stat(m.SETTINGS_FILE).st_mode & 0o777 == 0o644, "settings are saved readable, 0644")
        # Values that end up written as root are filtered on the way in.
        m._save_settings({**m.DEFAULT_SETTINGS, "pins_owned": ["802-11-wireless.band", "ipv4.dns"],
                          "valve_powersave_before": "2\n[device]\nwifi.backend=iwd"})
        s_ = m._load_settings()
        ok(s_["pins_owned"] == ["802-11-wireless.band"], "only the two pin kinds can be recorded as ours")
        ok(s_["valve_powersave_before"] == "?",
           "a power save value that is not one of NetworkManager's is discarded")
        for v in ("0", "1", "2", "3", "", "?"):
            m._save_settings({**m.DEFAULT_SETTINGS, "valve_powersave_before": v})
            ok(m._load_settings()["valve_powersave_before"] == v, f"power save record {v!r} survives a load")
        # DNS saved off as custom-with-nothing by older versions turns on cleanly.
        m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": False, "dns_provider": "custom",
                          "dns_servers": ""})
        ok(m._load_settings()["dns_provider"] == "cloudflare",
           "an empty custom DNS from an older version loads as a provider that can be turned on")
    finally:
        m.SETTINGS_FILE = _real_settings

    _d = tempfile.mkdtemp()
    _p = os.path.join(_d, "n"); open(_p, "w").write("1234\n")
    ok(m._read_small_int(_p) == 1234, "the enforcement time is read from a plain file")
    _l = os.path.join(_d, "link"); os.symlink(_p, _l)
    ok(m._read_small_int(_l) == 0, "but not through a link")
    _f = os.path.join(_d, "fifo"); os.mkfifo(_f)
    ok(m._read_small_int(_f) == 0, "and not from a FIFO, which would block")
    ok('ENFORCED_FILE = "/run/wifi-optimizer/last_enforced"' in open("main.py").read(),
       "the enforcement time lives in /run, which only root writes")
    _tmpl = open("defaults/dispatcher.sh.tmpl").read()
    ok("/run/wifi-optimizer/last_enforced" in _tmpl and "SETTINGS_DIR/last_enforced" not in _tmpl,
       "and the dispatcher writes it there too")

    class LinkedLogs(m.Plugin):
        pass
    _logs = tempfile.mkdtemp(); _victim = tempfile.mkdtemp()
    for i in range(15):
        open(os.path.join(_victim, f"{i}.log"), "w").write("x")
    os.rmdir(_logs); os.symlink(_victim, _logs)
    _saved_dir = getattr(m.decky, "DECKY_PLUGIN_LOG_DIR", None)
    try:
        m.decky.DECKY_PLUGIN_LOG_DIR = _logs
        LinkedLogs()._rotate_logs()
        ok(len(os.listdir(_victim)) == 15, "log rotation does not follow a linked log directory")
    finally:
        m.decky.DECKY_PLUGIN_LOG_DIR = _saved_dir

    # A recorded sysctl baseline is the user's file: written back as root, a
    # backlog of 1 or a buffer of 0 would break networking until a reboot.
    _rel = m._kernel_release()
    _stock = m._stock_sysctl_defaults(_rel)
    _k = "net.core.netdev_max_backlog"
    _src_all = open("main.py").read()
    _vals = m.Plugin()._sysctl_restore_values(
        {"sysctl_baseline": {"kernel": _rel, "values": {_k: "1"}}})
    ok(_vals[_k] == _stock[_k],
       "a recorded sysctl baseline far below the default is not written back")
    _ok_val = str(int(_stock[_k]) * 2)
    _vals = m.Plugin()._sysctl_restore_values(
        {"sysctl_baseline": {"kernel": _rel, "values": {_k: _ok_val}}})
    ok(_vals[_k] == _ok_val, "while one within reason is")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("updates and installs: https only, and nothing planted is followed")
try:
    ok(_src_all.count('"--proto", "=https"') >= 2, "the update check's curl calls refuse anything but https")
    _inst = open("install.sh").read()
    ok(_inst.count("--proto '=https'") >= 2, "and so do the installer's")
    ok("*[!A-Za-z0-9.-]*) LATEST_TAG=\"\"" in _inst, "the installer only puts a plain version tag in a URL")
    ok('rm -f "$PLUGIN_DIR/$f.wifiopt-new"' in _script,
       "the update removes anything planted at its staging name before copying")
    ok('rm -f "$PLUGIN_DIR/dist/index.js.map"' in _script, "and at the source map's name")

    class PopenFails(m.Plugin):
        async def check_for_update(self):
            return {"update_available": True, "channel": "stable", "latest_version": "9.9.9"}
    _real_popen = m.subprocess.Popen
    _made = []
    _real_mkdtemp = m.tempfile.mkdtemp
    def _track_mkdtemp(*a, **k):
        d = _real_mkdtemp(*a, **k); _made.append(d); return d
    def _boom(*a, **k): raise OSError("no bash")
    try:
        m.subprocess.Popen = _boom; m.tempfile.mkdtemp = _track_mkdtemp
        pf = PopenFails()
        r = asyncio.run(pf.apply_update())
        ok(r.get("success") is False and not pf._update_running(),
           "an update that could not start does not read as running afterwards")
        ok(_made and not os.path.exists(_made[-1]), "and its script directory is removed")
    finally:
        m.subprocess.Popen = _real_popen; m.tempfile.mkdtemp = _real_mkdtemp

    class MidChange(m.Plugin):
        async def check_for_update(self):
            raise AssertionError("should not get this far")
    mc = MidChange(); mc._profile_change_depth = 1
    ok(asyncio.run(mc.apply_update()).get("success") is False,
       "no update while a lock or band change is mid-flight")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("upgrading from 0.11: what the old settings show is carried over")
try:

    class Upgrade(m.Plugin):
        def __init__(self, band=""):
            super().__init__(); self.band = band
        def _read_profile_field(self, uuid, field, timeout=5):
            return self.band if field == "802-11-wireless.band" else ""
    LU = "dddddddd-0000-0000-0000-0000000000aa"
    old = {**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True, "bssid_lock_connection_uuid": LU,
           "bssid_lock_uuids": [], "last_connection_uuid": LU, "band_preference_enabled": True,
           "band_preference": "a", "band_preference_ssid": "", "band_preference_uuids": [],
           "dns_enabled": True, "dns_uuids": [], "pins_owned": [], "settings_version": 0}
    st_ = dict(old); Upgrade(band="a")._migrate_settings(st_)
    ok(st_["bssid_lock_uuids"] == [LU], "the lock's profile is seeded into the tracking list")
    ok(st_["band_preference_uuids"] == [LU], "a band the last profile still carries is seeded")
    ok(st_["dns_uuids"] == [LU], "and DNS set on it")
    ok(set(st_["pins_owned"]) == {"802-11-wireless.bssid", "802-11-wireless.band"},
       "and both kinds of pin are recorded as ours")
    ok(st_["settings_version"] == m.Plugin._SETTINGS_VERSION, "the migration is recorded")
    st_ = dict(old); Upgrade(band="")._migrate_settings(st_)
    ok(st_["band_preference_uuids"] == [], "a profile without the band is not claimed")
    st_ = {**m.DEFAULT_SETTINGS, "settings_version": 0}; Upgrade()._migrate_settings(st_)
    ok(st_["pins_owned"] == [], "a fresh install owns no pins")
    st_ = {**old, "settings_version": m.Plugin._SETTINGS_VERSION}
    Upgrade(band="a")._migrate_settings(st_)
    ok(st_["bssid_lock_uuids"] == [], "an install already migrated is left alone")

    _main_src = _src_all[_src_all.index("    async def _main(self):"):_src_all.index("    async def _unload(self):")]
    ok(_main_src.index("self._remove_dispatcher()") < _main_src.index("_save_settings(settings)"),
       "a dispatcher that cannot be updated is removed rather than left running the old script")
    ok("settings = _load_settings()" in _main_src[_main_src.index("Fresh: the setters above"):],
       "startup re-reads settings before its last save")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("custom DNS: a list saved with commas is still a list")
try:
    class DnsComma(m.Plugin):
        def __init__(self):
            super().__init__(); self.mods = []
        def _resolve_uuid(self, msg=None): return "dddddddd-0000-0000-0000-0000000000bb", None
        def _nmcli_modify(self, u, k, v, timeout=5):
            self.mods.append((k, v)); return _ok()
        def _hard_reconnect(self, uuid=None): return True
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    dc = DnsComma()
    r = asyncio.run(dc.set_dns(True, "custom", "1.1.1.1, 9.9.9.9"))
    ok(r.get("success") and ("ipv4.dns", "1.1.1.1 9.9.9.9") in dc.mods,
       "comma-separated servers from an older version are accepted")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("6 GHz and the band: what a released pin may and may not be written back over")
try:
    class BandSafe(m.Plugin):
        def __init__(self, scan=""):
            super().__init__(); self.scan = scan
        def _run_cmd(self, cmd, timeout=5):
            if "SSID,BSSID,SIGNAL,FREQ" in " ".join(cmd):
                return _ok(self.scan)
            return _ok()
    bs = BandSafe()
    ok(bs._band_reassert_is_safe({"live": {"frequency": "5180.0"}}, "a", "Net") is True,
       "writing the band back is safe while the device is on that band")
    ok(bs._band_reassert_is_safe({"live": {"frequency": "2437"}}, "a", "Net") is False,
       "but not from 2.4 GHz with nothing on 5 GHz in range, which is how the device got stranded")
    bs = BandSafe("Net:AA\\:BB\\:CC\\:00\\:00\\:01:70:5180 MHz")
    ok(bs._band_reassert_is_safe({"live": {"frequency": "2437"}}, "a", "Net") is True,
       "and is from 2.4 GHz when the network shows a 5 GHz access point")
    bs._released_at = {"802-11-wireless.band": time.time()}
    ok(bs._band_reassert_is_safe({"live": {"frequency": "5180"}}, "a", "Net") is False,
       "never within ten minutes of releasing the band for stranding the device")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("the backend switch: an interface back, and not a second one")
try:
    class Settle(m.Plugin):
        def __init__(self, ifaces, recovers=True):
            super().__init__(); self.ifaces = list(ifaces); self.recovered = 0; self.recovers = recovers
        def _get_wifi_interface(self):
            return self.ifaces.pop(0) if self.ifaces else None
        def _recover_wlan0(self):
            self.recovered += 1
            if self.recovers: self.ifaces = ["wlan0"]
            return self.recovers
    _real_sleep2 = asyncio.sleep
    asyncio.sleep = _no_sleep
    try:
        st = Settle([None, None, None, "wlan31"])
        iface, rec = asyncio.run(st._settle_interface("iwd"))
        ok(iface == "wlan31" and st.recovered == 0 and rec is False,
           "under iwd the plugin waits for iwd's own interface instead of adding wlan0")
        st = Settle([None])
        iface, rec = asyncio.run(st._settle_interface("wpa_supplicant"))
        ok(iface == "wlan0" and st.recovered == 1 and rec is True,
           "under wpa_supplicant a missing wlan0 is recreated")

        class FailedSwitch(Settle):
            def __init__(self):
                super().__init__([None]); self._ensure_backend_switch_state()
            def _session_bus_cmd(self, args): return ["steamosctl"] + args[1:]
            def _run_cmd(self, cmd, timeout=5):
                return {"success": False, "stdout": "", "stderr": "no phy0", "returncode": 1}
            def _steamosctl_backend(self): return "wpa_supplicant"
            def _valve_powersave(self): return None
        fs = FailedSwitch()
        asyncio.run(fs._steamos_manager_backend_switch_worker("wpa_supplicant"))
        res = fs._backend_switch["result"]
        ok(res["success"] is False and fs.recovered == 1 and res.get("needs_reboot") is False,
           "a switch that fails part way still leaves the device with an interface")
    finally:
        asyncio.sleep = _real_sleep2
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("the backend switch on other systems: nothing stopped that NetworkManager still needs")
try:
    class Outranked(m.Plugin):
        def __init__(self):
            super().__init__(); self.cmds = []; self._ensure_backend_switch_state()
        def _merged_nm_backend(self): return "iwd"
        def _run_cmd(self, cmd, timeout=5):
            self.cmds.append(cmd); return _ok()
    _real_conf = m.GENERIC_BACKEND_CONF
    try:
        m.GENERIC_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "99-backend.conf")
        o = Outranked()
        asyncio.run(o._generic_backend_switch_worker("wpa_supplicant"))
        ok(o._backend_switch["result"]["success"] is False and not o.cmds,
           "when another file keeps the old backend in force, nothing is stopped")
        ok(not os.path.exists(m.GENERIC_BACKEND_CONF), "and our file is put back as it was")

        class NeverTakes(Outranked):
            def _merged_nm_backend(self): return None
            def _effective_generic_backend(self): return None
            def _get_wifi_interface(self): return None
        asyncio.sleep = _no_sleep
        try:
            nt = NeverTakes()
            asyncio.run(nt._generic_backend_switch_worker("iwd"))
        finally:
            asyncio.sleep = _real_sleep2
        ok(nt._backend_switch["result"]["success"] is False
           and ["/usr/bin/systemctl", "start", "wpa_supplicant"] in nt.cmds,
           "and a switch that never takes over puts the previous backend back")

        # Bazzite dropped iwd; our file naming it would leave no WiFi at all.
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\nwifi.iwd.autoconnect=yes\n")
        class NoIwd(m.Plugin):
            _IWD_UNITS = ("/nonexistent/iwd.service",)
            def __init__(self):
                super().__init__(); self.cmds = []
            def _run_cmd(self, cmd, timeout=5):
                self.cmds.append(cmd); return _ok()
        ni = NoIwd()
        ok(ni._heal_orphaned_iwd_conf() is True and not os.path.exists(m.GENERIC_BACKEND_CONF)
           and ["/usr/bin/systemctl", "restart", "NetworkManager"] in ni.cmds,
           "a backend setting naming an iwd that is no longer installed is removed at startup")
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        class HasIwd(NoIwd):
            _IWD_UNITS = (m.GENERIC_BACKEND_CONF,)
        ok(HasIwd()._heal_orphaned_iwd_conf() is False and os.path.exists(m.GENERIC_BACKEND_CONF),
           "but left alone while iwd is installed")
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=wpa_supplicant\n")
        ok(NoIwd()._heal_orphaned_iwd_conf() is False, "and when it names wpa_supplicant")
    finally:
        m.GENERIC_BACKEND_CONF = _real_conf
    ok("self._heal_orphaned_iwd_conf()" in _main_src, "_main runs that check")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Valve's power save setting: ours is not left in it")
try:
    _real_valve = m.WIFI_BACKEND_CONF
    try:
        m.WIFI_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "99-valve-wifi-backend.conf")
        vp = m.Plugin()
        ok(vp._valve_powersave() is None, "no file reads as no value")
        open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=2\n[device]\nwifi.backend=wpa_supplicant\n")
        ok(vp._valve_powersave() == "2", "the value is read from the connection section")
        vp._set_valve_powersave("3")
        _txt = open(m.WIFI_BACKEND_CONF).read()
        ok(vp._valve_powersave() == "3" and "wifi.backend=wpa_supplicant" in _txt,
           "and put back without touching Valve's backend line")
        vp._set_valve_powersave(None)
        _txt = open(m.WIFI_BACKEND_CONF).read()
        ok(vp._valve_powersave() is None and "wifi.backend=wpa_supplicant" in _txt,
           "or removed when there was none")
        open(m.WIFI_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        vp._set_valve_powersave("3")
        ok(vp._valve_powersave() == "3" and "wifi.backend=iwd" in open(m.WIFI_BACKEND_CONF).read(),
           "a file without a connection section gains one")

        for before, expect, label in [
            ("?", "3", "with nothing recorded, a 2 goes back to Valve's default"),
            ("", None, "a file that named nothing before goes back to naming nothing"),
            ("3", "3", "a recorded value is what is put back"),
            ("2", "2", "a 2 that was there before the plugin is the user's and stays"),
        ]:
            open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=2\n[device]\nwifi.backend=wpa_supplicant\n")
            vp._release_valve_powersave({"valve_powersave_before": before})
            ok(vp._valve_powersave() == expect, label)
        open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=3\n")
        vp._release_valve_powersave({"valve_powersave_before": "?"})
        ok(vp._valve_powersave() == "3", "a value other than ours is never touched")

        # Taking power save over records what Valve's file said; giving it back restores it.
        _real_nm = m.NM_CONF_PATH
        try:
            m.NM_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
            class PS(m.Plugin):
                def _get_wifi_interface(self): return None
                def _apply_driver_fixes(self, d): pass
                def _apply_pcie_aspm_fix(self, d): return []
            open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=3\n[device]\nwifi.backend=wpa_supplicant\n")
            m._save_settings(dict(m.DEFAULT_SETTINGS))
            asyncio.run(PS().set_power_save(True))
            ok(m._load_settings()["valve_powersave_before"] == "3",
               "turning power save off records Valve's value first")
            # A backend switch then copies our 2 into Valve's file...
            open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=2\n[device]\nwifi.backend=iwd\n")
            asyncio.run(PS().set_power_save(False))
            ok(vp._valve_powersave() == "3" and m._load_settings()["valve_powersave_before"] == "?",
               "and giving it back restores that value")
        finally:
            m.NM_CONF_PATH = _real_nm

        class ValveSwitch(m.Plugin):
            def __init__(self):
                super().__init__(); self._ensure_backend_switch_state()
            def _session_bus_cmd(self, args): return ["steamosctl"] + args[1:]
            def _run_cmd(self, cmd, timeout=5):
                if cmd[0] == "steamosctl":
                    open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=2\n[device]\nwifi.backend=iwd\n")
                return _ok()
            async def _settle_interface(self, backend): return "wlan31", False
            def _get_active_connection_uuid(self): return "u"
            def _steamosctl_backend(self): return "iwd"
        _real_nm = m.NM_CONF_PATH
        try:
            m.NM_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
            open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
            open(m.WIFI_BACKEND_CONF, "w").write("[device]\nwifi.backend=wpa_supplicant\n")
            asyncio.sleep = _no_sleep
            try:
                vs = ValveSwitch()
                asyncio.run(vs._steamos_manager_backend_switch_worker("iwd"))
            finally:
                asyncio.sleep = _real_sleep2
            _txt = open(m.WIFI_BACKEND_CONF).read()
            ok(vs._backend_switch["result"]["success"] and "wifi.powersave" not in _txt
               and "wifi.backend=iwd" in _txt,
               "a backend switch does not leave our power save value in Valve's file")
        finally:
            m.NM_CONF_PATH = _real_nm
    finally:
        m.WIFI_BACKEND_CONF = _real_valve
    for name in ("reset_settings", "_uninstall"):
        body = _src_all[_src_all.index(f"    async def {name}(self"):]
        body = body[:body.index("\n    async def ", 10)]
        ok("self._release_valve_powersave(" in body, f"{name} gives Valve's power save setting back")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("SteamOS after 3.9: Valve's holo names")
try:
    ok(m._HOLO_BACKEND_HELPER == "/usr/bin/holo-polkit-helpers/holo-wifi-set-backend-privileged",
       "the renamed backend helper is known")
    ok("HOLO_NM_DEFAULT_CONF," in _src_all[_src_all.index("def _get_current_backend"):][:1500],
       "and the renamed NetworkManager defaults are read for the backend")
    class Holo(m.Plugin): pass
    _real_open = open
    import builtins
    def _fake_open(path, *a, **k):
        if path == "/etc/os-release":
            import io; return io.StringIO('ID=holo\nPRETTY_NAME="SteamOS Holo"\n')
        return _real_open(path, *a, **k)
    builtins.open = _fake_open
    try:
        ok(Holo()._detect_distro() == {"id": "steamos", "name": "SteamOS Holo"},
           "an image that identifies as holo is treated as SteamOS")
    finally:
        builtins.open = _real_open
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("concurrency: one profile change at a time")
try:
    for name, call in [("set_bssid_lock", lambda p: p.set_bssid_lock(True)),
                       ("set_band_preference", lambda p: p.set_band_preference(True, "a")),
                       ("allow_wpa2", lambda p: p.allow_wpa2())]:
        p = m.Plugin(); p._profile_change_depth = 1
        r = asyncio.run(call(p))
        ok(r.get("error") == "busy" and p._profile_change_depth == 1,
           f"{name} refuses while another profile change is mid-flight, and leaves its guard alone")
        p = m.Plugin(); p._update_starting = True
        ok(asyncio.run(call(p)).get("error") == "busy", f"{name} refuses while an update installs")
    _lock_src = _src_all[_src_all.index("    async def set_bssid_lock"):_src_all.index("    async def set_band_preference")]
    ok("await asyncio.to_thread(\n                    self._settle_band_before_locking" in _lock_src,
       "the lock's band settling (a scan and a radio cycle) runs off the event loop")
    _band_src = _src_all[_src_all.index("    async def set_band_preference"):]
    ok("await asyncio.to_thread(self._band_unreachable" in _band_src[:6000],
       "and so does the band setter's 15 second scan")

    class Relock(m.Plugin):
        def __init__(self):
            super().__init__(); self.mods = []
        def _get_wifi_interface(self): return "wlan0"
        def _get_active_connection_uuid(self): return "u"
        def _run_cmd(self, cmd, timeout=5): return _ok("Connected to 02:00:00:00:00:22 (on wlan0)\n\tfreq: 5180\n")
        def _nmcli_modify(self, u, k, v, timeout=5): self.mods.append((k, v)); return _ok()
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": False})
    rl = Relock(); rl._relock_after_band_change("u")
    ok(not rl.mods, "a lock turned off while the band changed is not put back")
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True})
    rl = Relock(); rl._relock_after_band_change("u")
    ok(("802-11-wireless.bssid", "02:00:00:00:00:22") in rl.mods, "one still on is")

    # Two cleanups for one list in one pass must both land.
    class TwoCleanups(m.Plugin):
        def _nmcli_modify(self, u, k, v, timeout=5): return _ok()
    A1, A2 = "dddddddd-0000-0000-0000-0000000000c1", "dddddddd-0000-0000-0000-0000000000c2"
    _st = {**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True, "bssid_lock_uuids": [A1, A2]}
    _pending = {}; _status = {"external": {}, "drift": {}}
    tc = TwoCleanups()
    for u in (A1, A2):
        tc._apply_pin_cleanup({"kind": "pin_cleanup", "uuid": u, "property": "802-11-wireless.bssid",
                               "list_key": "bssid_lock_uuids", "off_value": "", "stranded": True},
                              _st, _status, _pending)
    ok(_pending.get("bssid_lock_uuids") == [], "two releases on one list in one pass both stick")

    # set_regdomain must not write back a copy taken before iw ran.
    class RegRace(m.Plugin):
        def _regdomain_info(self): return {"readable": True, "global": "00"}
        def _run_cmd(self, cmd, timeout=5):
            if cmd[:3] == ["/usr/bin/iw", "reg", "set"]:
                s2 = m._load_settings(); s2["cake_enabled"] = True; m._save_settings(s2)
            return _ok()
    m._save_settings({**m.DEFAULT_SETTINGS, "cake_enabled": False})
    asyncio.run(RegRace().set_regdomain("DE"))
    _after = m._load_settings()
    ok(_after["cake_enabled"] is True and _after["regdomain"] == "DE" and _after["regdomain_previous"] == "00",
       "setting the region keeps a change saved while it ran")

    # A collector that never returns is reported, cached result or not.
    class Wedged(m.Plugin): pass
    w_ = Wedged(); w_._collect_depth = 1; w_._collect_started_at = time.monotonic() - 120
    w_._last_status = {"success": True, "connected": True, "settings": {}}
    ok(asyncio.run(w_.get_status()).get("success") is False,
       "a wedged status read is reported even when an older result could be handed back")
    w_._collect_started_at = time.monotonic()
    ok(asyncio.run(w_.get_status()).get("connected") is True, "while a merely slow one reuses it")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("the access point move: up means activated, not activating")
try:
    class Act(m.Plugin):
        def __init__(self, out): super().__init__(); self.out = out
        def _run_cmd(self, cmd, timeout=5): return _ok(self.out)
    ok(Act("u1:activated\nu2:activating")._connection_activated("u1") is True, "activated counts")
    ok(Act("u1:activating")._connection_activated("u1") is False,
       "activating does not, since that is where a refused handshake sits")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Force Reapply: the lock stays on the network it was set for")
try:
    class Reapply(m.Plugin):
        def __init__(self, here):
            super().__init__(); self.here = here; self.locks = 0
        def _get_active_connection_uuid(self): return "dddddddd-0000-0000-0000-0000000000d2"
        def _get_profile_ssid(self, uuid, timeout=5):
            return {"dddddddd-0000-0000-0000-0000000000d1": "Home"}.get(uuid, self.here)
        async def set_bssid_lock(self, enabled): self.locks += 1; return {"success": True}
        async def set_power_save(self, d): return {"success": True}
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True, "auto_fix_on_wake": False,
                      "bssid_lock_connection_uuid": "dddddddd-0000-0000-0000-0000000000d1"})
    ra = Reapply("Cafe"); asyncio.run(ra.reapply_all())
    ok(ra.locks == 0, "reapplying on another network does not pin its access point")
    ra = Reapply("Home"); asyncio.run(ra.reapply_all())
    ok(ra.locks == 1, "while another saved copy of the locked network does get it")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("dispatcher: written whole, and inert without python3")
try:
    _real_disp = m.DISPATCHER_PATH; _real_pdir = m.decky.DECKY_PLUGIN_DIR
    try:
        m.DISPATCHER_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer")
        m.decky.DECKY_PLUGIN_DIR = os.getcwd()
        ok(m.Plugin()._install_dispatcher() is True
           and os.stat(m.DISPATCHER_PATH).st_mode & 0o777 == 0o755
           and os.listdir(os.path.dirname(m.DISPATCHER_PATH)) == ["99-wifi-optimizer"],
           "the dispatcher is renamed into place, executable, with no temp file left")
    finally:
        m.DISPATCHER_PATH = _real_disp; m.decky.DECKY_PLUGIN_DIR = _real_pdir
    ok(_tmpl.index("command -v python3") < _tmpl.index("SETTINGS_RAW=$(python3"),
       "without python3 the dispatcher applies nothing rather than its fallbacks")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("reach check: the cached scan only")
try:
    r = Reach("sae", JASONS_ROOM)
    r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
    ok(r.cached_only is True, "the poll's security check never makes nmcli rescan")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("frontend: failures said, controls held during an update")
try:
    _tsx = open("src/index.tsx").read()
    _ht = _tsx[_tsx.index("const handleToggle = async"):]
    _ht = _ht[:_ht.index("return outcome;")]
    ok("} catch (e) {" in _ht, "a toggle whose call throws shows an error instead of nothing")
    ok("disabled={isBusy}" not in _tsx and _tsx.count("isBusy || updating") >= 15,
       "every control is held while an update installs")
    ok("const UPDATE_TIMEOUT_MS = 6 * 60 * 1000;" in _tsx,
       "the panel waits longer for an update than the script allows its download")
    _bp = _tsx[_tsx.index("const beginBackendPoll"):]
    _bp = _bp[:_bp.index("}, BACKEND_POLL_INTERVAL);")]
    ok("if (inFlight) return;" in _bp, "backend switch polls never overlap")
    ok("failures >= BACKEND_POLL_MAX_FAILURES" in _bp, "one failed poll does not abandon a switch")
    ok("BACKEND_SWITCH_MAX_S" in _bp, "and a switch that never reports back releases the panel")
    _rs = _tsx[_tsx.index("backend.resetSettings()") - 400:_tsx.index("backend.resetSettings()")]
    ok("resetDnsDraft();" in _rs and "setNotices({});" in _rs,
       "Reset clears the DNS draft and the notices along with everything else")
    ok(not re.search(r"<span[^>]*onClick=\{handle(AllowWpa2|RepairDrift)\}", _tsx)
       and "onClick={handleAllowWpa2}" in _tsx and "onClick={handleRepairDrift}" in _tsx,
       "Allow WPA2 and Fix now are buttons the D-pad can reach, not clickable text")
    ok("anyone nearby can broadcast your network's name" in _tsx,
       "Allow WPA2 warns that a same-named access point may not be yours")
    ok("this.setState({ err: null })" in _tsx, "the error screen offers a way back without restarting")
    _dist = open("dist/index.js").read()
    ok("Couldn't reach the plugin's backend" in _dist and "Try again" in _dist,
       "and the built bundle carries these")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("iwd: the lock and the band stand down, because NetworkManager cannot use them there")
try:
    # NetworkManager's iwd backend lists access points with made-up addresses on a
    # fixed 2.4 GHz frequency, and only activates a profile through one it finds
    # compatible: a real address pin or band "a" matches none, so every
    # reconnect NetworkManager makes asks for the password (issue #6).
    class OnIwd(m.Plugin):
        def _backend_is_iwd(self): return True
    class OnWpa(m.Plugin):
        def _backend_is_iwd(self): return False
    _lockset = {**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True, "band_preference_enabled": True}
    _st = {"live": {}}
    _eff = OnIwd()._pins_for_backend(_lockset, _st)
    ok(not _eff["bssid_lock_enabled"] and not _eff["band_preference_enabled"]
       and _st["live"].get("pins_inactive_iwd") is True,
       "under iwd a status pass treats both pins as off, and says so")
    _st = {"live": {}}
    ok(OnWpa()._pins_for_backend(_lockset, _st) is _lockset and not _st["live"],
       "under wpa_supplicant nothing changes")
    r = asyncio.run(OnIwd().set_bssid_lock(True))
    ok(r.get("error") == "needs_wpa_supplicant", "turning the lock on under iwd is refused with the reason")
    r = asyncio.run(OnIwd().set_band_preference(True, "a"))
    ok(r.get("error") == "needs_wpa_supplicant", "and so is the band preference")
    class IwdOff(OnIwd):
        def _release_lock_from_all_profiles(self): self.released = True; return None
    io = IwdOff(); r = asyncio.run(io.set_bssid_lock(False))
    ok(r.get("success") and io.released, "turning the lock off still works under iwd")

    class IwdOptimize(OnIwd):
        def __init__(self):
            super().__init__(); self.locked = False
        async def set_auto_fix(self, e): return {"success": True}
        async def set_buffer_tuning(self, e): return {"success": True}
        async def set_power_save(self, d): return {"success": True}
        async def set_bssid_lock(self, e): self.locked = True; return {"success": True}
    io = IwdOptimize(); r = asyncio.run(io.optimize_safe())
    ok(not io.locked and r.get("total") == 3 and r.get("applied") == 3,
       "Optimize Safe leaves the lock out under iwd instead of reporting it failed")

    # Pins we wrote are cleared under iwd, even with the setting still on.
    LOCKU = "dddddddd-0000-0000-0000-0000000000e1"
    class PinnedIwd(Pinned):
        def _backend_is_iwd(self): return True
        def _get_active_connection_uuid(self): return LOCKU
    m._save_settings({**base, "bssid_lock_enabled": True, "bssid_lock_uuids": [LOCKU],
                      "bssid_lock_connection_uuid": LOCKU,
                      "pins_owned": ["802-11-wireless.bssid"]})
    pin = PinnedIwd(bssid="02\\:00\\:00\\:00\\:00\\:11")
    st = asyncio.run(pin.get_status())
    ok((LOCKU, "802-11-wireless.bssid", "") in pin.mods
       and not any(k == "802-11-wireless.bssid" and v for _u, k, v in pin.mods),
       "a lock pin we wrote is cleared under iwd and no new one is written")
    ok(m._load_settings()["bssid_lock_enabled"] is True and st.get("live", {}).get("pins_inactive_iwd"),
       "while the setting stays on for when wpa_supplicant is back")

    class ToIwd(m.Plugin):
        def __init__(self):
            super().__init__(); self.cleared = False
        def _has_backend_tool(self): return True
        def _get_current_backend(self): return "wpa_supplicant"
        def _get_backend_method(self): return "steamos_manager"
        def _clear_lock_and_band_pins(self): self.cleared = True
        async def _steamos_manager_backend_switch_worker(self, target): await asyncio.sleep(0)
    ti = ToIwd(); r = asyncio.run(ti.start_backend_switch("iwd"))
    ok(r.get("accepted") and ti.cleared, "switching to iwd takes our pins off first")
    ti = ToIwd(); ti._get_current_backend = lambda: "iwd"
    r = asyncio.run(ti.start_backend_switch("wpa_supplicant"))
    ok(r.get("accepted") and not ti.cleared, "switching away from iwd leaves them to come back")
    ok("IWD_PIN_NOTICE" in open("src/index.tsx").read()
       and "Paused while the WiFi backend is iwd" in open("dist/index.js").read(),
       "the panel says why the lock and band are not applied under iwd")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("review round: what the fixes themselves got wrong")
try:
    # iwd reached without the plugin's switch: every pin of ours comes off
    # at once, whether or not the panel is open.
    class UnpinIwd(m.Plugin):
        def __init__(self, iwd=True):
            super().__init__(); self.iwd = iwd; self.mods = []
        def _backend_is_iwd(self): return self.iwd
        def _nmcli_modify(self, u, k, v, timeout=5): self.mods.append((u, k, v)); return _ok()
    P1, P2 = "dddddddd-0000-0000-0000-0000000000f1", "dddddddd-0000-0000-0000-0000000000f2"
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True,
                      "bssid_lock_uuids": [P1, P2], "band_preference_uuids": [P2],
                      "pins_owned": ["802-11-wireless.bssid"]})
    ui = UnpinIwd(); asyncio.run(ui._unpin_if_iwd())
    ok({(P1, "802-11-wireless.bssid", ""), (P2, "802-11-wireless.bssid", ""),
        (P2, "802-11-wireless.band", "")} <= set(ui.mods),
       "under iwd every profile we pinned is unpinned in one go")
    n = len(ui.mods); asyncio.run(ui._unpin_if_iwd())
    ok(len(ui.mods) == n, "and only once while iwd stays in use")
    ui.iwd = False; asyncio.run(ui._unpin_if_iwd()); ui.iwd = True
    asyncio.run(ui._unpin_if_iwd())
    ok(len(ui.mods) > n, "but again after a spell back on wpa_supplicant")
    ok("await self._unpin_if_iwd()" in open("main.py").read().split("async def _stranded_watch_once")[1][:300],
       "the background watch runs it, so it does not wait for the panel")

    # The iwd tag lifts the enabled check for the two pins only, never IPv6.
    class IwdTag(m.Plugin):
        def _collect_status(self):
            return ({"success": True, "live": {"pins_inactive_iwd": True}, "drift": {},
                     "external": {}, "settings": {}}, {},
                    [{"kind": "pin_cleanup", "uuid": "u", "property": "ipv6.method",
                      "list_key": "ipv6_uuids", "off_value": "auto"},
                     {"kind": "pin_cleanup", "uuid": "u", "property": "802-11-wireless.bssid",
                      "list_key": "bssid_lock_uuids", "off_value": ""}])
        def _apply_status_actions(self, status, pending, actions): self.seen = actions
    it = IwdTag(); asyncio.run(it.get_status())
    ok(not it.seen[0].get("iwd") and it.seen[1].get("iwd"),
       "under iwd only the address and band cleanups bypass the enabled check")

    # A failed generic switch leaves only the previous backend enabled.
    class Rollback(m.Plugin):
        def __init__(self): super().__init__(); self.cmds = []
        def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
    _rc = m.GENERIC_BACKEND_CONF
    try:
        m.GENERIC_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "99-b.conf")
        rb = Rollback(); rb._restore_generic_backend("wpa_supplicant")
        ok(rb.cmds[0] == ["/usr/bin/systemctl", "disable", "--now", "iwd"]
           and ["/usr/bin/systemctl", "start", "wpa_supplicant"] in rb.cmds,
           "rolling back stops and disables the backend that did not take")
    finally:
        m.GENERIC_BACKEND_CONF = _rc

    # Valve's helper drops the power save line; it is put back.
    _rv = m.WIFI_BACKEND_CONF
    try:
        m.WIFI_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "99-valve.conf")
        open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=3\n[device]\nwifi.backend=iwd\n")
        class Legacy(m.Plugin):
            def __init__(self): super().__init__(); self._ensure_backend_switch_state()
            def _run_cmd(self, cmd, timeout=5):
                if cmd[1:2] == ["write_config"]:
                    open(m.WIFI_BACKEND_CONF, "w").write("[device]\nwifi.backend=wpa_supplicant\n")
                return _ok()
            def _get_wifi_interface(self): return "wlan0"
            def _get_active_connection_uuid(self): return "u"
            def _get_current_backend(self): return "wpa_supplicant"
        asyncio.sleep = _no_sleep
        try:
            asyncio.run(Legacy()._backend_switch_worker("wpa_supplicant"))
        finally:
            asyncio.sleep = _real_sleep2
        _t = open(m.WIFI_BACKEND_CONF).read()
        ok("wifi.powersave=3" in _t and "wifi.backend=wpa_supplicant" in _t,
           "a switch through Valve's helper keeps Steam's power save setting")
    finally:
        m.WIFI_BACKEND_CONF = _rv

    # A long lock list does not crowd out the band candidates.
    many = [f"dddddddd-0000-0000-0000-0000000001{i:02d}" for i in range(16)]
    BU = "dddddddd-0000-0000-0000-0000000002aa"
    cands = m.Plugin()._stranded_candidates({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True,
        "band_preference_enabled": True, "bssid_lock_uuids": many,
        "band_preference_uuids": [BU]})
    ok(BU in cands and "802-11-wireless.band" in cands[BU]
       and len(cands) <= 2 * m.Plugin._STRANDED_CANDIDATES + 2,
       "the release looks at recent profiles of each kind, bounded")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
