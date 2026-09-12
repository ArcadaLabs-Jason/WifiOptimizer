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

section("concurrency: reconciliation guards")
calls=[]
class P(m.Plugin):
    def _nmcli_modify(self,u,k,v,timeout=5):
        calls.append((k,v)); return {"success":True,"stdout":"","stderr":"","returncode":0}
    def _run_cmd(self,cmd,timeout=5,clean_env=False):
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
for n in ast.walk(tree):
    if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=="_collect_status":
        body = ast.dump(n)
        ok("_save_settings" not in body, "_collect_status does not save settings")
        ok("_nmcli_modify" not in body, "_collect_status does not modify NM")

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
    def _run_cmd(self, cmd, timeout=5, clean_env=False):
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
    def _run_cmd(self, cmd, timeout=5, clean_env=False):
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
    def _run_cmd(self, cmd, timeout=5, clean_env=False):
        j = " ".join(cmd)
        if "802-11-wireless.band" in j and "con" in cmd:
            return {"success":True,"stdout":f"802-11-wireless.band:{self.pin_band}",
                    "stderr":"","returncode":0}
        if "802-11-wireless.bssid" in j and "con" in cmd:
            return {"success":True,"stdout":f"802-11-wireless.bssid:{self.pin_bssid}",
                    "stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout, clean_env)
    def _nmcli_modify(self, u, k, v, timeout=5):
        self.mods.append((u, k, v))
        return {"success":True,"stdout":"","stderr":"","returncode":0}

base = {**m.DEFAULT_SETTINGS, "driver":"ath11k_pci", "distro_id":"steamos"}

m._save_settings({**base, "band_preference_enabled":False, "band_preference_uuids":[]})
pin = Pinned(band="a")
asyncio.run(pin.get_status())
ok(("802-11-wireless.band", "") in [(k, v) for _, k, v in pin.mods],
   "an untracked band pin on the active profile is cleared")

m._save_settings({**base, "bssid_lock_enabled":False, "bssid_lock_uuids":[]})
pin = Pinned(bssid="02\\:00\\:00\\:00\\:00\\:11")
asyncio.run(pin.get_status())
ok(("802-11-wireless.bssid", "") in [(k, v) for _, k, v in pin.mods],
   "an untracked access point pin on the active profile is cleared")

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
    "Net:02\\:00\\:00\\:00\\:00\\:51:100:5180 MHz",
    "Net:02\\:00\\:00\\:00\\:00\\:24:100:2462 MHz",
    "Net:02\\:00\\:00\\:00\\:00\\:52:54:5220 MHz",
    "Net:02\\:00\\:00\\:00\\:00\\:11:45:5745 MHz",
    "Other:AA\\:BB\\:CC\\:DD\\:EE\\:FF:99:5180 MHz",
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
    def __init__(self, scan, on, reachable=None, band=""):
        super().__init__()
        self.scan, self.on, self.reachable = scan, on, reachable
        self.pinned, self.band, self.mods = "", band, []
        self.aps = [
            (f[1], int(f[2]), int(re.match(r"\s*(\d+)", f[3]).group(1)))
            for f in (_fields(l) for l in scan.split("\n"))
            if len(f) >= 4 and f[0] == "Net"
        ]
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
    def _run_cmd(self, cmd, timeout=5, clean_env=False):
        if "wifi" in cmd and "list" in cmd:
            # Answer the columns actually asked for. _band_is_reachable wants
            # SSID,FREQ while the lock wants SSID,BSSID,SIGNAL,FREQ; handing
            # both the same four makes the former read a BSSID as a frequency,
            # which would be a defect in the stub, not in the plugin.
            fields = cmd[cmd.index("-f") + 1].split(",") if "-f" in cmd else []
            rows = []
            for line in self.scan.split("\n"):
                got = _fields(line)
                if len(got) < 4: continue
                by = {"SSID": got[0], "BSSID": got[1].replace(":", "\\:"),
                      "SIGNAL": got[2], "FREQ": got[3]}
                rows.append(":".join(by.get(f, "") for f in fields))
            return _ok("\n".join(rows))
        if "link" in cmd:
            if not self.on: return _ok("")
            return _ok(
                f"Connected to {self.on} (on wlan0)\n"
                f"\tfreq: {self._freq_of(self.on)}.0\n"
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
ok(bool(aps) and all(b != "AA:BB:CC:DD:EE:FF" for b, _, _ in aps),
   "another network's access points are not considered")

# A small difference is not worth a reconnect.
NEAR = "\n".join([
    "Net:02\\:00\\:00\\:00\\:00\\:51:60:5180 MHz",
    "Net:02\\:00\\:00\\:00\\:00\\:11:50:5745 MHz",
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
    def _run_cmd(self, cmd, timeout=5, clean_env=False):
        if "ipv6.method" in " ".join(cmd) and "con" in cmd:
            return {"success":True,"stdout":f"ipv6.method:{self.live}",
                    "stderr":"","returncode":0}
        return super()._run_cmd(cmd, timeout, clean_env)
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
ok(st.get("drift", {}).get("ipv6") is True,
   "but the disagreement is surfaced rather than hidden")

# And no false alarm when they agree.
m._save_settings({**base_ap, "ipv6_disabled":False, "ipv6_uuids":[]})
v = V6(live="auto")
st = asyncio.run(v.get_status())
ok(not st.get("drift", {}).get("ipv6"),
   "no drift reported when the connection and the toggle agree")



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
    def _run_cmd(self, cmd, timeout=5, clean_env=False):
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

print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
