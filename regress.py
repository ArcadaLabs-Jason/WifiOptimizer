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
# Nor where streaming auto mode keeps its state, or the host's processes.
_run_dir = tempfile.mkdtemp()
m.STREAM_FLAG_FILE = os.path.join(_run_dir, "streaming")
m.STREAM_HOLD_FILE = os.path.join(_run_dir, "streaming-hold")
m.PCIE_BASELINE_FILE = os.path.join(_run_dir, "pcie-baseline")
m.BACKEND_SWITCH_MARKER = os.path.join(_run_dir, "backend-switch")
m.UPDATE_LOCK_FILE = os.path.join(_run_dir, "update.lock")
# And the lock the plugin and the dispatcher share while the fixes change.
m.APPLY_LOCK_FILE = os.path.join(_run_dir, "apply.lock")
m.PROC_DIR = os.path.join(tempfile.mkdtemp(), "no-proc")
# Nor the host's radios or radio switches: the backend switch's interface
# recovery lists every wiphy, and the not-connected reason reads rfkill.
m.SYSFS_IEEE80211 = os.path.join(tempfile.mkdtemp(), "no-ieee80211")
m.Plugin.RFKILL_DIR = os.path.join(tempfile.mkdtemp(), "no-rfkill")
# Nor where the host keeps its tools: a check run on a Mac would otherwise
# see /usr/bin/sysctl turned into the Mac's /usr/sbin/sysctl.
_REAL_TOOL_DIRS = getattr(m, "TOOL_DIRS", ())
m.TOOL_DIRS = ()
FAILS = []
def ok(c, l):
    print(("PASS  " if c else "FAIL  ")+l)
    if not c: FAILS.append(l)

def section(t): print(f"\n--- {t} ---")

def _maybe_run(result):
    """Run a coroutine to its end; anything else is already the result.
    For calls that became coroutines, so older code still runs the check."""
    return asyncio.run(result) if asyncio.iscoroutine(result) else result

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

section("security: a directory of the user's swapped for a link")
# The settings and log directories belong to the desktop user, who can swap
# one for a link while root works in it. Every write, move and removal there
# goes through _open_dir_no_follow, which refuses a link at any component.
try:
    _odnf = getattr(m, "_open_dir_no_follow", None)
    _top = tempfile.mkdtemp(); _away = tempfile.mkdtemp()
    os.mkdir(os.path.join(_away, "plugin"))
    open(os.path.join(_away, "plugin", "settings.json"), "w").write("PRECIOUS")
    open(os.path.join(_away, "plugin", "keep.log"), "w").write("PRECIOUS")
    os.symlink(_away, os.path.join(_top, "settings"))            # a link mid-path
    os.symlink(os.path.join(_away, "plugin"), os.path.join(_top, "flat"))  # and at the end
    _real = os.path.join(_top, "real"); os.mkdir(_real)
    for _via in ("settings/plugin", "flat"):
        _p = os.path.join(_top, _via, "settings.json")
        ok(m._write_no_follow(_p, "x") is False
           and open(os.path.join(_away, "plugin", "settings.json")).read() == "PRECIOUS",
           f"a write through a linked directory ({_via}) is refused")
        try:
            m._remove_no_follow(_p); _removed = True
        except OSError:
            _removed = False
        ok(not _removed and os.path.exists(os.path.join(_away, "plugin", "settings.json")),
           f"and so is a removal ({_via})")
        _saved = m.SETTINGS_FILE
        try:
            m.SETTINGS_FILE = _p
            try:
                m._save_settings({"x": 1}); _wrote = True
            except OSError:
                _wrote = False
            ok(not _wrote and open(os.path.join(_away, "plugin", "settings.json")).read() == "PRECIOUS",
               f"and a settings save ({_via})")
            open(os.path.join(_away, "plugin", "settings.json"), "w").write("{not json")
            ok(m._load_settings() == m.DEFAULT_SETTINGS
               and open(os.path.join(_away, "plugin", "settings.json")).read() == "{not json"
               and not os.path.exists(os.path.join(_away, "plugin", "settings.json.corrupt")),
               f"and the settings read neither follows it nor sets the file there aside ({_via})")
            open(os.path.join(_away, "plugin", "settings.json"), "w").write("PRECIOUS")
        finally:
            m.SETTINGS_FILE = _saved
    _errs = []
    _real_err = m.decky.logger.error
    m.decky.logger.error = staticmethod(lambda msg: _errs.append(msg))
    try:
        getattr(m, "_REFUSED_LINKS", set()).clear()
        _lp = os.path.join(_top, "settings", "plugin", "x.json")
        m._write_no_follow(_lp, "x"); m._write_no_follow(_lp, "x")
    finally:
        m.decky.logger.error = _real_err
    ok(len([e for e in _errs if "'settings'" in e]) == 1,
       f"a refused link is logged once, naming it, so a moved folder is not a silent reset ({_errs})")
    _p = os.path.join(_real, "settings.json")
    ok(m._write_no_follow(_p, "ok") is True and open(_p).read() == "ok",
       "while a real directory is written as before")
    _saved = m.SETTINGS_FILE
    try:
        m.SETTINGS_FILE = os.path.join(_real, "new", "settings.json")
        m._save_settings({"bssid_lock_enabled": True})
        ok(m._load_settings().get("bssid_lock_enabled") is True
           and oct(os.stat(m.SETTINGS_FILE).st_mode & 0o777) == "0o644"
           and not [f for f in os.listdir(os.path.dirname(m.SETTINGS_FILE)) if f.endswith(".tmp")],
           "and a save creates a missing settings directory, leaves 0644 and no temp file")
    finally:
        m.SETTINGS_FILE = _saved
    _host_tmp = tempfile.gettempdir()
    if os.path.realpath(_host_tmp) != os.path.abspath(_host_tmp):
        # macOS: /var is a link root made in /, which only root can write.
        _fd = _odnf(_host_tmp); os.close(_fd)
        ok(True, "a link only root could have made (here the host's /var) is followed")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("security: the dispatcher's settings read")
try:
    import subprocess as _sp
    _tm = open("defaults/dispatcher.sh.tmpl").read()
    _prog = _tm[_tm.index("python3 -I -c '") + len("python3 -I -c '"):]
    _prog = _prog[:_prog.index("\n' ")]
    def _disp(path):
        return _sp.run([sys.executable, "-I", "-c", _prog, path], capture_output=True,
                       text=True, timeout=10).stdout.split("\n")[:5]
    _dd = tempfile.mkdtemp()
    _sf = os.path.join(_dd, "settings.json")
    open(_sf, "w").write(json.dumps({"power_save_disabled": False, "cake_enabled": True,
                                     "driver": "ath11k_pci", "streaming_mode_enabled": True}))
    ok(_disp(_sf) == ["False", "False", "ath11k_pci", "True", "True"],
       "it reads the settings it applies")
    _lk = os.path.join(_dd, "link.json"); os.symlink(_sf, _lk)
    ok(_disp(_lk) == ["True", "False", "unknown", "False", "False"],
       "a link at the name is not followed: safe defaults")
    _ff = os.path.join(_dd, "fifo.json"); os.mkfifo(_ff)
    try:
        ok(_disp(_ff) == ["True", "False", "unknown", "False", "False"],
           "and a FIFO neither blocks the connect nor reads as settings")
    except _sp.TimeoutExpired:
        ok(False, "and a FIFO neither blocks the connect nor reads as settings (it hung)")
    ok("python3 -I -c" in _tm and "python3 -c" not in _tm,
       "the dispatcher runs its Python isolated from the environment")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

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
# hashlib (with _blake2 and _sha512 or _sha2) and base64 (with binascii) are
# in both, for the release signature check.
# threading: imported unconditionally by subprocess and asyncio themselves
# (subprocess.py and asyncio/events.py both start with it), so no build that
# runs either can lack it; the process search's single worker needs it.
BUNDLED = {"os", "re", "copy", "pwd", "json", "tempfile", "time",
           "asyncio", "subprocess", "decky", "hashlib", "base64", "threading"}
# An import inside `try: ... except ImportError` cannot stop the backend
# loading; the code there carries on without the module. Only fcntl, for the
# apply lock, is imported that way, and only inside the function using it.
OPTIONAL = {"fcntl"}
_imported = set()
_guarded = set()
for node in ast.walk(_tree):
    if isinstance(node, ast.Try) and any(
            isinstance(h.type, ast.Name) and h.type.id == "ImportError" for h in node.handlers):
        for inner in node.body:
            if isinstance(inner, ast.Import):
                _guarded.update(a.name.split(".")[0] for a in inner.names)
for node in ast.walk(_tree):
    if isinstance(node, ast.Import):
        _imported.update(a.name.split(".")[0] for a in node.names)
    elif isinstance(node, ast.ImportFrom) and node.level == 0:
        _imported.add(node.module.split(".")[0])
_unguarded_optional = (_imported & OPTIONAL) - _guarded
ok(_imported - OPTIONAL <= BUNDLED and not _unguarded_optional,
   f"main.py imports only modules Decky bundles (extra: {sorted(_imported - BUNDLED - OPTIONAL)}, "
   f"optional but not guarded: {sorted(_unguarded_optional)})")

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

section("runtime: a command that will not die does not hold the plugin")
try:
    import subprocess as _sp3
    # iw or tc behind a WiFi driver that wedged on a bad wake sit in the
    # kernel, where SIGKILL does not reach them. subprocess.run kills a child
    # that timed out and then waits for it with no limit, which on the event
    # loop froze every call the panel makes. This child ignores the kill and
    # only ever comes back from a wait that has a limit.
    class _Stuck:
        waits = []
        def __init__(self, *a, **k):
            self.args = a[0] if a else k.get("args"); self.returncode = None
            self.pid = 4242; self.stdout = self.stderr = None
        def _wait(self, kind, timeout):
            _Stuck.waits.append((kind, timeout))
            if timeout is None:
                time.sleep(1.5)  # stands in for forever
                return ("", "") if kind == "communicate" else 0
            time.sleep(min(timeout, 0.05))
            raise _sp3.TimeoutExpired(self.args, timeout)
        def communicate(self, input=None, timeout=None): return self._wait("communicate", timeout)
        def wait(self, timeout=None): return self._wait("wait", timeout)
        def poll(self): return None
        def kill(self): pass
        def send_signal(self, sig): pass
        def terminate(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): self.wait()
    _real_popen3 = m.subprocess.Popen
    try:
        m.subprocess.Popen = _Stuck
        _r3 = m.Plugin()._run_cmd(["/usr/bin/iw", "dev", "wlan0", "link"], timeout=1)
    finally:
        m.subprocess.Popen = _real_popen3
    ok(not [w for w in _Stuck.waits if w[1] is None]
       and _r3 == {"success": False, "stdout": "", "stderr": "Command timed out", "returncode": -1},
       f"a child that survives its kill is left behind, never waited on without a limit ({_Stuck.waits})")
    _p3 = m.Plugin()
    ok(_p3._run_cmd(["/bin/sh", "-c", "echo out; echo err >&2; exit 3"])
       == {"success": False, "stdout": "out", "stderr": "err", "returncode": 3},
       "a real command's output and exit status come back as they always did")
    _t3 = time.monotonic(); _r3 = _p3._run_cmd(["/bin/sleep", "5"], timeout=1)
    ok(_r3["stderr"] == "Command timed out" and time.monotonic() - _t3 < 3,
       "and a real one that overruns is killed at its timeout")
    ok(_p3._run_cmd(["/nonexistent/wifiopt-x"])["stderr"] == "Command not found: /nonexistent/wifiopt-x",
       "a missing command is still named")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

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
# Relative to now, not zero: the monotonic clock starts at boot, so on a
# machine that has just started, zero is still inside the window.
r._reach_checked_at = time.monotonic() - (m.Plugin._DUPLICATE_CHECK_SECONDS + 1)
r._recheck_security_reach(WU, "wlan0", "Home", "62:e9:31:0d:1f:e7")
ok(r._wpa2_blocked is None, "and past it the finding is re-read, so it clears when the radio goes")

# A BSSID pin stops background scans, so the cached list keeps the other
# access points at what they read when the pin was set.
class PinnedReach(Reach):
    def __init__(self, km, aps, stream=False):
        super().__init__(km, aps); self.scans = []; self.reads = 0; self.stream = stream
    def _visible_access_points(self, iface, ssid, rescan=False, cached_only=False):
        self.reads += 1; return super()._visible_access_points(iface, ssid, rescan, cached_only)
    def _run_cmd(self, cmd, timeout=5):
        if "rescan" in cmd: self.scans.append(list(cmd))
        return {"success": True, "stdout": "", "stderr": "", "returncode": 0}
    def _stream_running(self, settings): return self.stream
    # The status read's own, which never waits on the process search (L7).
    def _stream_running_now(self, settings): return self.stream

ON = "62:e9:31:0d:1f:e7"
PIN = "62:E9:31:0D:1F:E7"
def reach_later(r, back):
    # Every remembered time moved `back` seconds into the past, relative to
    # now and never zero (the monotonic clock starts at boot).
    for name in ("_reach_checked_at", "_lock_scan_at"):
        if getattr(r, name, None) is not None:
            setattr(r, name, getattr(r, name) - back)
    for name in ("_reach_scan_used", "_lock_scan_used"):
        if getattr(r, name, None) is not None:
            setattr(r, name, getattr(r, name) - back)

r = PinnedReach("sae", JASONS_ROOM)
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(r.scans == [["/usr/bin/nmcli", "dev", "wifi", "rescan", "ifname", "wlan0"]] and r.reads == 0
   and r._wpa2_blocked is None,
   "pinned: a scan is asked for and nothing is judged on the stale list")
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(len(r.scans) == 1 and r.reads == 0, "and the next poll, before it can land, neither scans again nor reads")
reach_later(r, m.Plugin._LOCK_SCAN_SETTLE_SECONDS + 3)
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(r.reads == 1 and r._wpa2_blocked == {"signal": 100, "current_signal": 52} and len(r.scans) == 1,
   "once the scan has landed it is read, and the finding made from it")
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(r.reads == 1, "then the usual five-minute throttle applies")
reach_later(r, m.Plugin._DUPLICATE_CHECK_SECONDS + 1)
r.aps = []
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(len(r.scans) == 2 and r.reads == 1 and r._wpa2_blocked is not None,
   "when due again it asks for a new scan and keeps its finding until that lands")
reach_later(r, m.Plugin._LOCK_SCAN_SETTLE_SECONDS + 3)
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(r.reads == 2 and r._wpa2_blocked is None, "and a fresh list without the radio clears it")

r = PinnedReach("wpa-psk", JASONS_ROOM)
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(not r.scans and r._wpa2_blocked is None, "a profile that allows WPA2 never costs a scan")
r = PinnedReach("sae", JASONS_ROOM, stream=True)
for _ in range(3):
    r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
    reach_later(r, m.Plugin._LOCK_SCAN_SETTLE_SECONDS + 3)
ok(not r.scans and r.reads == 0 and r._wpa2_blocked is None,
   "during a stream nothing is scanned and nothing judged")
r = PinnedReach("sae", JASONS_ROOM)
r._lock_scan_at = time.monotonic() - (m.Plugin._LOCK_SCAN_SETTLE_SECONDS + 3)
r._lock_scan_used = r._lock_scan_at
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, PIN)
ok(not r.scans and r.reads == 1 and r._wpa2_blocked is not None,
   "a scan the lock's own check asked for, and already read, is shared rather than repeated")
ok(r._lock_scan_used == r._lock_scan_at, "and reading it here does not use up the lock's read")
r = PinnedReach("sae", JASONS_ROOM)
r._recheck_security_reach(WU, "wlan0", "Home", ON, {}, "")
ok(not r.scans and r.reads == 1 and r._wpa2_blocked is not None,
   "with no pin the cached list is current and read at once, as before")
r._recheck_security_reach("other-uuid", "wlan0", "Home", ON, {}, PIN)
ok(r._wpa2_blocked is None, "a different profile does not inherit the finding while it waits for a scan")
_src = open("main.py").read()
ok('live.get("connected_bssid", ""),\n                                     settings, live.get("bssid_lock", ""))' in _src
   and 'self._report_network_problems(status, uuid, iface, active_ssid() or "", settings)' in _src,
   "the status poll hands the check the profile's pin and the settings")

class Allow(m.Plugin):
    """Profile security as NetworkManager holds it, and whether a reconnect takes."""
    def __init__(self, km="sae", connects=True, lock=False, found=True, **extra):
        super().__init__(); self.km = km; self.connects = connects
        self.writes = []; self.lock_calls = 0
        m._save_settings({"bssid_lock_enabled": lock, **extra})
        # What the status poll found, which is what the panel's button acts on.
        if found:
            self._wpa2_blocked = {"signal": 80, "current_signal": 40}
            self._reach_checked_for = WU
    def _resolve_uuid(self, msg=None): return WU, None
    def _get_profile_ssid(self, uuid, timeout=5): return "Home"
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

    a = Allow(found=False)
    res = asyncio.run(a.allow_wpa2())
    ok(not res.get("success") and res.get("error") == "not_needed" and not a.writes,
       "nor is one with no stronger WPA2-only access point found for it")
    a = Allow(); a._reach_checked_for = "aa0fd3f5-0000-0000-0000-000000000000"
    res = asyncio.run(a.allow_wpa2())
    ok(not res.get("success") and not a.writes,
       "a finding made for another profile does not count")
    _six = {"band_preference_enabled": True, "band_preference": "6GHz",
            "band_preference_ssid": "Home"}
    a = Allow(**_six)
    res = asyncio.run(a.allow_wpa2())
    ok(not res.get("success") and res.get("error") == "band_6ghz" and not a.writes,
       "held to 6 GHz on this network it refuses: 6 GHz is WPA3 only, and wpa-psk may not join it")
    a = Allow(**{**_six, "band_preference_ssid": "Office"})
    res = asyncio.run(a.allow_wpa2())
    ok(res.get("success") and a.writes, "6 GHz held for another network does not stop it here")

    class Wpa2Reach(m.Plugin):
        """The finding, from a cached scan of one network on two bands."""
        def _visible_access_points(self, iface, ssid, cached_only=False):
            return [("AA:00:00:00:00:02", 85, 5180, "WPA2"),
                    ("AA:00:00:00:00:01", 40, 6115, "WPA3")]
    # NetworkManager 1.58.1: a wpa-psk profile still reaches the WPA3-only
    # access point here, so the finding stands (J1 gates it below 1.58.1).
    _w2r = Wpa2Reach(); _w2r._nm_version_seen = (1, 58, 1)
    _find = getattr(_w2r, "_wpa2_finding", lambda *_a: "missing")
    ok(_find("wlan0", "Home", "aa:00:00:00:00:01", {}) == {"signal": 85, "current_signal": 40},
       "a much stronger WPA2-only access point is found")
    ok(_find("wlan0", "Home", "aa:00:00:00:00:01", _six) is None,
       "but not one on another band than the network is held to")
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

# The tracking lists have a cap. Cutting the oldest profile off one used to
# leave it carrying our change with nothing that knew to undo it.
_full = [f"aa0fd3f5-0000-0000-0000-0000000001{i:02d}" for i in range(m.Plugin._MAX_TRACKED_LOCK_UUIDS)]
m._save_settings({**base_ap, "ipv6_disabled": True, "ipv6_uuids": list(_full)})
v = V6(); asyncio.run(v.set_ipv6(True))
_tracked = m._load_settings()["ipv6_uuids"]
ok((_full[0], "ipv6.method", "auto") in v.mods and _full[0] not in _tracked
   and len(_tracked) == m.Plugin._MAX_TRACKED_LOCK_UUIDS and U6 in _tracked,
   "a profile that falls off a full tracking list has the change taken off it first")
class V6Stuck(V6):
    def _nmcli_modify(self, u, k, v, timeout=5):
        self.mods.append((u, k, v))
        if u == _full[0]:
            return {"success": False, "stdout": "", "stderr": "permission denied", "returncode": 1}
        return _ok()
m._save_settings({**base_ap, "ipv6_disabled": True, "ipv6_uuids": list(_full)})
v = V6Stuck(); asyncio.run(v.set_ipv6(True))
ok(_full[0] in m._load_settings()["ipv6_uuids"],
   "and one that will not take it off stays tracked, past the cap if need be")



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
        # The beta channel reads the list of releases, as GitHub's API gives it.
        return {"success":True,"stdout":json.dumps([{"tag_name":"v"+self.latest,"draft":False,
                                                     "prerelease":"-" in self.latest}]),
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
                   ("5825", "a"), ("5955", "6GHz"), ("6115 MHz", "6GHz"), ("5925", "6GHz"),
                   ("7125", "6GHz"), ("7130", ""), ("5850", ""), ("", ""), ("junk", "")]:
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
    ok(r.get("success") is True and "a few weren't accepted" in (r.get("message") or "")
       and "netdev_budget" not in (r.get("message") or ""),
       "a partly applied tuning says so, in words rather than sysctl names (L29)")
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

    # A state that already held our value had nothing recorded for it. It is
    # left alone on restore: mt7921e keeps its own ASPM off, and writing the
    # kernel's usual 1 turned on what the driver had turned off.
    m.SYSFS_NET, dev = pcie_device(OLED)
    for f in OLED:
        open(os.path.join(dev, "link", f), "w").write("0")
    if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)
    Pcie()._apply_pcie_aspm_fix(True)
    Pcie()._apply_pcie_aspm_fix(False)
    ok(all(open(os.path.join(dev, "link", f)).read() == "0" for f in OLED),
       "a state with nothing recorded is left as it was, not set to the kernel's usual value")
finally:
    m.SYSFS_NET = _real_sysfs
    if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)


section("driver parameters: turning the fix off puts back what the system had")
try:
    import builtins as _bi
    _mod_root = tempfile.mkdtemp()
    _LPS = "/sys/module/rtw88_core/parameters/disable_lps_deep"
    _ASPM = "/sys/module/rtw88_pci/parameters/disable_aspm"
    def _sysmod(path): return os.path.join(_mod_root, path[len("/sys/module/"):])
    def _param(path, val=None):
        if val is not None:
            os.makedirs(os.path.dirname(_sysmod(path)), exist_ok=True)
            _real_open_dp(_sysmod(path), "w").write(val)
        return _real_open_dp(_sysmod(path)).read()
    _real_open_dp = _bi.open
    def _open_dp(path, *a, **k):
        if isinstance(path, str) and path.startswith("/sys/module/"):
            path = _sysmod(path)
        return _real_open_dp(path, *a, **k)
    _conf_dir = tempfile.mkdtemp()
    _saved_dp = (m.MODPROBE_DIRS, m.PROC_CMDLINE, m.MODPROBE_CONF_PATH, m._boot_time,
                 m.SYSFS_NET)
    _net_dp = tempfile.mkdtemp(); os.makedirs(os.path.join(_net_dp, "wlan0"))
    def _operstate(v): open(os.path.join(_net_dp, "wlan0", "operstate"), "w").write(v + "\n")
    class Drv(m.Plugin):
        def _get_wifi_interface(self): return "wlan0"
    def _fresh(lps="N", aspm="N", conf=None, cmdline="", ours=None, ours_before_boot=False):
        for f in os.listdir(_conf_dir): os.remove(os.path.join(_conf_dir, f))
        if conf: open(os.path.join(_conf_dir, "50-guide.conf"), "w").write(conf)
        open(m.PROC_CMDLINE, "w").write(cmdline)
        if os.path.exists(m.MODPROBE_CONF_PATH): os.remove(m.MODPROBE_CONF_PATH)
        if ours: open(m.MODPROBE_CONF_PATH, "w").write(ours)
        _t = os.stat(m.MODPROBE_CONF_PATH).st_mtime if ours else time.time()
        m._boot_time = (lambda: int(_t) + 60) if ours_before_boot else (lambda: int(_t) - 60)
        if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)
        _param(_LPS, lps); _param(_ASPM, aspm); _operstate("up")
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88"})
    m.MODPROBE_DIRS = (_conf_dir,)
    m.PROC_CMDLINE = os.path.join(tempfile.mkdtemp(), "cmdline")
    m.MODPROBE_CONF_PATH = os.path.join(_conf_dir, "99-wifi-optimizer.conf")
    m.SYSFS_NET = _net_dp
    _bi.open = _open_dp
    try:
        _fresh()
        Drv()._apply_driver_fixes(True, persist=False)
        ok(_param(_LPS) == "Y" and _param(_ASPM) == "Y", "the fix writes Y")
        Drv()._apply_driver_fixes(False)
        ok(_param(_LPS) == "N" and _param(_ASPM) == "N", "and turning it off puts back the N the system had")

        _fresh(aspm="Y", conf="# from a guide\noptions rtw88_pci disable_aspm=Y\n")
        Drv()._apply_driver_fixes(True, persist=False)
        Drv()._apply_driver_fixes(False)
        ok(_param(_ASPM) == "Y" and _param(_LPS) == "N",
           "a disable_aspm=Y from the user's own modprobe file is not overwritten with N")
        _fresh(aspm="Y", cmdline="quiet rtw88_pci.disable_aspm=1 splash")
        Drv()._apply_driver_fixes(True, persist=False); Drv()._apply_driver_fixes(False)
        ok(_param(_ASPM) == "Y", "nor one from the kernel command line")
        _fresh(aspm="Y")
        Drv()._apply_driver_fixes(True, persist=False, record_all=True)
        Drv()._apply_driver_fixes(False)
        ok(_param(_ASPM) == "Y",
           "a Y already there when auto mode first applies the fix is the system's, and is put back")
        _fresh(lps="Y", aspm="Y", ours_before_boot=True,
               ours="options rtw88_core disable_lps_deep=Y\noptions rtw88_pci disable_aspm=Y\n")
        Drv()._apply_driver_fixes(True, persist=False, record_all=True)
        os.remove(m.MODPROBE_CONF_PATH)
        Drv()._apply_driver_fixes(False)
        ok(_param(_LPS) == "N" and _param(_ASPM) == "N",
           "but a Y our own modprobe file gave the driver at boot is ours, and comes off")

        _fresh(); _operstate("dormant")
        Drv()._apply_driver_fixes(True, persist=False)
        ok(_param(_ASPM) == "N" and _param(_LPS) == "Y",
           "rtw88 disable_aspm is not switched on while the radio is not associated")
        _operstate("up")
        Drv()._apply_driver_fixes(True, persist=False)
        ok(_param(_ASPM) == "Y", "and is once it is")

        ok([m._kernel_bool(v) for v in ("Y", "y", "1", "on", "true", "N", "0", "off", "no", "", None, "x")]
           == ["Y", "Y", "Y", "Y", "Y", "N", "N", "N", "N", None, None, None],
           "parameter values are read the way the kernel reads a boolean")
        ok(m._options_set("options rtw88_pci foo=1 disable_aspm\n", "rtw88_pci", "disable_aspm") == "Y"
           and m._options_set("options rtw88-pci disable_aspm=0 # was Y\n", "rtw88_pci", "disable_aspm") == "0"
           and m._options_set("#options rtw88_pci disable_aspm=Y\n", "rtw88_pci", "disable_aspm") is None,
           "and modprobe lines the way modprobe does (bare flag, dashes, comments)")
    finally:
        _bi.open = _real_open_dp
        (m.MODPROBE_DIRS, m.PROC_CMDLINE, m.MODPROBE_CONF_PATH, m._boot_time,
         m.SYSFS_NET) = _saved_dp
        if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)
        m._save_settings(dict(m.DEFAULT_SETTINGS))

    _tmd = open("defaults/dispatcher.sh.tmpl").read()
    _psd = _tmd[_tmd.index("# Apply power save"):_tmd.index("# Apply buffer tuning")]
    ok("python3 -I - " in _tmd and "python3 - " not in _tmd,
       "the dispatcher's baseline Python runs isolated too")
    ok(_psd.index("driver parameters before applying") < _psd.index("pcie-baseline")
       < _psd.index('echo Y 2>/dev/null > "$p"'),
       "the dispatcher logs and records the driver parameters before it writes them")
    _linkblk = _psd[_psd.index('if [ -d "$WIFI_PCI/link" ]'):]
    _linkblk = _linkblk[:_linkblk.index("\n    fi\n")]
    ok('if [ -f "$WIFI_PCI/power/control" ]' in _psd and "power/control" not in _linkblk,
       "and writes power/control whether or not the device has a link directory")
    # D9: the template also reads last_enforced earlier, so the write is what
    # has to come after the gate.
    ok('[ "$APPLIED" = 1 ] || exit 0' in _tmd
       and _tmd.index('[ "$APPLIED" = 1 ] || exit 0')
       < _tmd.index("date +%s > /run/wifi-optimizer/last_enforced"),
       "last_enforced is stamped only when something was applied")
    ok("SETTINGS_DIR" not in _tmd, "no unused SETTINGS_DIR")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")


section("tools are found where the distro keeps them, and 6 GHz is the card's own answer")
try:
    _tdirs = (tempfile.mkdtemp(), tempfile.mkdtemp())
    open(os.path.join(_tdirs[1], "iw"), "w").write("")
    m.TOOL_DIRS = _tdirs; m._TOOL_PATHS.clear()
    ok(m._tool(os.path.join(_tdirs[0], "iw")) == os.path.join(_tdirs[1], "iw"),
       "a tool missing from /usr/bin is run from /usr/sbin (Bazzite keeps iw, ip, tc and sysctl there)")
    ok(m._tool(os.path.join(_tdirs[0], "nothere")) == os.path.join(_tdirs[0], "nothere"),
       "and one found nowhere keeps the path asked for, so the error names it")
    ok(m._tool("/opt/x/iw") == "/opt/x/iw", "a path outside the system directories is never redirected")
    m.TOOL_DIRS = (); m._TOOL_PATHS.clear()
    _dsp = open("defaults/dispatcher.sh.tmpl").read()
    ok(not re.search(r"/usr/s?bin/(iw|ip|tc|sysctl|modprobe) ", _dsp),
       "the dispatcher finds them on its fixed PATH rather than by one distro's layout")

    _phy_net = tempfile.mkdtemp(); os.makedirs(os.path.join(_phy_net, "wlan0", "phy80211"))
    open(os.path.join(_phy_net, "wlan0", "phy80211", "name"), "w").write("phy0\n")
    _IW5 = ("Wiphy phy0\n\tBand 2:\n\t\tFrequencies:\n\t\t\t* 5180.0 MHz [36] (23.0 dBm)\n"
            "\t\t\t* 5885.0 MHz [177] (disabled)\n")
    _IW6 = _IW5 + "\tBand 4:\n\t\tFrequencies:\n\t\t\t* 5955.0 MHz [1] (disabled)\n"
    class Phy(m.Plugin):
        out = _IW5
        def _get_wifi_interface(self): return "wlan0"
        def _detect_wifi_driver(self): return "mt7921e"
        def _run_cmd(self, cmd, timeout=5):
            return _ok(self.out) if cmd[1:4] == ["phy", "phy0", "info"] else _ok("")
    _sn = m.SYSFS_NET; m.SYSFS_NET = _phy_net
    try:
        ok(asyncio.run(Phy().get_device_info())["supports_6ghz"] is False,
           "an MT7921 on the mt7921e driver, which lists no 6 GHz channel, is not offered 6 GHz")
        _p6 = Phy(); _p6.out = _IW6
        ok(asyncio.run(_p6.get_device_info())["supports_6ghz"] is True,
           "a card listing a 6 GHz channel is, even one the region has disabled")
        _pf = Phy(); _pf.out = ""
        # D16: mt7921e also drives the MT7921, so its fallback is False now.
        ok(asyncio.run(_pf.get_device_info())["supports_6ghz"]
           is m.DRIVER_PROFILES["mt7921e"]["supports_6ghz"],
           "and when the card cannot be asked the driver's profile decides")
    finally:
        m.SYSFS_NET = _sn
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")


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

m._save_settings({**m.DEFAULT_SETTINGS, "last_connection_uuid": A6, "pins_owned": []})
d = Dns(B6)
d._clear_profile_pins()
ok(not any(k == "802-11-wireless.band" for _, k, _v in d.mods),
   "reset and uninstall leave alone a band this plugin never pinned")
m._save_settings({**m.DEFAULT_SETTINGS, "last_connection_uuid": A6,
                  "pins_owned": ["802-11-wireless.band"]})
d = Dns(B6)
d._clear_profile_pins()
ok((A6, "802-11-wireless.band", "") in d.mods,
   "but clear one it did, even on a profile it lost track of")

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
    return await asyncio.gather(u.apply_update("0.19.0-beta"), u.apply_update("0.19.0-beta")), u
(r1, r2), u = asyncio.run(two_updates())
ok(u.checks == 1, "two overlapping update requests run one update")
class HeldUpdate(TwoUpdates):
    async def check_for_update(self):
        self.checks += 1
        await asyncio.sleep(0.05)
        return {"update_available": False}
async def refused_mid_check():
    u = HeldUpdate()
    first = asyncio.ensure_future(u.apply_update("0.19.0-beta"))
    await asyncio.sleep(0.01)
    await u.apply_update("0.19.0-beta")
    held = u._update_running()
    await first
    return held
ok(asyncio.run(refused_mid_check()) is True,
   "a refused request does not release the claim of the one still running")

# The fallback at the end of an update restarts plugin_loader, which restarts
# Steam, so an update is not started during a game or a stream.
_saved_proc = m.PROC_DIR
try:
    m.PROC_DIR = tempfile.mkdtemp()
    os.mkdir(os.path.join(m.PROC_DIR, "4242"))
    open(os.path.join(m.PROC_DIR, "4242", "cmdline"), "wb").write(
        b"/home/deck/.local/share/Steam/ubuntu12_32/reaper\0SteamLaunch\0AppId=1091500\0--\0game.exe\0")
    open(os.path.join(m.PROC_DIR, "4242", "comm"), "wb").write(b"reaper\n")
    u = TwoUpdates()
    r = asyncio.run(u.apply_update("0.19.0-beta"))
    ok(r.get("success") is False and "game" in r.get("message", "") and u.checks == 0
       and not u._update_running(),
       f"no update while a game runs, said why, and the claim is given back ({r.get('message')!r})")
    os.remove(os.path.join(m.PROC_DIR, "4242", "cmdline"))
    open(os.path.join(m.PROC_DIR, "4242", "cmdline"), "wb").write(
        b"/home/deck/.local/share/Steam/ubuntu12_32/steam\0-gamepadui\0")
    u = TwoUpdates()
    asyncio.run(u.apply_update("0.19.0-beta"))
    ok(u.checks == 1, "Steam on its own is not a game")
finally:
    m.PROC_DIR = _saved_proc
class StreamOn(TwoUpdates):
    def _stream_running(self, settings): return True
u = StreamOn()
r = asyncio.run(u.apply_update("0.19.0-beta"))
ok(r.get("success") is False and u.checks == 0 and not u._update_running(),
   "nor during a stream")
# Keep fixes on counts as a stream. Told to close a game or stream, someone
# running neither had no idea what was stopping the update.
_hold_was = os.path.exists(m.STREAM_HOLD_FILE)
open(m.STREAM_HOLD_FILE, "w").write("1\n")
try:
    u = TwoUpdates(); r = asyncio.run(u.apply_update("0.19.0-beta"))
    ok(r.get("success") is False and "Keep fixes on" in r.get("message", "") and u.checks == 0,
       f"an update held back by Keep fixes on says that is the reason ({r.get('message')!r})")
finally:
    if not _hold_was: os.remove(m.STREAM_HOLD_FILE)
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

# What went wrong is in the log. The panel shows these messages as they are,
# and an exception's text is no message for anyone.
class SwitchBoom(Switch):
    def __init__(self): super().__init__(); self._ensure_backend_switch_state()
    def _has_backend_tool(self): raise RuntimeError("Traceback /usr/lib/secret detail")
r = asyncio.run(SwitchBoom().start_backend_switch("wpa_supplicant"))
ok(r.get("accepted") is False and "secret" not in r.get("message", "") and r.get("message"),
   f"a backend switch that fails to start says so plainly ({r.get('message')!r})")
_real_conf_e = m.GENERIC_BACKEND_CONF
try:
    m.GENERIC_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "conf.d", "99-backend.conf")
    class WorkerBoom(m.Plugin):
        def __init__(self): super().__init__(); self._ensure_backend_switch_state()
        def _merged_nm_backend(self): raise RuntimeError("Traceback /usr/lib/secret detail")
    wb = WorkerBoom(); asyncio.run(wb._generic_backend_switch_worker("iwd"))
    _wm = wb._backend_switch["result"].get("message", "")
    ok(_wm and "secret" not in _wm, f"and so does one that fails part way ({_wm!r})")
finally:
    m.GENERIC_BACKEND_CONF = _real_conf_e
class RegBoom(m.Plugin):
    def _regdomain_info(self): raise RuntimeError("Traceback /usr/lib/secret detail")
ok("secret" not in json.dumps(asyncio.run(RegBoom().get_regdomain())),
   "reading the wireless region hands back no exception text")
class DiagBoom(m.Plugin):
    async def get_diagnostic_info(self): raise RuntimeError("Traceback /usr/lib/secret detail")
_dr = asyncio.run(DiagBoom().save_diagnostic_info())
ok(_dr.get("success") is False and "secret" not in json.dumps(_dr) and _dr.get("message"),
   "nor does saving the diagnostics")

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

# Turning a fix off saves the setting first. The dispatcher and the startup
# apply act on it, so a reconnect part way through the revert put back what
# was coming off, under a toggle that then read off.
class SeenOff(m.Plugin):
    """Records what the saved setting said when each revert step ran."""
    def __init__(self, key, iw_ok=True):
        super().__init__(); self.key = key; self.iw_ok = iw_ok; self.seen = []
    def _get_wifi_interface(self): return "wlan0"
    def _run_cmd(self, cmd, timeout=5):
        self.seen.append((cmd[0].rsplit("/", 1)[-1], m._load_settings()[self.key]))
        if cmd[:2] == ["/usr/bin/iw", "dev"] and not self.iw_ok:
            return {"success": False, "stdout": "", "stderr": "busy", "returncode": 1}
        return _ok()
    def _apply_driver_fixes(self, enable, persist=True, record_all=False):
        self.seen.append(("drivers", m._load_settings()[self.key]))
    def _apply_pcie_aspm_fix(self, enable, record_all=False):
        self.seen.append(("pcie", m._load_settings()[self.key])); return []
    def _valve_powersave(self): return "3"
_real_nm_off = m.NM_CONF_PATH
try:
    m.NM_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
    for key, call in [("power_save_disabled", lambda p: p.set_power_save(False)),
                      ("buffer_tuning_enabled", lambda p: p.set_buffer_tuning(False)),
                      ("cake_enabled", lambda p: p.set_cake(False))]:
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, key: True})
        so = SeenOff(key); r = asyncio.run(call(so))
        ok(r.get("success") is True and so.seen and all(v is False for _, v in so.seen)
           and m._load_settings()[key] is False,
           f"turning {key} off saves it off before anything is reverted ({so.seen})")
    m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True})
    open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
    r = asyncio.run(SeenOff("power_save_disabled", iw_ok=False).set_power_save(False))
    ok(r.get("success") is False and m._load_settings()["power_save_disabled"] is True
       and os.path.exists(m.NM_CONF_PATH),
       "and when the radio refuses, nothing came off, so the setting goes back to on")
finally:
    m.NM_CONF_PATH = _real_nm_off

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
    # The script's own directory, left behind by one killed outright, which
    # cannot tidy up. It used to block updates, switches and Reset for good.
    _state(None)
    _ur = m.Plugin(); _ur._update_dir = tempfile.mkdtemp()
    _ur._update_started_at = time.monotonic()
    ok(_ur._update_running() is True, "an update started a moment ago is running while its directory is there")
    _ur._update_started_at = time.monotonic() - m.UPDATE_STATE_MAX_AGE - 5
    ok(_ur._update_running() is False,
       "but a directory older than the script's time limit is one it could not remove, not an update")
    os.rmdir(_ur._update_dir)

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
            # The signature check has its own section; here it passes.
            async def _signed_release(self, version):
                return {"ok": True, "url": "https://example.invalid/u.tar.gz", "sha256": "0" * 64}
        _state("failed 0.19.0-beta\n")
        st = Starts(0); r = asyncio.run(st.apply_update("0.19.0-beta"))
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
       "the panel's subscriptions go when Decky dismounts the plugin")
    _dispose = _watch[_watch.index("export function disposeUpdateWatch"):]
    ok("stop()" not in _dispose and "disposed" not in _watch and "if (!timer)" in _dispose,
       "but a watch in progress carries on: the panel left on screen after a reload may be the one the user pressed")
    ok('progress.phase === "swapped" && progress.running_version !== target' in _watch
       and "shared().reloadAsked === version" in _watch,
       "the reload is asked for once per version, and only while the old version still answers")
    ok("checkUpdateOnLoad()" in _idx, "a copy that loads mid-update picks the watch up")
    _done = _idx[_idx.index('progress?.phase === "done"'):]
    ok(_done.index("setUpdateInfo(null)") < _done.index("runUpdateCheck(true)") < _done.index("} else if (progress)"),
       "a panel left open across the update stops offering the version it just installed")
    _fresh = _watch[_watch.index("export function watchUpdate"):_watch.index("export function subscribeUpdate")]
    ok("watchUpdate(version, true)" in _idx and "if (fresh)" in _fresh
       and "shared().reloadAsked = undefined" in _fresh,
       "the panel follows an update it started, and asks for the reload again even for a version installed earlier")
    ok("shared().announced = undefined" in _fresh,
       "and says it updated again too, for a version installed earlier in the same session")
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
        _above = tempfile.mkdtemp(); _vic2 = tempfile.mkdtemp()
        os.mkdir(os.path.join(_vic2, "WiFi Optimizer"))
        for i in range(15):
            open(os.path.join(_vic2, "WiFi Optimizer", f"{i}.log"), "w").write("x")
        os.symlink(_vic2, os.path.join(_above, "logs"))
        m.decky.DECKY_PLUGIN_LOG_DIR = os.path.join(_above, "logs", "WiFi Optimizer")
        LinkedLogs()._rotate_logs()
        ok(len(os.listdir(os.path.join(_vic2, "WiFi Optimizer"))) == 15,
           "nor a link further up the path")
        _own = tempfile.mkdtemp()
        for i in range(15):
            _lf = os.path.join(_own, f"{i:02d}.log"); open(_lf, "w").write("x")
            os.utime(_lf, (1700000000 + i, 1700000000 + i))
        open(os.path.join(_own, "notes.txt"), "w").write("x")
        m.decky.DECKY_PLUGIN_LOG_DIR = _own
        LinkedLogs()._rotate_logs()
        _left = sorted(os.listdir(_own))
        ok(_left == [f"{i:02d}.log" for i in range(5, 15)] + ["notes.txt"],
           f"a real log directory keeps the ten newest logs and nothing else is touched ({len(_left)} left)")
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
        # Signed, so the failure under test is the launch's, not the check's.
        async def _signed_release(self, version):
            return {"ok": True, "url": "https://example.invalid/u.tar.gz", "sha256": "0" * 64}
    _real_popen = m.subprocess.Popen
    _made = []
    _real_mkdtemp = m.tempfile.mkdtemp
    def _track_mkdtemp(*a, **k):
        d = _real_mkdtemp(*a, **k); _made.append(d); return d
    def _boom(*a, **k): raise OSError("no bash")
    try:
        m.subprocess.Popen = _boom; m.tempfile.mkdtemp = _track_mkdtemp
        pf = PopenFails()
        r = asyncio.run(pf.apply_update("9.9.9"))
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

section("Force 6 GHz: offered and allowed only where it can work, put back if it does not")
try:
    _six_dev = {"supports_6ghz": True, "distro_id": "steamos"}

    class Band6(Band):
        """The band setter on a 6 GHz capable device and NetworkManager."""
        def __init__(self, nm="nmcli tool, version 1.58.1", km="wpa-psk", pmf="0 (default)",
                     link="Connected to 02:00:00:00:00:66\n\tfreq: 6115", profile_band=None, **kw):
            super().__init__(link=link, **kw)
            self.nm = nm; self.km = km; self.pmf = pmf; self.versions_read = 0
            self.profile_band = profile_band   # what the profile holds; None = unreadable
            self.km_reads = 0
        bands_in_range = (5180, 6115)
        def _run_cmd(self, cmd, timeout=5):
            if cmd[:4] == ["/usr/bin/nmcli", "-t", "-f", "802-11-wireless.band"]:
                if self.profile_band is None:
                    return {"success": False, "stdout": "", "stderr": "", "returncode": 10}
                return {"success": True, "stdout": f"802-11-wireless.band:{self.profile_band}\n",
                        "stderr": "", "returncode": 0}
            if cmd == ["/usr/bin/nmcli", "--version"]:
                self.versions_read += 1
                if self.nm is None:
                    return {"success": False, "stdout": "", "stderr": "", "returncode": 1}
                return {"success": True, "stdout": self.nm, "stderr": "", "returncode": 0}
            return super()._run_cmd(cmd, timeout)
        def _profile_key_mgmt(self, uuid):
            self.km_reads += 1; return self.km
        def _read_profile_field(self, uuid, field, timeout=5):
            return self.pmf if field == "802-11-wireless-security.pmf" else ""
        def _backend_is_iwd(self): return False

    for _out, _want in [("nmcli tool, version 1.52.1-1.5", (1, 52, 1)),
                        ("nmcli tool, version 1.58.0", (1, 58, 0)),
                        ("nmcli tool, version 1.58-rc1", (1, 58, 0)),
                        ("something else", None)]:
        ok(Band6(nm=_out)._nm_version() == _want, f"{_out!r} reads as {_want}")
    d = Band6(nm=None)
    ok(d._nm_version() is None and d._nm_version() is None and d.versions_read == 2,
       "a failed read is not kept, so the next caller tries again")
    d = Band6()
    d._nm_version(); d._nm_version()
    ok(d.versions_read == 1, "a good read is kept: nmcli is asked once")

    for _v, _km, _pmf, _dev, _ok_, _what in [
        ("1.58.1", "wpa-psk", "0 (default)", _six_dev, True, "a WPA2-style profile on 1.58.1"),
        ("1.58.0", "sae", "0 (default)", _six_dev, True, "a WPA3 profile on 1.58.0"),
        ("1.58.0", "wpa-psk", "0 (default)", _six_dev, False,
         "a WPA2-style profile on 1.58.0, which would not reconnect on its own"),
        ("1.58.1", "wpa-psk", "1 (disable)", _six_dev, False, "management frame protection turned off"),
        ("1.58.1", "wpa-eap", "0 (default)", _six_dev, False, "an enterprise network"),
        ("1.58.1", "", "0 (default)", _six_dev, False, "an open network"),
        ("1.52.1-1.5", "sae", "0 (default)", _six_dev, False, "NetworkManager 1.52, as on SteamOS 3.8"),
        ("1.58.1", "sae", "0 (default)", {"supports_6ghz": False}, False, "a card without 6 GHz"),
    ]:
        m._save_settings({**m.DEFAULT_SETTINGS, **_dev})
        _r = Band6(nm=f"nmcli tool, version {_v}", km=_km, pmf=_pmf)._six_ghz_refusal(BU)
        ok((_r is None) == _ok_ and (_ok_ or (_r.get("success") is False and _r.get("message"))),
           f"{_what}: {'allowed' if _ok_ else 'refused, with a reason'} ({(_r or {}).get('message')!r})")
    m._save_settings({**m.DEFAULT_SETTINGS, **_six_dev})
    _r = Band6(nm="nmcli tool, version 1.52.1-1.5")._six_ghz_refusal(BU)
    ok("SteamOS 3.9" in _r.get("message", ""), "on SteamOS the refusal says which release brings it")

    _dev_ok = Band6()
    _dev_ok._nm_version()
    ok(_dev_ok._six_ghz_offered({"supports_6ghz": True}, {"live": {"wifi_backend": "wpa_supplicant"}}) is True,
       "offered with a 6 GHz card and NetworkManager 1.58")
    ok(_dev_ok._six_ghz_offered({"supports_6ghz": True}, {"live": {"wifi_backend": "iwd"}}) is False
       and _dev_ok._six_ghz_offered({"supports_6ghz": False}, {"live": {}}) is False,
       "not under iwd, which ignores the band, nor without the card")
    _old = Band6(nm="nmcli tool, version 1.52.1-1.5"); _old._nm_version()
    ok(_old._six_ghz_offered({"supports_6ghz": True}, {"live": {}}) is False
       and Band6()._six_ghz_offered({"supports_6ghz": True}, {"live": {}}) is False,
       "nor on 1.52, nor before the version has been read: the poll never forks for it")

    ok(m._clean_settings({"band_preference": "6GHz"})["band_preference"] == "6GHz"
       and m._clean_settings({"band_preference": "6ghz"})["band_preference"] == "a",
       "the settings file keeps 6GHz and nothing like it")

    asyncio.sleep = _no_sleep
    try:
        m._save_settings(band_settings(**_six_dev))
        d = Band6()
        res = asyncio.run(d.set_band_preference(True, "6GHz"))
        ok(res.get("success") is True and wrote(d, BU, "6GHz"), f"6 GHz is applied and checked ({res})")
        _st = m._load_settings()
        ok(_st.get("band_preference") == "6GHz" and _st.get("band_preference_enabled") is True,
           "and recorded")

        m._save_settings(band_settings(**_six_dev))
        d = Band6(nm="nmcli tool, version 1.58.0")
        res = asyncio.run(d.set_band_preference(True, "6GHz"))
        ok(res.get("success") is False and not d.mods and d.reconnects == 0,
           "a refusal writes nothing and leaves the radio alone")
        m._save_settings(band_settings(**_six_dev))
        d = Band6(); d.bands_in_range = (5180, 2462)
        res = asyncio.run(d.set_band_preference(True, "6GHz"))
        ok(res.get("success") is False and "6 GHz" in res.get("message", "") and not d.mods,
           f"nor with no 6 GHz access point of this network in range ({res.get('message')!r})")

        # The scan showed one, but the device came back on 5 GHz.
        m._save_settings(band_settings(band_preference_enabled=True, band_preference="a",
                                       band_preference_ssid="Home", band_preference_uuids=[BU],
                                       **_six_dev))
        d = Band6(link="Connected to 02:00:00:00:00:55\n\tfreq: 5180")
        res = asyncio.run(d.set_band_preference(True, "6GHz"))
        _st = m._load_settings()
        ok(res.get("success") is False and res.get("message", "").startswith("Couldn't connect on 6 GHz"),
           f"landing anywhere but 6 GHz is reported ({res.get('message')!r})")
        ok(d.mods[-1] == (BU, "802-11-wireless.band", "a") and d.reconnects == 2,
           "the band is written back to 5 GHz and the device reconnected")
        ok(_st.get("band_preference") == "a" and _st.get("band_preference_enabled") is True
           and _st.get("band_preference_uuids") == [BU],
           "and the settings say 5 GHz again")
        m._save_settings(band_settings(**_six_dev))
        d = Band6(landed=BOLD)
        res = asyncio.run(d.set_band_preference(True, "6GHz"))
        ok(res.get("success") is False and d.mods[-1] == (BU, "802-11-wireless.band", "")
           and m._load_settings().get("band_preference_enabled") is not True,
           "coming back on another profile is not 6 GHz either; with no band before, none is left")
        _real_wait = m.Plugin._ASSOCIATION_WAIT_SECONDS
        m.Plugin._ASSOCIATION_WAIT_SECONDS = 0
        try:
            m._save_settings(band_settings(bssid_lock_enabled=True, bssid_lock_connection_uuid=BU,
                                           **_six_dev))
            d = Band6(link="")
            res = asyncio.run(d.set_band_preference(True, "6GHz"))
        finally:
            m.Plugin._ASSOCIATION_WAIT_SECONDS = _real_wait
        ok(res.get("success") is False and (BU, "802-11-wireless.band", "") in d.mods
           and d.reconnects == 2 and m._load_settings().get("bssid_lock_enabled") is True,
           "no association at all is put back too, with the lock left on to re-pin")

        # Put back means the band the profile itself held, which the settings
        # need not agree with.
        for _held, _what in [("bg", "a band set outside the plugin"),
                             ("", "no band, on a copy the preference never reached")]:
            m._save_settings(band_settings(band_preference_enabled=True, band_preference="a",
                                           band_preference_ssid="Home", band_preference_uuids=[BU],
                                           **_six_dev))
            d = Band6(link="Connected to 02:00:00:00:00:55\n\tfreq: 5180", profile_band=_held)
            res = asyncio.run(d.set_band_preference(True, "6GHz"))
            ok(res.get("success") is False and d.mods[-1] == (BU, "802-11-wireless.band", _held),
               f"a failed 6 GHz change restores the profile's own band: {_what} ({d.mods[-1]})")
        m._save_settings(band_settings(bssid_lock_enabled=True, bssid_lock_connection_uuid=BU,
                                       **_six_dev))
        d = Band6(profile_band="bg", refuse={(BU, "802-11-wireless.bssid")})
        res = asyncio.run(d.set_band_preference(True, "6GHz"))
        ok(res.get("success") is False and d.mods[-1] == (BU, "802-11-wireless.band", "bg"),
           "and so does a failed lock release in the middle of one")

        # A preference set on another network keeps that scope, so changing
        # the band here would reassert it there.
        for _band in ("6GHz", "a"):
            m._save_settings(band_settings(band_preference_enabled=True,
                                           band_preference="bg" if _band == "a" else "a",
                                           band_preference_ssid="Office",
                                           band_preference_uuids=[BOLD], **_six_dev))
            d = Band6()
            res = asyncio.run(d.set_band_preference(True, _band))
            _st = m._load_settings()
            ok(res.get("success") is False and res.get("error") == "band_scoped_elsewhere"
               and "Office" in res.get("message", "") and not d.mods and d.reconnects == 0
               and _st.get("band_preference_ssid") == "Office",
               f"band {_band!r} is refused while the preference belongs to another network")
        m._save_settings(band_settings(band_preference_enabled=True, band_preference="a",
                                       band_preference_ssid="Office",
                                       band_preference_uuids=[BOLD], **_six_dev))
        d = Band6()
        res = asyncio.run(d.set_band_preference(False))
        ok(res.get("success") is True and m._load_settings().get("band_preference_enabled") is False,
           "while turning it off from there still works")
    finally:
        asyncio.sleep = _real_sleep

    # The drift reassertion writes 6GHz into whichever profile of the network
    # is in use, which the setter's check never saw.
    _act6 = {"kind": "band", "uuid": BU, "value": "6GHz", "observed": "a"}
    for _v, _km, _writes, _what in [
        ("1.58.0", "wpa-psk", False, "a WPA2-style copy on 1.58.0 is left alone"),
        ("1.58.1", "wpa-psk", True, "one that can join 6 GHz is reasserted"),
    ]:
        m._save_settings({**m.DEFAULT_SETTINGS, **_six_dev})
        _set6 = {**m._load_settings(), "band_preference_enabled": True,
                 "band_preference": "6GHz", "band_preference_ssid": "Home"}
        d = Band6(nm=f"nmcli tool, version {_v}", km=_km)
        _status = {"drift": {"band_preference": True}, "live": {}}
        d._apply_band(dict(_act6), _set6, _status, {})
        ok(wrote(d, BU, "6GHz") is _writes and _status["drift"].get("band_preference") is True,
           f"drift reassert of 6 GHz: {_what}, the drift still reported until it lands")
    d = Band6(nm="nmcli tool, version 1.58.0")
    for _ in range(3):
        d._apply_band(dict(_act6), _set6, {"drift": {}, "live": {}}, {})
    ok(d.km_reads == 1, f"and the check is asked once, not on every poll ({d.km_reads} reads)")

    _pref6 = {**m.DEFAULT_SETTINGS, "band_preference_enabled": True,
              "band_preference": "6GHz", "band_preference_ssid": "Net"}
    ok(m.Plugin()._band_conflicts(_pref6, "6115", "Net") is False
       and m.Plugin()._band_conflicts(_pref6, "5180", "Net") is True,
       "with 6 GHz preferred, a 6 GHz association is the one that does not conflict")
    ok(m.Plugin()._pin_rules_out("802-11-wireless.band", "6GHz", [("A", 5180, 70)]) is True
       and m.Plugin()._pin_rules_out("802-11-wireless.band", "6GHz", [("A", 5180, 70), ("B", 6115, 40)]) is False,
       "a 6 GHz pin is released when nothing on 6 GHz is in range, like the others")
    _aps6 = [("A", 80, 5180, "WPA2 WPA3"), ("B", 60, 6115, "WPA3")]
    ok([ap[0] for ap in m.Plugin()._lock_candidates(_aps6, "Net", _pref6, "")] == ["B"],
       "the lock only picks among 6 GHz access points then")

    _tsx = open("src/index.tsx").read(); _dist = open("dist/index.js").read()
    ok('backend.setBandPreference(val, s?.band_preference === "bg" ? "bg" : "a")' in _tsx,
       "the 5 GHz toggle never sends 6GHz, which stays recorded after 6 GHz is turned off")
    ok('label="Force 6 GHz band"' in _tsx and 'backend.setBandPreference(val, "6GHz")' in _tsx
       and "Force 6 GHz band" in _dist and "six_ghz_offered" in _dist,
       "the panel has the 6 GHz toggle, shown from the status flag, and the built bundle does too")
    ok('status["live"]["six_ghz_offered"] = self._six_ghz_offered(settings, status)' in open("main.py").read(),
       "the status poll reports whether to offer it")
except Exception as _e:
    import traceback; traceback.print_exc()
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
        os.remove(m.GENERIC_BACKEND_CONF)

        # The new backend is enabled, and checked, before the old one is
        # stopped or disabled. The other way round, a failed enable or a
        # reload in between left neither enabled: no WiFi from the next boot.
        class Takes(Outranked):
            fail = ()
            def _merged_nm_backend(self): return None
            def _effective_generic_backend(self): return "iwd"
            def _get_wifi_interface(self): return "wlan0"
            def _get_active_connection_uuid(self): return "u"
            def _run_cmd(self, cmd, timeout=5):
                self.cmds.append(cmd)
                self.marked = getattr(self, "marked", []) + [os.path.exists(m.BACKEND_SWITCH_MARKER)]
                if tuple(cmd[1:3]) in self.fail:
                    return {"success": False, "stdout": "", "stderr": "failed", "returncode": 1}
                return _ok()
        asyncio.sleep = _no_sleep
        try:
            tk = Takes(); asyncio.run(tk._generic_backend_switch_worker("iwd"))
            _c = [c[1:3] for c in tk.cmds]
            ok(tk._backend_switch["result"]["success"] is True
               and _c.index(["enable", "iwd"]) < _c.index(["stop", "wpa_supplicant"])
               and _c.index(["enable", "iwd"]) < _c.index(["disable", "wpa_supplicant"]),
               f"switching enables the new backend before stopping or disabling the old ({_c})")
            ok(all(tk.marked) and not os.path.exists(m.BACKEND_SWITCH_MARKER),
               "with a marker in /run while it works, gone once it is done")
            if os.path.exists(m.GENERIC_BACKEND_CONF): os.remove(m.GENERIC_BACKEND_CONF)
            tk = Takes(); tk.fail = (("enable", "iwd"),)
            asyncio.run(tk._generic_backend_switch_worker("iwd"))
            ok(tk._backend_switch["result"]["success"] is False
               and not any(c[1] in ("stop", "disable") for c in tk.cmds)
               and not os.path.exists(m.GENERIC_BACKEND_CONF),
               "a new backend that cannot be enabled stops the switch with nothing stopped or disabled")
            tk = Takes(); tk.fail = (("restart", "NetworkManager"),)
            asyncio.run(tk._generic_backend_switch_worker("iwd"))
            _c = [c[1:3] for c in tk.cmds]
            ok(tk._backend_switch["result"]["success"] is False
               and ["start", "wpa_supplicant"] in _c[_c.index(["restart", "NetworkManager"]):],
               "and when NetworkManager will not restart the previous backend is put back")
        finally:
            asyncio.sleep = _real_sleep2
        # A switch the plugin did not live to finish is settled at the next load.
        for effective, expect, label in [
            ("iwd", ["disable", "wpa_supplicant"], "one that took has the old backend left disabled"),
            (None, ["start", "wpa_supplicant"], "one that had not taken has the old backend put back"),
        ]:
            class Interrupted(Outranked):
                def _effective_generic_backend(self): return effective
            open(m.BACKEND_SWITCH_MARKER, "w").write("wpa_supplicant iwd\n")
            ip = Interrupted()
            getattr(ip, "_reconcile_backend_switch", lambda: None)()
            ok(expect in [c[1:3] for c in ip.cmds] and not os.path.exists(m.BACKEND_SWITCH_MARKER),
               f"a backend switch cut off part way is settled at the next load: {label}")
        ip = Interrupted(); getattr(ip, "_reconcile_backend_switch", lambda: None)()
        ok(not ip.cmds, "with no marker nothing is touched")
        if os.path.exists(m.GENERIC_BACKEND_CONF): os.remove(m.GENERIC_BACKEND_CONF)
    finally:
        m.GENERIC_BACKEND_CONF = _real_conf
    ok("self._heal_orphaned_iwd_conf()" in _main_src
       or "asyncio.to_thread(self._heal_orphaned_iwd_conf)" in _main_src, "_main runs that check")
    ok("self._reconcile_backend_switch" in _main_src, "and the one for an interrupted switch")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("startup: migrated settings first, nothing lost, nothing left running")
try:
    class Boot(m.Plugin):
        def __init__(self, driver="unknown", during=None):
            super().__init__(); self.calls = []; self._driver = driver; self._during = during
        async def get_device_info(self):
            return {"success": True, "driver": self._driver, "model": "Jupiter",
                    "device_family": "deck_lcd", "device_label": "Steam Deck LCD",
                    "chip_label": "unknown" if self._driver == "unknown" else "WiFi 5 (RTL8822CE)"}
        def _nm_version(self):
            if self._during:
                self._during()
            return "1.52.1"
        def _detect_distro(self): return {"id": "steamos", "name": "SteamOS"}
        def _acknowledge_update(self): pass
        def _rotate_logs(self, keep=10): pass
        def _run_cmd(self, cmd, timeout=5): return _ok()
        def _install_dispatcher(self): self.calls.append("install"); return True
        def _remove_dispatcher(self): self.calls.append("remove"); return True
        def _heal_orphaned_iwd_conf(self):
            self.calls.append(("heal", m._load_settings().get("settings_version"))); return False
        async def _unpin_if_iwd(self):
            self.calls.append(("unpin", m._load_settings().get("settings_version")))
        async def _startup_streaming(self, settings): pass
        def _get_wifi_interface(self): return None
        def _get_backend_method(self): return "none"
        async def _stranded_watch(self): return

    def _concurrent():
        cur = m._load_settings(); cur["dns_enabled"] = True; m._save_settings(cur)

    _real = m.SETTINGS_FILE
    try:
        m.SETTINGS_FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
        m._save_settings({**m.DEFAULT_SETTINGS, "settings_version": 0, "driver": "rtw88",
                          "chip_label": "WiFi 5 (RTL8822CE)", "bssid_lock_enabled": True,
                          "bssid_lock_connection_uuid": "U1"})
        b = Boot("unknown", _concurrent)
        async def _run_main(plugin):
            await plugin._main()
            await asyncio.sleep(0)
        asyncio.run(_run_main(b))
        st = m._load_settings()
        ok(st.get("driver") == "rtw88" and st.get("chip_label") == "WiFi 5 (RTL8822CE)",
           "a radio missing at load does not overwrite the driver already known")
        _v = m.Plugin._SETTINGS_VERSION
        ok(("unpin", _v) in b.calls and ("heal", _v) in b.calls,
           "the iwd unpin and the backend heal see the migrated settings")
        ok(st.get("dns_enabled") is True,
           "a setting saved while startup was waiting is not overwritten")
        m._save_settings({**m.DEFAULT_SETTINGS, "auto_fix_on_wake": False})
        b = Boot("rtw88"); asyncio.run(_run_main(b))
        ok("remove" in b.calls and "install" not in b.calls,
           "with auto-fix off, a script an older version left is removed")
        ok(m._load_settings().get("driver") == "rtw88", "and a detected driver is recorded")
    finally:
        m.SETTINGS_FILE = _real
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("settings that cannot be saved: startup carries on, and the panel is told")
try:
    # A link in the user's directories is refused, which is one real way a
    # save fails; a full or read-only disk is another.
    _real = m.SETTINGS_FILE
    _away = tempfile.mkdtemp(); _top = tempfile.mkdtemp()
    os.symlink(_away, os.path.join(_top, "linked"))
    class BootNoSave(Boot):
        async def _startup_streaming(self, settings): self.calls.append("streaming")
    try:
        m.SETTINGS_FILE = os.path.join(_top, "linked", "settings.json")
        b = BootNoSave("rtw88")
        asyncio.run(_run_main(b))
        ok("streaming" in b.calls,
           f"startup goes on past a settings save that fails, to the streaming pickup and the rest ({b.calls})")
        class Unsaved(m.Plugin):
            def _collect_status(self):
                return ({"success": True, "connected": False, "settings": {}, "live": {},
                         "drift": {}, "external": {}}, {}, [])
        ok(asyncio.run(Unsaved().get_status()).get("settings_unwritable") is True,
           "and the status says the settings could not be saved")
        class Ipv6NoSave(m.Plugin):
            def _resolve_uuid(self, msg=None): return "dddddddd-0000-0000-0000-0000000000e1", None
            def _nmcli_modify(self, u, k, v, timeout=5): return _ok()
            def _hard_reconnect(self, uuid=None): return True
        r = asyncio.run(Ipv6NoSave().set_ipv6(True))
        ok(r.get("success") is False and r.get("error") == "settings_unwritable"
           and "couldn't save" in r.get("message", "").lower(),
           f"a change made but not saved says so, rather than that something went wrong ({r.get('message')!r})")
    finally:
        m.SETTINGS_FILE = _real
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    ok(asyncio.run(Unsaved().get_status()).get("settings_unwritable") is False,
       "a save that lands clears it")

    # Something rewriting profiles or the backend is in progress: said on
    # every reply, the ones a busy poll hands back from cache included.
    pc = Unsaved()
    ok(asyncio.run(pc.get_status()).get("profile_change_in_flight") is False,
       "nothing in progress reads as nothing in progress")
    pc._profile_change_depth = 1
    ok(asyncio.run(pc.get_status()).get("profile_change_in_flight") is True,
       "a profile change in progress is reported")
    pc._profile_change_depth = 0; pc._ensure_backend_switch_state()
    pc._backend_switch["in_progress"] = True
    pc._collect_depth = 1; pc._collect_started_at = time.monotonic()
    ok(asyncio.run(pc.get_status()).get("profile_change_in_flight") is True,
       "and so is a backend switch, on a reply taken from the last poll")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("system config files are written whole, and teardown undoes power save and region now")
try:
    _d = tempfile.mkdtemp(); _f = os.path.join(_d, "conf.d", "99-x.conf")
    ok(m._write_system_conf(_f, "a=1\n") is True and open(_f).read() == "a=1\n",
       "a config file is written")
    _ino = os.stat(_f).st_ino
    ok(m._write_system_conf(_f, "a=1\n") is False and os.stat(_f).st_ino == _ino,
       "and left untouched when it already says the same")
    ok(m._write_system_conf(_f, "a=2\n") is True and open(_f).read() == "a=2\n"
       and os.listdir(os.path.dirname(_f)) == ["99-x.conf"],
       "a change replaces it whole, leaving no temp file behind")

    class Tear(m.Plugin):
        valve = None
        def __init__(self): super().__init__(); self.cmds = []
        def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
        def _get_wifi_interface(self): return "wlan0"
        def _valve_powersave(self): return self.valve
        def _release_valve_powersave(self, settings): pass
        def _apply_driver_fixes(self, enable, persist=True, record_all=False): pass
        def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
    _rn, _rm, _rs = m.NM_CONF_PATH, m.MODPROBE_CONF_PATH, m.SETTINGS_FILE
    try:
        m.NM_CONF_PATH = os.path.join(_d, "99-wifi-optimizer.conf")
        m.MODPROBE_CONF_PATH = os.path.join(_d, "99-modprobe.conf")
        m.SETTINGS_FILE = os.path.join(_d, "settings.json")
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        open(m.MODPROBE_CONF_PATH, "w").write("options rtw88_pci disable_aspm=Y\n")
        t = Tear(); t._release_power_save()
        ok(not os.path.exists(m.NM_CONF_PATH) and not os.path.exists(m.MODPROBE_CONF_PATH)
           and ["/usr/bin/nmcli", "general", "reload", "conf"] in t.cmds
           and ["/usr/bin/iw", "dev", "wlan0", "set", "power_save", "on"] in t.cmds,
           "Reset and uninstall have NetworkManager drop our file now and turn power save back on")
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        t = Tear(); t.valve = "2"; t._release_power_save()
        ok(not any(c[:2] == ["/usr/bin/iw", "dev"] for c in t.cmds),
           "but leave it off when Steam's own setting has it off")
        # Valve's line before NetworkManager rereads its config: the other
        # way round it read our 2 still there and kept power save off.
        class TearValve(Tear):
            def _release_valve_powersave(self, settings): self.cmds.append(["valve released"])
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        t = TearValve(); t._release_power_save()
        _reload = ["/usr/bin/nmcli", "general", "reload", "conf"]
        ok(["valve released"] in t.cmds and _reload in t.cmds
           and t.cmds.index(["valve released"]) < t.cmds.index(_reload),
           f"Valve's power save line is put back before NetworkManager rereads its config ({t.cmds[:3]})")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": False})
        t = Tear(); t._release_power_save()
        ok(not any(c[-2:] == ["power_save", "on"] for c in t.cmds),
           "the radio's power save is only turned on where this plugin had it off")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        # One step that fails does not leave the rest undone.
        os.mkdir(m.NM_CONF_PATH); open(os.path.join(m.NM_CONF_PATH, "x"), "w").close()
        open(m.MODPROBE_CONF_PATH, "w").write("options rtw88_pci disable_aspm=Y\n")
        t = Tear()
        try:
            t._release_power_save(); _raised = None
        except Exception as _e2:
            _raised = _e2
        ok(_raised is None and not os.path.exists(m.MODPROBE_CONF_PATH)
           and ["/usr/bin/iw", "dev", "wlan0", "set", "power_save", "on"] in t.cmds,
           f"a file that cannot be removed does not stop the rest being undone ({_raised!r})")
        os.remove(os.path.join(m.NM_CONF_PATH, "x")); os.rmdir(m.NM_CONF_PATH)
        m._save_settings({**m.DEFAULT_SETTINGS, "regdomain": "DE", "regdomain_previous": "US"})
        t = Tear(); t._release_regdomain()
        ok(["/usr/bin/iw", "reg", "set", "US"] in t.cmds, "a region chosen here is put back to the one before it")
        m._save_settings({**m.DEFAULT_SETTINGS, "regdomain": ""})
        t = Tear(); t._release_regdomain()
        ok(t.cmds == [], "and nothing is touched when no region was chosen here")
    finally:
        m.NM_CONF_PATH, m.MODPROBE_CONF_PATH, m.SETTINGS_FILE = _rn, _rm, _rs
    _tm3 = open("defaults/dispatcher.sh.tmpl").read()
    ok("/run/wifi-optimizer/pcie-baseline" in _tm3 and 'PCIE_BASELINE_FILE = "/run/wifi-optimizer/pcie-baseline"' in open("main.py").read()
       and _tm3.index("pcie-baseline") < _tm3.index('echo 0 2>/dev/null > "$WIFI_PCI/link/$f"')
       and "O_EXCL" in _tm3,
       "the dispatcher notes the system's PCIe values once a boot, before its first write")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("the plugin log carries no network names and only part of each address")
try:
    import logging as _lg, inspect as _insp2
    ok(m._mask_addresses("Locking to D8:07:B6:90:C7:D1 rather than 62:e9:31:0d:1f:e7")
       == "Locking to xx:xx:xx:xx:C7:D1 rather than xx:xx:xx:xx:1f:e7",
       "an access point's address keeps only its last two octets")
    _lgr = _lg.getLogger("wifiopt-test"); _cap = []
    class _Cap(_lg.Handler):
        def emit(self, record): _cap.append(record.getMessage())
    _lgr.addHandler(_Cap()); _lgr.addFilter(m._MaskAddresses()); _lgr.propagate = False
    _lgr.warning("moved from %s", "D8:07:B6:90:C7:D1")
    ok(_cap == ["moved from xx:xx:xx:xx:C7:D1"], "applied to every message the logger handles, arguments included")
    import re as _re2
    _bad = []
    for _mm in _re2.finditer(r"(decky\.logger\.(?:info|error|warning)|self\._log_throttled)\(", _src_all):
        _i = _mm.end(); _dp = 1; _j = _i
        while _dp and _j < len(_src_all):
            _c = _src_all[_j]; _dp += _c == "("; _dp -= _c == ")"; _j += 1
        if _re2.search(r"\{[^}]*(?<![bB])(ssid|scoped_to|adopted)\b[^}]*\}|_get_profile_ssid", _src_all[_i:_j]):
            _bad.append(_src_all.count("\n", 0, _mm.start()) + 1)
    ok(_bad == [], f"no log call writes a network name (found at lines {_bad})")
    _t = open("defaults/dispatcher.sh.tmpl").read()
    ok(_t.index("export PATH=/usr/bin:/bin:/usr/sbin:/sbin") < _t.index("python3"),
       "the root dispatcher finds its tools on a fixed PATH")
    _cs = m._clean_settings({**m.DEFAULT_SETTINGS, "ap_move_failures": {
        "D8:07:B6:90:C7:D1": 1792603383, "62:E9:31:0D:1F:E7": "yesterday", "not-a-mac": 5,
        "AA:BB:CC:DD:EE:FF": True}})
    ok(_cs["ap_move_failures"] == {"D8:07:B6:90:C7:D1": 1792603383},
       "a hand-edited access point failure that is not an address and a time is dropped on load")
    _setters = {"set_power_save": "disabled", "set_auto_fix": "enabled", "set_bssid_lock": "enabled",
                "set_band_preference": "enabled", "set_dns": "enabled", "set_ipv6": "disabled",
                "set_buffer_tuning": "enabled", "set_cake": "enabled", "set_streaming_mode": "enabled",
                "set_streaming_hold": "held", "set_streaming_app": "watched", "set_update_notify": "enabled"}
    _missing = [n for n, p in _setters.items()
                if f"{p} = {p} is True" not in _insp2.getsource(getattr(m.Plugin, n))]
    ok(_missing == [], f"every on/off setter takes only a real True as on (missing: {_missing})")
    ok('tag[1:] if isinstance(tag, str) and tag.startswith("v")' in _src_all,
       "a release tag loses one leading v, not every one")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Reset and uninstall do not strand WiFi on a backend switched from here")
try:
    import inspect as _insp
    _rc, _rb = m.GENERIC_BACKEND_CONF, m.BAZZITE_IWD_CONF
    class Release(m.Plugin):
        merged = None
        def __init__(self): super().__init__(); self.cmds = []
        def _merged_nm_backend(self, *a, **k): return self.merged
        def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
    try:
        _d = tempfile.mkdtemp()
        m.GENERIC_BACKEND_CONF = os.path.join(_d, "99-wifi-optimizer-backend.conf")
        m.BAZZITE_IWD_CONF = os.path.join(_d, "iwd.conf")
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\nwifi.iwd.autoconnect=yes\n")
        r = Release(); r._release_generic_backend_conf()
        ok(not os.path.exists(m.GENERIC_BACKEND_CONF)
           and ["/usr/bin/systemctl", "enable", "--no-reload", "wpa_supplicant"] in r.cmds
           and ["/usr/bin/systemctl", "disable", "--no-reload", "iwd"] in r.cmds,
           "removing our iwd setting turns wpa_supplicant back on for the next boot and iwd off")
        ok(not any(c[1] in ("stop", "restart", "start") or "--now" in c for c in r.cmds),
           "without stopping or restarting anything, so the connection in use carries on")
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        open(m.BAZZITE_IWD_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        r = Release(); r._release_generic_backend_conf()
        ok(r.cmds == [] and not os.path.exists(m.GENERIC_BACKEND_CONF),
           "when the system's own setting names the same backend, no service is touched")
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=wpa_supplicant\n")
        r = Release(); r._release_generic_backend_conf()
        ok(["/usr/bin/systemctl", "enable", "--no-reload", "iwd"] in r.cmds
           and ["/usr/bin/systemctl", "disable", "--no-reload", "wpa_supplicant"] in r.cmds,
           "and when it names the other one, that one is turned back on")
        os.remove(m.BAZZITE_IWD_CONF)
        r = Release(); r.merged = "wpa_supplicant"; r._release_generic_backend_conf()
        ok(r.cmds == [], "with no file of ours there is nothing to do")
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=wpa_supplicant\n")
        open(m.BAZZITE_IWD_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        ok(Release()._configured_backend_from_files() == "iwd",
           "Bazzite's iwd.conf outranks ours, the order NetworkManager reads them in")
        os.remove(m.BAZZITE_IWD_CONF)
        # The backend left in charge is enabled while our file still names
        # the other, and the file kept if that fails: killed or refused in
        # between, the other order left a boot with no WiFi.
        class ReleaseOrder(Release):
            enable_ok = True
            def _run_cmd(self, cmd, timeout=5):
                self.cmds.append((cmd[1:], os.path.exists(m.GENERIC_BACKEND_CONF)))
                if cmd[1] == "enable" and not self.enable_ok:
                    return {"success": False, "stdout": "", "stderr": "no", "returncode": 1}
                return _ok()
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        ro = ReleaseOrder(); ro._release_generic_backend_conf()
        ok(ro.cmds == [(["enable", "--no-reload", "wpa_supplicant"], True),
                       (["disable", "--no-reload", "iwd"], False)]
           and not os.path.exists(m.GENERIC_BACKEND_CONF),
           f"wpa_supplicant is enabled before our iwd setting goes, iwd disabled after ({ro.cmds})")
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        ro = ReleaseOrder(); ro.enable_ok = False; ro._release_generic_backend_conf()
        ok(os.path.exists(m.GENERIC_BACKEND_CONF) and not any(c[0][0] == "disable" for c in ro.cmds),
           "and when it cannot be enabled our setting stays, with nothing disabled")
        ok(not [f for f in os.listdir(_d) if "aside" in f],
           "asking NetworkManager what it falls back to leaves nothing set aside")
        os.remove(m.GENERIC_BACKEND_CONF)
    finally:
        m.GENERIC_BACKEND_CONF, m.BAZZITE_IWD_CONF = _rc, _rb
    _uninst_src = _insp.getsource(m.Plugin._uninstall) + _insp.getsource(
        getattr(m.Plugin, "_teardown", m.Plugin._uninstall))
    _reset_src = _insp.getsource(m.Plugin.reset_settings) + _insp.getsource(
        getattr(m.Plugin, "_reset_locked", m.Plugin.reset_settings))
    ok("self._release_generic_backend_conf" in _uninst_src
       and "self._release_generic_backend_conf" in _reset_src
       and "GENERIC_BACKEND_CONF" not in _uninst_src,
       "uninstall and Reset both go through that, and neither deletes the file directly")
    _tm2 = open("defaults/dispatcher.sh.tmpl").read()
    _gone = _tm2[_tm2.index('if [ ! -d "$PLUGIN_DIR" ]'):]
    _gone = _gone[:_gone.index("\nfi\n")]
    ok("99-wifi-optimizer-backend.conf" not in _gone,
       "the dispatcher's own clean-up leaves the backend setting in place")
    ok(_gone.index("sleep") < _gone.index("rm -f"),
       "and looks again before removing anything, since an install swaps the directory briefly")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("uninstall: what lasts longest first, inside Decky's five seconds")
try:
    import threading as _thr4
    U1, U2 = "eeeeeeee-0000-0000-0000-0000000000b1", "eeeeeeee-0000-0000-0000-0000000000b2"
    class Steps(m.Plugin):
        """Each teardown step recorded, in order, and where it ran."""
        def __init__(self):
            super().__init__(); self.order = []; self.threads = []
        def _note(self, name):
            self.order.append(name); self.threads.append(_thr4.current_thread() is _thr4.main_thread())
        def _release_generic_backend_conf(self): self._note("backend")
        def _stop_update(self): self._note("update")
        def _release_power_save_files(self, settings, reload=True): self._note("power save files"); return True
        def _clear_profile_pins(self, deadline=None, settings=None):
            self._note("pins"); return {"bssid_lock_uuids": []}
        def _remove_dispatcher(self): self._note("dispatcher"); return True
        def _release_power_save(self, settings=None, reload_conf=False): self._note("power save")
        def _release_regdomain(self, settings=None): self._note("region")
        def _revert_queue_tuning(self, settings): self._note("queue")
    st = Steps(); asyncio.run(st._uninstall())
    _o = st.order
    ok(_o[:1] == ["backend"] and "power save files" in _o and "pins" in _o and "dispatcher" in _o
       and _o.index("power save files") < _o.index("pins") < _o.index("dispatcher"),
       f"the backend setting goes first, then the files that outlive a reboot and the pins, "
       f"then the dispatcher that would clean up after them ({_o})")
    ok(st.threads and not any(st.threads), "and all of it runs off the event loop")

    # A NetworkManager that answers slowly: the pins it does not reach stay
    # recorded for a reinstall, and the rest still happens in time.
    class SlowNm(m.Plugin):
        def __init__(self): super().__init__(); self.cmds = []
        def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
        def _nmcli_modify(self, uuid, key, value, timeout=5):
            time.sleep(min(timeout, 0.6)); return _ok()
        def _get_wifi_interface(self): return None
        def _apply_driver_fixes(self, enable, persist=True, record_all=False): pass
        def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
        def _remove_dispatcher(self): return True
        def _release_generic_backend_conf(self): pass
    _many = [f"eeeeeeee-0000-0000-0000-0000000002{i:02d}" for i in range(12)]
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_uuids": _many,
                      "pins_owned": ["802-11-wireless.bssid"]})
    open(m.UPDATE_LOCK_FILE, "w").close()
    sn = SlowNm(); _t = time.monotonic(); asyncio.run(sn._uninstall()); _took = time.monotonic() - _t
    _kept = m._load_settings()
    ok(_took < m.Plugin._UNINSTALL_BUDGET + 1 if hasattr(m.Plugin, "_UNINSTALL_BUDGET") else False,
       f"uninstall finishes inside Decky's budget however slow nmcli is ({_took:.1f}s)")
    ok(os.path.exists(m.SETTINGS_FILE) and 0 < len(_kept["bssid_lock_uuids"]) < 12
       and _kept["pins_owned"] == ["802-11-wireless.bssid"],
       "pins it ran out of time for stay in a settings file a reinstall can work from")
    ok(not os.path.exists(m.UPDATE_LOCK_FILE), "the update script's lock file goes too")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    asyncio.run(SlowNm()._uninstall())
    ok(not os.path.exists(m.SETTINGS_FILE), "with every pin off, the settings file is removed as before")

    # An update still running would put the plugin's files back.
    _real_sr2 = m.SYSTEMD_RUN
    try:
        m.SYSTEMD_RUN = os.path.join(tempfile.mkdtemp(), "systemd-run"); open(m.SYSTEMD_RUN, "w").close()
        su = SlowNm(); su._update_dir = tempfile.mkdtemp()
        asyncio.run(su._uninstall())
        ok(["/usr/bin/systemctl", "stop", "--no-block", "wifi-optimizer-update-*.service"] in su.cmds,
           "uninstall stops an update that is still running")
        su = SlowNm(); asyncio.run(su._uninstall())
        ok(not any("wifi-optimizer-update-*.service" in c for c in su.cmds),
           "and leaves systemd alone when none is")
    finally:
        m.SYSTEMD_RUN = _real_sr2
except Exception as _e:
    import traceback; traceback.print_exc()
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
                def _apply_driver_fixes(self, d, persist=True, record_all=False): pass
                def _apply_pcie_aspm_fix(self, d, record_all=False): return []
            open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=3\n[device]\nwifi.backend=wpa_supplicant\n")
            m._save_settings(dict(m.DEFAULT_SETTINGS))
            asyncio.run(PS().set_power_save(True))
            ok(m._load_settings()["valve_powersave_before"] == "3",
               "turning power save off records Valve's value first")
            # A backend switch then copies our 2 into Valve's file...
            open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=2\n[device]\nwifi.backend=iwd\n")
            asyncio.run(PS().set_power_save(False))
            ok(vp._valve_powersave() == "3" and m._load_settings()["valve_powersave_before"] == "3",
               "and giving it back restores that value, recorded as the user's own (L3)")
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
    for name, body in (("reset_settings", _reset_src), ("_uninstall", _uninst_src)):
        ok("self._release_power_save" in body, f"{name} gives Valve's power save setting back")
    _rel = _src_all[_src_all.index("    def _release_power_save(self"):]
    _rel = _rel[:_rel.index("\n    def _release_regdomain", 10)]
    ok("self._release_valve_powersave(" in _rel and "self._reload_nm_conf()" in _rel
       and '"power_save", "on"' in _rel,
       "which also has NetworkManager drop our file now and turns the radio's power save back on")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("the backend is read the way NetworkManager reads it")
try:
    _nmd = tempfile.mkdtemp()
    open(os.path.join(_nmd, "10-x.conf"), "w").write("[device]\nwifi.backend=wpa_supplicant\n")
    class Merged(m.Plugin):
        _NM_CONF_SOURCES = (_nmd,)
        asked = 0
        def _merged_nm_backend(self):
            Merged.asked += 1; return "iwd"
        def _configured_backend_from_files(self, *a, **k): return "wpa_supplicant"
    _mg = Merged()
    ok(_mg._get_current_backend() == "iwd",
       "NetworkManager's merged configuration outranks the files this plugin knows about")
    _mg._get_current_backend(); _mg._get_current_backend()
    ok(Merged.asked == 1, "and NetworkManager is asked once, not on every status poll")
    open(os.path.join(_nmd, "50-user.conf"), "w").write("[device]\nwifi.backend=iwd\n")
    _mg._get_current_backend()
    ok(Merged.asked == 2, "until a configuration file appears or changes")
    class NoMerged(Merged):
        def _merged_nm_backend(self): return None
    ok(NoMerged()._get_current_backend() == "wpa_supplicant",
       "and the known files answer when NetworkManager cannot be asked")
    _units = tempfile.mkdtemp(); _iwd = os.path.join(_units, "iwd.service")
    class Units(m.Plugin):
        _IWD_UNITS = (os.path.join(_units, "nothere.service"), _iwd)
    m._save_settings({**m.DEFAULT_SETTINGS, "distro_id": "cachyos"})
    ok(Units()._get_backend_method() == "none", "no iwd unit anywhere: no backend switch")
    open(_iwd, "w").write("")
    ok(Units()._get_backend_method() == "generic" and Units()._get_backend_method_cached() is True,
       "an iwd unit in any of systemd's unit directories counts, the same list everywhere")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("NetworkManager reads our power save file as soon as it is written")
try:
    _rn_nm = m.NM_CONF_PATH
    m.NM_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
    class Reloads(m.Plugin):
        def __init__(self): super().__init__(); self.reloads = 0
        def _get_wifi_interface(self): return None
        def _apply_driver_fixes(self, *a, **k): pass
        def _apply_pcie_aspm_fix(self, *a, **k): return []
        def _valve_powersave(self): return "3"
        def _reload_nm_conf(self): self.reloads += 1
    try:
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": False})
        _rl = Reloads(); asyncio.run(_rl.set_power_save(True))
        ok(_rl.reloads == 1, "writing it has NetworkManager read its config again")
        _rl = Reloads(); asyncio.run(_rl.set_power_save(True))
        ok(_rl.reloads == 0, "and an unchanged file does not")
    finally:
        m.NM_CONF_PATH = _rn_nm
        m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("a system config file that is not text is replaced, not a dead end")
try:
    _bin = os.path.join(tempfile.mkdtemp(), "99-x.conf")
    open(_bin, "wb").write(b"\xff\xfe\x00garbage")
    ok(m._write_system_conf(_bin, "[connection]\n") is True and open(_bin).read() == "[connection]\n",
       "a file of undecodable bytes at the name is written over")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("the panel is told plainly what holds where")
try:
    _st = {"drift": {}, "external": {}, "live": {}}
    m.Plugin()._propose_band({**m.DEFAULT_SETTINGS, "band_preference_enabled": True,
                              "band_preference_ssid": "Home"}, A6, "", lambda: "Cafe", _st, [])
    ok(_st["live"].get("band_other_network") is True and not _st["drift"],
       "a band preference set on another network is reported as such, not as drift")
    _st = {"drift": {}, "external": {}, "live": {}}
    m.Plugin()._propose_band({**m.DEFAULT_SETTINGS, "band_preference_enabled": True,
                              "band_preference_ssid": "Home"}, A6, "a", lambda: None, _st, [])
    ok("band_other_network" not in _st["live"], "but not when the network in use could not be read")
    _st = {"drift": {}, "external": {}, "live": {}}
    m.Plugin()._check_dns_drift({**m.DEFAULT_SETTINGS, "dns_enabled": True, "dns_uuids": [A6]}, B6, _st)
    ok(_st["live"].get("dns_other_network") is True, "and so is custom DNS set on another network")

    class Rivals(m.Plugin):
        prio = None
        def _profile_key_mgmt(self, uuid): return "sae" if uuid == A6 else "wpa-psk"
        def _rival_profiles(self, uuid, ssid): return [(B6, "wpa-psk")]
        def _profile_pins(self, uuid): return {}
        def _precedence_priority(self, uuid, rivals=None): return self.prio
    _rv = Rivals(); _rv.prio = 5; _acts = []
    _rv._recheck_rival_profiles({**m.DEFAULT_SETTINGS, "priority_set": False}, A6, lambda: "Home", _acts)
    ok(_rv._dup_conflict and _rv._dup_outranks is False,
       "duplicates still tied are not claimed as prioritized")
    _rv._nmcli_modify = lambda *a, **k: _ok()
    _rv._reassert_exhausted = lambda kind: False
    _rv._apply_priority({"kind": "priority", "uuid": A6, "value": 5},
                        {**m.DEFAULT_SETTINGS, "priority_set": False}, _st, {})
    ok(_rv._dup_outranks is True, "and are once the priority has been written")
    _rv2 = Rivals(); _rv2.prio = None
    _rv2._recheck_rival_profiles({**m.DEFAULT_SETTINGS, "priority_set": True}, A6, lambda: "Home", [])
    ok(_rv2._dup_outranks is True, "or when the copy in use already outranks the rest")

    class DnsCheck(Dns):
        pass
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    for _bad in ("2606:4700::1111", "1.1.1", "1.1.1.256", "01.1.1.1x"):
        _r = asyncio.run(DnsCheck(A6).set_dns(True, "custom", _bad))
        ok(_r.get("success") is False and _r.get("error") == "invalid_dns"
           and "IPv4" in (_r.get("message") or ""),
           f"custom DNS {_bad!r} is refused before nmcli sees it, saying what is accepted")
    _r = asyncio.run(DnsCheck(A6).set_dns(True, "custom", "1.1.1.1, 9.9.9.9"))
    ok(_r.get("success") is True, "while IPv4 addresses, commas or not, are taken")

    _src_all_now = open("main.py").read()
    _msgs = re.findall(r'"message": f?"([^"]*)"', _src_all_now)
    # Messages built over several lines are caught by their phrases.
    _msgs += re.findall(r"Decky log|Reboot required", _src_all_now)
    _jargon = [t for t in _msgs if re.search(r"BSSID|UUID|wlan0|Decky log|Reboot required|qdisc|nmcli", t)]
    ok(not _jargon, f"no message the panel shows uses jargon ({_jargon})")
    _logged = []
    _real_err = m.decky.logger.error
    m.decky.logger.error = staticmethod(lambda msg: _logged.append(msg))
    try:
        class NoBand(Band):
            def _nmcli_modify(self, uuid, key, value, timeout=5):
                if key == "802-11-wireless.band" and value:
                    return {"success": False, "stdout": "", "stderr": "Error: nope here", "returncode": 1}
                return super()._nmcli_modify(uuid, key, value, timeout)
        m._save_settings(band_settings())
        _nb = NoBand(); _nb.bands_in_range = (5180,)
        _r = asyncio.run(_nb.set_band_preference(True, "a"))
    finally:
        m.decky.logger.error = _real_err
    ok(_r.get("success") is False and any("nope here" in l for l in _logged),
       f"the command output the panel no longer shows is logged instead ({_r.get('message')!r})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("SteamOS after 3.9: Valve's holo names")
try:
    ok(m._HOLO_BACKEND_HELPER == "/usr/bin/holo-polkit-helpers/holo-wifi-set-backend-privileged",
       "the renamed backend helper is known")
    ok("HOLO_NM_DEFAULT_CONF," in _src_all[_src_all.index("def _configured_backend_from_files"):][:1500],
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

    # DNS and IPv6 write the profile and reconnect just as the lock and the
    # band do, and a backend switch restarts NetworkManager under all four.
    import threading as _thr
    class DnsGuard(Dns):
        def __init__(self, active):
            super().__init__(active); self.depth_seen = []; self.threads = []
        def _hard_reconnect(self, uuid=None):
            self.depth_seen.append(getattr(self, "_profile_change_depth", 0))
            self.threads.append(_thr.current_thread() is _thr.main_thread()); return True
    _guarded = [("set_dns", lambda p: p.set_dns(True, "cloudflare", "")),
                ("set_ipv6", lambda p: p.set_ipv6(True))]
    for name, call in _guarded:
        p = DnsGuard(A6); p._profile_change_depth = 1
        r = asyncio.run(call(p))
        ok(r.get("error") == "busy" and not p.mods and p._profile_change_depth == 1,
           f"{name} refuses while another profile change is mid-flight, touching nothing")
        p = DnsGuard(A6); p._update_starting = True
        ok(asyncio.run(call(p)).get("error") == "busy" and not p.mods, f"{name} refuses while an update installs")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        p = DnsGuard(A6)
        r = asyncio.run(call(p))
        ok(r.get("success") is True and p.depth_seen == [1] and getattr(p, "_profile_change_depth", 0) == 0,
           f"{name} holds the guard across its reconnect and gives it back after ({p.depth_seen})")
        ok(p.threads == [False], f"and {name}'s reconnect runs off the event loop")
    for name, call in _guarded + [("set_bssid_lock", lambda p: p.set_bssid_lock(True)),
                                  ("set_band_preference", lambda p: p.set_band_preference(True, "a")),
                                  ("allow_wpa2", lambda p: p.allow_wpa2())]:
        p = DnsGuard(A6); p._ensure_backend_switch_state(); p._backend_switch["in_progress"] = True
        r = asyncio.run(call(p))
        ok(r.get("error") == "busy" and not p.mods, f"{name} refuses while the WiFi backend is being switched")
    class LockOffThread(m.Plugin):
        def __init__(self): super().__init__(); self.threads = []
        def _get_wifi_interface(self): return "wlan0"
        def _get_active_connection_uuid(self): return A6
        def _nmcli_modify(self, u, k, v, timeout=5): return _ok()
        def _hard_reconnect(self, uuid=None):
            self.threads.append(_thr.current_thread() is _thr.main_thread()); return True
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True, "bssid_lock_connection_uuid": A6})
    lt = LockOffThread()
    ok(asyncio.run(lt.set_bssid_lock(False)).get("success") is True and lt.threads == [False],
       "turning the lock off reconnects off the event loop too")
    _sw = Switch(); _sw._profile_change_depth = 1
    r = asyncio.run(_sw.start_backend_switch("wpa_supplicant"))
    ok(r.get("accepted") is False and r.get("reason") == "busy" and _sw._backend_switch["in_progress"] is False,
       "no backend switch while a profile change is mid-flight")
    _lock_src = _src_all[_src_all.index("    async def set_bssid_lock"):_src_all.index("    async def set_band_preference")]
    # The slice runs on to set_band_preference, so it takes in _lock_onto_best,
    # where the enable path's settling now lives, shared with moving the lock.
    ok("await asyncio.to_thread(\n            self._settle_band_before_locking" in _lock_src,
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
    def _relock_both(rl, u):
        # The two halves the band setter runs apart: the pin, then its record.
        pinned = rl._relock_pin(u)
        if pinned:
            rl._record_relock(pinned)
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": False})
    rl = Relock(); _relock_both(rl, "u")
    ok(not rl.mods, "a lock turned off while the band changed is not put back")
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True})
    rl = Relock(); _relock_both(rl, "u")
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
    ok("const UPDATE_TIMEOUT_MS = 15 * 60 * 1000;" in _tsx,
       "the panel waits for an update as long as the script may run, game wait included")
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
        async def _release_lock_from_all_profiles(self): self.released = True; return None
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
    class ElsewhereOptimize(IwdOptimize):
        def _backend_is_iwd(self): return False
        def _get_active_connection_uuid(self): return B6
        def _get_profile_ssid(self, uuid, timeout=5): return {A6: "Home", B6: "Cafe"}.get(uuid)
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True,
                      "bssid_lock_connection_uuid": A6, "bssid_lock_uuids": [A6]})
    io = ElsewhereOptimize(); r = asyncio.run(io.optimize_safe())
    ok(not io.locked and r.get("total") == 3 and r.get("results", {}).get("bssid_lock", {}).get("success")
       and "another network" in r["results"]["bssid_lock"].get("message", ""),
       "Optimize Safe on another network leaves the lock where it was set, and says so")
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True,
                      "bssid_lock_connection_uuid": B6, "bssid_lock_uuids": [B6]})
    io = ElsewhereOptimize(); asyncio.run(io.optimize_safe())
    ok(io.locked, "while on the locked network it is applied as before")
    m._save_settings(dict(m.DEFAULT_SETTINGS))

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

section("streaming auto mode: the fixes follow the stream")
try:
    def _flags(flag=False, hold=False):
        for path, on in ((m.STREAM_FLAG_FILE, flag), (m.STREAM_HOLD_FILE, hold)):
            if on: open(path, "w").write("1\n")
            elif os.path.exists(path): os.remove(path)

    _flags()
    pl = m.Plugin()
    ok(pl._volatile_gate_open({**m.DEFAULT_SETTINGS}) is True, "with auto mode off the fixes apply, as always")
    ok(m.DEFAULT_SETTINGS["streaming_mode_enabled"] is False, "and auto mode starts off")
    ok(pl._volatile_gate_open({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True}) is False,
       "in auto mode with no stream they do not")
    _flags(flag=True)
    ok(pl._volatile_gate_open({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True}) is True,
       "and with one they do")
    _flags()

    kept, refused = m._parse_stream_patterns("Sunshine-client, steam ab moon, moon gamescope")
    ok(kept == ["sunshine-client", "moon"], f"custom patterns are kept lowercase, once ({kept})")
    ok("steam" in refused and "ab" in refused and "gamescope" in refused,
       "one that would match Steam, gamescope or almost anything is refused")
    kept, refused = m._parse_stream_patterns(" ".join(f"app{i}xx" for i in range(12)))
    ok(len(kept) == m.STREAM_PATTERNS_MAX and len(refused) == 12 - m.STREAM_PATTERNS_MAX,
       "and there is a limit to how many")

    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_apps": {"moonlight": False, "evil": True,
                      "parsec": "yes"}, "streaming_custom_patterns": "x" * 5000})
    _ls = m._load_settings()
    ok(_ls["streaming_apps"] == {"moonlight": False}, "only known apps with a plain on or off are loaded")
    ok(len(_ls["streaming_custom_patterns"]) <= 512, "and the pattern field is capped")

    # Detection against a stand-in /proc.
    _proc = tempfile.mkdtemp()
    def _p(pid, cmd, comm):
        d = os.path.join(_proc, str(pid)); os.makedirs(d)
        open(os.path.join(d, "cmdline"), "wb").write(cmd)
        open(os.path.join(d, "comm"), "w").write(comm + "\n")
    _p(100, b"/app/bin/Moonlight\0--fullscreen\0", "moonlight")
    _p(101, b"", "kworker/0:1")
    _p(102, b"/usr/bin/sunshine-client\0", "sunshine-client")
    _p(os.getpid(), b"python3 moonlight\0", "python3")
    _real_proc = m.PROC_DIR
    try:
        m.PROC_DIR = _proc
        found = pl._find_stream(pl._stream_patterns({**m.DEFAULT_SETTINGS}))
        ok(found == {"app": "Moonlight", "process": "moonlight"},
           f"a streaming app is found by its command line ({found})")
        found = pl._find_stream(pl._stream_patterns({**m.DEFAULT_SETTINGS,
                                                     "streaming_apps": {"moonlight": False}}))
        ok(found is None, "an app the user unticked is not")
        found = pl._find_stream(pl._stream_patterns({**m.DEFAULT_SETTINGS,
            "streaming_apps": {"moonlight": False}, "streaming_custom_patterns": "sunshine-client"}))
        ok(found and found["process"] == "sunshine-client", "a pattern of the user's own is")
        shutil_rm = __import__("shutil").rmtree
        shutil_rm(os.path.join(_proc, "100"))
        ok(pl._find_stream(pl._stream_patterns({**m.DEFAULT_SETTINGS})) is None,
           "and the plugin's own process does not count, nor do kernel threads")
    finally:
        m.PROC_DIR = _real_proc

    class Stream(m.Plugin):
        def __init__(self, found=None):
            super().__init__(); self.found = found; self.applied = 0; self.reverted = 0
        def _find_stream(self, patterns): return self.found
        async def reapply_volatile(self):
            self.applied += 1; return {"success": True}
        def _revert_volatile_fixes(self, settings): self.reverted += 1
    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})
    st = Stream({"app": "Moonlight", "process": "moonlight"}); _flags()
    asyncio.run(st._stream_pass())
    ok(os.path.exists(m.STREAM_FLAG_FILE) and st.applied == 1, "a stream starting puts the fixes on")
    asyncio.run(st._stream_pass())
    ok(st.applied == 1, "once, not on every pass")
    st.found = None
    asyncio.run(st._stream_pass())
    ok(os.path.exists(m.STREAM_FLAG_FILE) and st.reverted == 0,
       "one pass without it keeps them on, so a restarting app does not flap them")
    asyncio.run(st._stream_pass())
    ok(not os.path.exists(m.STREAM_FLAG_FILE) and st.reverted == 1, "a second takes them off")
    st = Stream(None); _flags(flag=True)
    asyncio.run(st._stream_pass(settle=True))
    ok(not os.path.exists(m.STREAM_FLAG_FILE) and st.reverted == 1,
       "when Steam says something exited, they come off at once")
    st = Stream(None); _flags(hold=True)
    asyncio.run(st._stream_pass(settle=True))
    ok(os.path.exists(m.STREAM_FLAG_FILE) and st.applied == 1,
       "held on by the user, they stay on with nothing detected")
    _flags()
    m._save_settings({**m.DEFAULT_SETTINGS})
    st = Stream({"app": "Moonlight", "process": "moonlight"})
    asyncio.run(st._stream_pass())
    ok(not os.path.exists(m.STREAM_FLAG_FILE) and st.applied == 0, "with auto mode off, detection does nothing")

    class Cmds(m.Plugin):
        def __init__(self):
            super().__init__(); self.cmds = []; self.drivers = []
        def _get_wifi_interface(self): return "wlan0"
        def _run_cmd(self, cmd, timeout=5):
            self.cmds.append(cmd)
            if cmd[:3] == ["/usr/bin/sysctl", "-n", "net.core.rmem_max"]:
                return _ok("212992")
            return _ok()
        def _apply_driver_fixes(self, enable, persist=True, record_all=False):
            self.drivers.append((enable, persist)); self.driver_record = getattr(self, "driver_record", []) + [record_all]
        def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
        def _valve_powersave(self): return getattr(self, "valve", "3")
    _real_nm, _real_mp = m.NM_CONF_PATH, m.MODPROBE_CONF_PATH
    try:
        m.NM_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
        m.MODPROBE_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
        m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True,
                          "power_save_disabled": False}); _flags()
        c = Cmds()
        r1 = asyncio.run(c.set_power_save(True))
        r2 = asyncio.run(c.set_buffer_tuning(True))
        r3 = asyncio.run(c.set_cake(True))
        _ls = m._load_settings()
        ok(all(r.get("success") and r.get("paused") for r in (r1, r2, r3)) and not c.cmds,
           "turning a fix on in auto mode with no stream records it and changes nothing yet")
        ok(_ls["power_save_disabled"] and _ls["buffer_tuning_enabled"] and _ls["cake_enabled"],
           "the choice is kept for the next stream")
        r = asyncio.run(c.reapply_volatile())
        ok(r.get("paused") and not c.cmds, "a game launch reapplies nothing while no stream runs")

        _flags(flag=True); c = Cmds()
        asyncio.run(c.set_power_save(True))
        ok(["/usr/bin/iw", "dev", "wlan0", "set", "power_save", "off"] in c.cmds,
           "with a stream running power save goes off")
        ok(not os.path.exists(m.NM_CONF_PATH), "but NetworkManager's file, which would hold it off all day, is not written")
        ok(c.drivers == [(True, False)], "nor the modprobe options, which only apply at the next module load")

        c = Cmds(); c.valve = "3"
        c._revert_volatile_fixes(m._load_settings())
        ok(["/usr/bin/iw", "dev", "wlan0", "set", "power_save", "on"] in c.cmds and c.drivers == [(False, True)],
           "after the stream power save comes back on")
        ok(["/usr/bin/tc", "qdisc", "del", "dev", "wlan0", "root"] in c.cmds
           and ["/usr/bin/ip", "link", "set", "wlan0", "txqueuelen", "1000"] in c.cmds,
           "and CAKE comes off, with the queue length back to the default")
        c = Cmds(); c.valve = "2"
        c._revert_volatile_fixes(m._load_settings())
        ok(not any(cmd[-2:] == ["power_save", "on"] for cmd in c.cmds),
           "unless Steam's own power management is off, when it stays off")

        # Drift: no stream means off on purpose, not drifted.
        _flags(); c = Cmds()
        status = {"live": {}, "drift": {}}; actions = []
        c._collect_host_tuning(m._load_settings(), "u", "wlan0", status, actions)
        ok("buffer_tuning" not in status["drift"] and "cake" not in status["drift"] and not actions,
           "with no stream nothing reads as drifted, and CAKE is not put back")
        _flags(flag=True); status = {"live": {}, "drift": {}}; actions = []
        c._collect_host_tuning(m._load_settings(), "u", "wlan0", status, actions)
        ok(status["drift"].get("buffer_tuning") and status["drift"].get("cake") and actions,
           "during a stream a missing fix is drift as before")

        # Turning auto mode on and off.
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        open(m.MODPROBE_CONF_PATH, "w").write("options x y=1\n")
        _flags()
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True})
        st = Stream(None)
        r = asyncio.run(st.set_streaming_mode(True))
        ok(r.get("success") and m._load_settings()["streaming_mode_enabled"], "auto mode turns on")
        ok(not os.path.exists(m.NM_CONF_PATH) and not os.path.exists(m.MODPROBE_CONF_PATH),
           "and removes what held power save off all the time")
        ok(st.reverted == 1 and not os.path.exists(m.STREAM_FLAG_FILE),
           "with nothing streaming, the fixes that were on come off straight away")
        _flags(hold=True)
        r = asyncio.run(st.set_streaming_mode(False))
        ok(st.applied == 1 and not os.path.exists(m.STREAM_HOLD_FILE),
           "turning it off puts them back on for good, and drops the hold")
        st = Stream(None); m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})
        r = asyncio.run(st.set_streaming_hold(True))
        ok(r.get("held") and os.path.exists(m.STREAM_FLAG_FILE) and st.applied == 1,
           "holding the fixes on applies them for a stream detection missed")
        asyncio.run(st.set_streaming_hold(False))
        ok(not os.path.exists(m.STREAM_FLAG_FILE) and st.reverted == 1, "and letting go takes them off")
        r = asyncio.run(st.set_streaming_app("evil", True))
        ok(r.get("success") is False, "only known apps can be ticked")
        r = asyncio.run(st.set_streaming_patterns("steam sunshine-client"))
        ok(m._load_settings()["streaming_custom_patterns"] == "sunshine-client" and "steam" in r.get("message", ""),
           "a refused pattern is named back to the user")
    finally:
        m.NM_CONF_PATH, m.MODPROBE_CONF_PATH = _real_nm, _real_mp
        _flags()

    # Review round fixes.
    _rv = m.WIFI_BACKEND_CONF
    _real_nm2, _real_mp2 = m.NM_CONF_PATH, m.MODPROBE_CONF_PATH
    try:
        m.WIFI_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "99-valve-wifi-backend.conf")
        m.NM_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
        m.MODPROBE_CONF_PATH = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer.conf")
        open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=2\n[device]\nwifi.backend=wpa_supplicant\n")
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "valve_powersave_before": "2"})
        class RealValve(Cmds):
            def _valve_powersave(self): return m.Plugin._valve_powersave(self)
        c = RealValve(); _maybe_run(c._drop_persistent_power_save())
        ok(["/usr/bin/nmcli", "general", "reload", "conf"] in c.cmds,
           "NetworkManager reads its config again once our file is gone, or it holds power save off until reboot")
        ok(m._load_settings()["valve_powersave_before"] == "2",
           "Steam's own power management setting stays recorded as the user's")
        m.Plugin()._release_valve_powersave(m._load_settings())
        ok(m.Plugin()._valve_powersave() == "2", "so a later Reset or uninstall leaves it alone")
        c = RealValve(); _maybe_run(c._drop_persistent_power_save())
        ok(["/usr/bin/nmcli", "general", "reload", "conf"] not in c.cmds,
           "and nothing is reloaded when there was no file of ours to remove")
        # QA gate 2026-10-20: with no file of ours, a 2 in Valve's file is the
        # user's own Steam setting, and used to be undone on every load and at
        # the end of every stream.
        open(m.WIFI_BACKEND_CONF, "w").write("[connection]\nwifi.powersave=2\n[device]\nwifi.backend=wpa_supplicant\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True, "valve_powersave_before": ""})
        c = RealValve(); _maybe_run(c._drop_persistent_power_save())
        ok(m.Plugin()._valve_powersave() == "2" and m._load_settings()["valve_powersave_before"] == "2",
           "a load with no file of ours leaves the user's Steam power management setting alone, and records it as theirs")
        m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True, "power_save_disabled": True,
                          "valve_powersave_before": "?"})
        c = RealValve(); asyncio.run(c.set_power_save(False))
        ok(m.Plugin()._valve_powersave() == "2",
           "and so does the end of a stream in auto mode, which never writes a file of ours")
        # Turning auto mode on removes those files in a worker thread. The
        # save that records Valve's value ran there too, racing every setter
        # on the event loop for the settings file.
        import threading as _thr2
        _save_threads = []; _real_save2 = m._save_settings
        def _spy_save(data):
            _save_threads.append(_thr2.current_thread() is _thr2.main_thread()); _real_save2(data)
        class StreamValve(Stream):
            def _run_cmd(self, cmd, timeout=5): return _ok()
            def _valve_powersave(self): return m.Plugin._valve_powersave(self)
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": False}); _flags()
        m._save_settings = _spy_save
        try:
            asyncio.run(StreamValve(None).set_streaming_mode(True))
        finally:
            m._save_settings = _real_save2
            _flags()
        ok(_save_threads and all(_save_threads) and m._load_settings()["streaming_mode_enabled"] is True,
           f"turning auto mode on saves the settings on the event loop only ({_save_threads})")
    finally:
        m.WIFI_BACKEND_CONF, m.NM_CONF_PATH, m.MODPROBE_CONF_PATH = _rv, _real_nm2, _real_mp2

    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True, "cake_enabled": True}); _flags()
    c = Cmds(); st_ = {"drift": {"cake": True}, "live": {}}
    c._apply_cake({"iface": "wlan0"}, m._load_settings(), st_, {})
    ok(not any(cmd[:2] == ["/usr/bin/tc", "qdisc"] for cmd in c.cmds),
       "a CAKE reassertion proposed before auto mode came on does not put CAKE back while paused")
    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True, "cake_enabled": True,
                      "buffer_tuning_enabled": True})
    c = Cmds(); asyncio.run(c.set_buffer_tuning(False))
    ok(["/usr/bin/ip", "link", "set", "wlan0", "txqueuelen", "1000"] in c.cmds,
       "turning buffer tuning off while CAKE is paused leaves the queue length at the default")
    c = Cmds(); asyncio.run(c.set_cake(False))
    ok(["/usr/bin/ip", "link", "set", "wlan0", "txqueuelen", "1000"] in c.cmds,
       "and turning CAKE off while tuning is paused does too")

    class Aspm(Cmds):
        def __init__(self):
            super().__init__(); self.record = []
        def _apply_pcie_aspm_fix(self, enable, record_all=False):
            self.record.append(record_all); return []
    _flags(flag=True)
    a = Aspm(); asyncio.run(a.set_power_save(True))
    ok(a.record == [False], "fixes already on are not read as the system's own PCIe values")
    a = Aspm(); a._gate_opening = True; asyncio.run(a.set_power_save(True))
    ok(a.record == [True], "only fixes coming on from off are")
    ok(getattr(a, "driver_record", None) == [True],
       "and the same goes for the driver parameters")

    st = Stream(None); m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True}); _flags(flag=True)
    st._stream_misses = 0
    asyncio.run(st.poke_detection())
    ok(os.path.exists(m.STREAM_FLAG_FILE) and st.reverted == 0,
       "a poke from Steam counts as one look, so a stream app restarting does not flap the fixes")
    st._stream_match = {"app": "Moonlight", "process": "moonlight"}; st._stream_misses = 0
    asyncio.run(st._stream_pass())
    ok(st._streaming_live(m._load_settings()).get("streaming_app") == "Moonlight",
       "and the panel keeps naming the stream until the fixes actually come off")
    _flags()

    async def _toggle_waits():
        sw = Stream(None); m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})
        lock = sw._stream_guard(); await lock.acquire()
        t = asyncio.ensure_future(sw.set_streaming_mode(False))
        await asyncio.sleep(0.05)
        before_release = m._load_settings()["streaming_mode_enabled"]
        lock.release(); await t
        return before_release, m._load_settings()["streaming_mode_enabled"]
    _b, _a = asyncio.run(_toggle_waits())
    ok(_b is True and _a is False,
       "turning auto mode off waits for a pass that is applying or taking off the fixes")

    # A read that fails hands back the defaults, which say auto mode is off.
    # The watch used to end on that and never look again until a reload.
    class Watched(m.Plugin):
        def __init__(self): super().__init__(); self.passes = 0
        async def _stream_pass(self, settle=False): self.passes += 1
    async def _watch_through_bad_read():
        _real_poll = m.STREAM_POLL_SECONDS
        m.STREAM_POLL_SECONDS = 0.01
        try:
            m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})
            _keep = m.SETTINGS_FILE + ".keep"; _link = m.SETTINGS_FILE + ".link"
            w = Watched(); task = asyncio.ensure_future(w._streaming_watch())
            await asyncio.sleep(0.05)
            os.replace(m.SETTINGS_FILE, _keep)
            os.symlink(_keep, _link); os.replace(_link, m.SETTINGS_FILE)  # not followed: unreadable
            await asyncio.sleep(0.05)
            os.replace(_keep, m.SETTINGS_FILE)
            seen = w.passes; await asyncio.sleep(0.05)
            alive = not task.done() and w.passes > seen
            m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": False})
            await asyncio.sleep(0.05)
            ended = task.done()
            task.cancel()
            return alive, ended
        finally:
            m.STREAM_POLL_SECONDS = _real_poll
    _alive, _ended = asyncio.run(_watch_through_bad_read())
    ok(_alive, "the streaming watch carries on through a settings read that fails")
    ok(_ended, "and still ends once the settings really say auto mode is off")

    # Startup: a reload mid-stream keeps the fixes on.
    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True}); _flags(flag=True)
    st = Stream({"app": "Moonlight", "process": "moonlight"})
    async def _start(p):
        await p._startup_streaming(m._load_settings())
        p._stream_task.cancel()
    asyncio.run(_start(st))
    ok(os.path.exists(m.STREAM_FLAG_FILE) and st.applied == 0 and st.reverted == 0,
       "a plugin reload during a stream neither takes the fixes off nor reapplies them")
    m._save_settings({**m.DEFAULT_SETTINGS}); _flags(flag=True, hold=True)
    asyncio.run(m.Plugin()._startup_streaming(m._load_settings()))
    ok(not os.path.exists(m.STREAM_FLAG_FILE) and not os.path.exists(m.STREAM_HOLD_FILE),
       "with auto mode off a leftover flag is cleared, so the dispatcher never reads it")
    _main_src = _src[_src.index("    async def _main(self):"):]
    ok(_main_src.index("await self._startup_streaming(")
       < _main_src.index("if iface and self._volatile_gate_open("),
       "startup looks for a stream before its own apply, which is gated")

    # PCIe power states are put back to what the system had.
    _dev = tempfile.mkdtemp(); os.makedirs(os.path.join(_dev, "link")); os.makedirs(os.path.join(_dev, "power"))
    for name, val in (("l1_aspm", "0"), ("l1_2_aspm", "1")):
        open(os.path.join(_dev, "link", name), "w").write(val)
    open(os.path.join(_dev, "power", "control"), "w").write("auto")
    _net = tempfile.mkdtemp(); os.makedirs(os.path.join(_net, "wlan0"))
    os.symlink(_dev, os.path.join(_net, "wlan0", "device"))
    _real_net, _real_root = m.SYSFS_NET, m.SYSFS_ROOT
    class Pcie(m.Plugin):
        def _get_wifi_interface(self): return "wlan0"
    try:
        m.SYSFS_NET, m.SYSFS_ROOT = _net, os.path.realpath(_dev)[:5]
        if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)
        Pcie()._apply_pcie_aspm_fix(True, record_all=True)
        ok(open(os.path.join(_dev, "link", "l1_2_aspm")).read() == "0"
           and open(os.path.join(_dev, "power", "control")).read() == "on", "PCIe power states go off")
        Pcie()._apply_pcie_aspm_fix(False)
        ok(open(os.path.join(_dev, "link", "l1_aspm")).read() == "0",
           "and a state the firmware had off stays off when they are restored")
        ok(open(os.path.join(_dev, "link", "l1_2_aspm")).read() == "1"
           and open(os.path.join(_dev, "power", "control")).read() == "auto",
           "while the rest go back to what they were")
        m.SYSFS_ROOT = _real_root
        # The kernel reports the L1 substates off once L1 is off. A baseline
        # read file by file, after each write, recorded them as off.
        class Coupled(Pcie):
            def _record_pcie_baseline(self, targets, baseline, record_all):
                return m.Plugin._record_pcie_baseline(targets, baseline, record_all)
        for name, val in (("l1_aspm", "1"), ("l1_1_aspm", "1"), ("l1_2_aspm", "1")):
            open(os.path.join(_dev, "link", name), "w").write(val)
        os.remove(m.PCIE_BASELINE_FILE)
        _real_open = open
        import builtins
        def _kernel_open(path, mode="r", *a, **k):
            fh = _real_open(path, mode, *a, **k)
            if "w" in mode and str(path).endswith("/link/l1_aspm"):
                class W:
                    def __enter__(s): return s
                    def __exit__(s, *e):
                        fh.close()
                        if open_val[0] == "0":
                            for sub in ("l1_1_aspm", "l1_2_aspm"):
                                _real_open(os.path.join(_dev, "link", sub), "w").write("0")
                    def write(s, v): open_val[0] = v; fh.write(v)
                return W()
            return fh
        open_val = [""]
        builtins.open = _kernel_open
        try:
            Coupled()._apply_pcie_aspm_fix(True, record_all=True)
        finally:
            builtins.open = _real_open
        _b = json.load(open(m.PCIE_BASELINE_FILE))
        ok(_b.get(os.path.join(os.path.realpath(_dev), "link", "l1_2_aspm")) == "1",
           "the L1 substates are recorded as they were, not as turning L1 off made them read")
        open(m.PCIE_BASELINE_FILE, "w").write(json.dumps({"/etc/passwd": "1", "/sys/x/l1_aspm": "evil",
                                                          "/sys/x/control": "on"}))
        ok(m._read_pcie_baseline() == {"/sys/x/control": "on"},
           "a baseline naming anything outside /sys, or a value that file cannot hold, is ignored")
    finally:
        m.SYSFS_NET, m.SYSFS_ROOT = _real_net, _real_root

    _tmpl = open("defaults/dispatcher.sh.tmpl").read()
    _gate = _tmpl.index('[ "$STREAM_MODE" = "True" ] && [ ! -f /run/wifi-optimizer/streaming ]')
    ok(_gate < _tmpl.index("# Apply power save") and "exit 0" in _tmpl[_gate:_gate + 200],
       "the dispatcher leaves the fixes off between streams")
    ok('case "$STREAM_MODE" in True|False) ;; *) STREAM_MODE="False" ;; esac' in _tmpl,
       "and accepts only True or False for the mode, reading anything else as off")
    ok(_tmpl.count('STREAM_MODE="False"') >= 3, "falling back to off when the settings cannot be read")
    ok("rtw89_pci/parameters/disable_aspm_l1ss" in _tmpl, "the dispatcher applies the rtw89 fixes the profile lists")
    _ix = open("src/index.tsx").read()
    ok(_ix.index('<PanelSection title="Advanced">') < _ix.index("<StreamingSection")
       < _ix.index('<PanelSection title="Live status">'),
       "the streaming section sits after Advanced, since it changes when the others apply")
    ok(all(f'gatedBadge("{k}"' in _ix for k in ("power_save", "buffer_tuning", "cake")),
       "the three fixes it governs read as paused between streams, not as off or drifted")
    _hook = _ix[_ix.index("RegisterForAppLifetimeNotifications"):_ix.index("onDismount()")]
    ok(_hook.count("pokeDetection()") >= 2, "a game starting or stopping makes it look again")
    ok("streaming={status?.live}" in _ix and "Auto mode: no stream, fixes paused"
       in open("src/components/PanelHeader.tsx").read(),
       "and the header always says which state it is in")
    _sm = m.Plugin.__dict__
    ok(all(n in _sm for n in ("set_streaming_mode", "set_streaming_hold", "set_streaming_app",
                              "set_streaming_patterns", "list_streaming_apps", "poke_detection")),
       "the panel has a call for each control")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("settings: a file that does not parse is kept, not overwritten")
try:
    _real_settings = m.SETTINGS_FILE
    try:
        _d = tempfile.mkdtemp(); m.SETTINGS_FILE = os.path.join(_d, "settings.json")
        _corrupt = m.SETTINGS_FILE + ".corrupt"
        _bad = '{"dns_uuids": ["aaaaaaaa-0000-0000-0000-0000000000a6"], "dns_enabled": tru'
        open(m.SETTINGS_FILE, "w").write(_bad)
        _loaded = m._load_settings()
        ok({**_loaded, "pins_owned": []} == m.DEFAULT_SETTINGS,
           "a settings file that does not parse loads as defaults")
        # The record of which pins were ours went with the file, and a pin
        # nobody owns is left alone as set by hand, so nothing would clear one.
        ok(sorted(_loaded["pins_owned"]) == ["802-11-wireless.band", "802-11-wireless.bssid"]
           and sorted(m._load_settings()["pins_owned"]) == ["802-11-wireless.band", "802-11-wireless.bssid"],
           "except that the band and BSSID pins count as ours, on that load and the ones after it")
        ok(os.path.isfile(_corrupt) and open(_corrupt).read() == _bad,
           "and is kept, unchanged, as settings.json.corrupt")
        ok(not os.path.exists(m.SETTINGS_FILE),
           "so saving the defaults afterwards cannot overwrite the user's data")
        _identity = getattr(m, "_corrupt_settings_identity", lambda: None)
        ok(_identity() is not None, "and the kept file is what records the event for the panel")
        m._save_settings(m._load_settings())
        ok(open(_corrupt).read() == _bad, "a later load and save leave the kept copy alone")
        open(m.SETTINGS_FILE, "w").write("[1, 2, 3]")
        m._load_settings()
        ok(open(_corrupt).read() == "[1, 2, 3]",
           "valid JSON that is not an object is set aside too, replacing the older copy")
        # Conditions that may pass, or were never the user's data: nothing moves.
        os.remove(_corrupt)
        ok(m._load_settings() == m.DEFAULT_SETTINGS and not os.path.exists(_corrupt),
           "a missing settings file sets nothing aside, and with nothing kept the defaults are plain")
        # A directory at the name: os.fdopen raises on one without closing
        # the descriptor, and root reads the settings on every poll.
        os.mkdir(m.SETTINGS_FILE)
        def _open_fds():
            _fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
            return len(os.listdir(_fd_dir))
        m._load_settings()
        _fds_before = _open_fds()
        for _ in range(50):
            _dir_read = m._load_settings()
        ok(_open_fds() <= _fds_before and _dir_read == m.DEFAULT_SETTINGS
           and os.path.isdir(m.SETTINGS_FILE) and not os.path.exists(_corrupt),
           f"a directory at the settings path loads as defaults without leaking a descriptor each read "
           f"({_fds_before} open before, {_open_fds()} after 50 reads)")
        os.rmdir(m.SETTINGS_FILE)
        os.mkfifo(m.SETTINGS_FILE)
        m._load_settings()
        ok(os.path.exists(m.SETTINGS_FILE) and not os.path.exists(_corrupt),
           "nor does a FIFO at the settings path")
        os.remove(m.SETTINGS_FILE)
        open(m.SETTINGS_FILE, "w").write('{"driver": "' + "x" * 70000 + '"}')
        _big = m._load_settings()
        ok({**_big, "pins_owned": []} == m.DEFAULT_SETTINGS and not os.path.exists(m.SETTINGS_FILE)
           and os.path.exists(_corrupt),
           "an oversized one is set aside like one that does not parse (L22)")
        for _left in (_corrupt, m.SETTINGS_FILE):
            if os.path.exists(_left): os.remove(_left)

        def _reloaded():
            """A plugin as a reload leaves it: a fresh copy of the module, so
            nothing the old process held in memory carries over, pointed at
            the same files."""
            _spec = importlib.util.spec_from_file_location("wifiopt_reloaded", "main.py")
            fresh = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(fresh)
            for name in ("SETTINGS_FILE", "SYSFS_NET", "UPDATE_STATE_FILE", "STREAM_FLAG_FILE",
                         "STREAM_HOLD_FILE", "PCIE_BASELINE_FILE", "PROC_DIR"):
                setattr(fresh, name, getattr(m, name))
            class RecoveredStatus(fresh.Plugin):
                def _collect_status(self):
                    return ({"success": True, "connected": False, "settings": {}, "live": {},
                             "drift": {}, "external": {}}, {}, [])
            return RecoveredStatus()
        def _shown(plugin):
            return asyncio.run(plugin.get_status()).get("settings_recovered") is True

        # As on the Deck: the panel's poll sets the file aside in one process
        # and Decky reloads the plugin before the notice is shown.
        open(m.SETTINGS_FILE, "w").write(_bad)
        _rp = _reloaded()
        ok(_shown(_rp), "the status tells the panel the settings were reset")
        ok(_shown(_rp), "on every poll, not once, so the plugin's own status reads cannot swallow it")
        ok(not os.path.exists(m.SETTINGS_FILE) and os.path.isfile(_corrupt),
           "(the poll itself set the unreadable file aside)")
        _rp = _reloaded()
        ok(_shown(_rp), "and still does after a plugin reload, which keeps nothing in memory")
        _ack = getattr(_rp, "acknowledge_settings_recovered", None)
        ok(_ack is not None and asyncio.run(_ack()).get("success") is True
           and not _shown(_rp), "until the panel says the user has seen it")
        ok(m._load_settings().get("corrupt_acknowledged") == _identity() is not None,
           "the acknowledgement is saved in settings, naming the kept file")
        ok(not _shown(_reloaded()), "so it stays dismissed after a reload")
        ok(open(_corrupt).read() == _bad, "and dismissing it leaves the kept copy alone")
        _before = open(m.SETTINGS_FILE).read()
        ok(asyncio.run(_rp.acknowledge_settings_recovered()).get("success") is True
           and open(m.SETTINGS_FILE).read() == _before,
           "dismissing it again changes nothing")

        # A different file at the name than the one acknowledged, with the
        # acknowledgement itself still in settings.
        _swap = os.path.join(_d, "swap")
        open(_swap, "w").write(_bad + " ")
        os.replace(_swap, _corrupt)
        ok(m._load_settings().get("corrupt_acknowledged") != [] and _shown(_reloaded()),
           "a different kept file than the one dismissed is reported again")
        asyncio.run(_rp.acknowledge_settings_recovered())
        ok(not _shown(_reloaded()), "(and dismissed again)")
        # The ordinary way it happens: the settings file breaks once more.
        m._save_settings({**m._load_settings(), "dns_enabled": True})
        open(m.SETTINGS_FILE, "w").write('{"broken": ')
        _rp = _reloaded()
        ok(_shown(_rp) and open(_corrupt).read() == '{"broken": ',
           "a second corruption after dismissing the first shows the notice again")
        asyncio.run(_rp.acknowledge_settings_recovered())
        ok(not _shown(_reloaded()), "(and dismissed again)")

        # Reset keeps the kept copy, and so keeps whether it was dismissed:
        # a notice already seen does not come back for an unchanged file.
        m._save_settings({**m._load_settings(), "streaming_custom_patterns": "moonlight"})
        asyncio.run(Sysctl(STOCK_618).reset_settings())
        ok(os.path.isfile(_corrupt) and m._load_settings().get("streaming_custom_patterns") == ""
           and not _shown(_reloaded()),
           "a reset keeps the kept file and does not bring back a dismissed notice")
        m._save_settings({**m._load_settings(), "corrupt_acknowledged": []})
        asyncio.run(Sysctl(STOCK_618).reset_settings())
        ok(_shown(_reloaded()), "nor hide one that has not been dismissed")
        _src_ack = open("main.py").read()
        _src_ack = _src_ack[_src_ack.index("    async def acknowledge_settings_recovered(self"):]
        _src_ack = _src_ack[:_src_ack.index("\n    def ", 10)]
        ok(_src_ack.index("_corrupt_settings_identity()") < _src_ack.index("_load_settings()"),
           "dismissing looks at the kept file before loading settings, which can set a new one aside")

        # Nothing at the name, or something that is not a file this plugin
        # moved there: no notice, and nothing followed.
        os.remove(_corrupt)
        ok(not _shown(_reloaded()), "no kept file, no notice")
        ok(asyncio.run(_rp.acknowledge_settings_recovered()).get("success") is True,
           "and dismissing with nothing kept still succeeds")
        _target = os.path.join(_d, "elsewhere")
        open(_target, "w").write(_bad)
        os.symlink(_target, _corrupt)
        m._save_settings({**m._load_settings(), "corrupt_acknowledged": []})
        ok(not _shown(_reloaded()) and _identity() is None,
           "a link at the kept file's name is not reported")
        asyncio.run(_rp.acknowledge_settings_recovered())
        ok(os.path.islink(_corrupt) and open(_target).read() == _bad
           and m._load_settings().get("corrupt_acknowledged") == [],
           "nor followed, and dismissing records nothing for it")
        os.remove(_corrupt)
        os.mkdir(_corrupt)
        ok(not _shown(_reloaded()) and _identity() is None, "nor is a directory there")
        os.rmdir(_corrupt)

        # The acknowledgement comes from a file the desktop user can edit.
        open(_corrupt, "w").write(_bad)
        _good = _identity()
        for _junk in ("1 2 3", [1, 2], [1, 2, 3, 4], [-1, 2, 3], [True, 2, 3], [1.0, 2, 3],
                      ["1", "2", "3"], None, {"ino": 1}, [[1], 2, 3]):
            m._save_settings({**m.DEFAULT_SETTINGS, "corrupt_acknowledged": _junk})
            ok(m._load_settings().get("corrupt_acknowledged") == [] and _shown(_reloaded()),
               f"a malformed acknowledgement ({_junk!r}) is dropped and the notice shown")
        m._save_settings({**m.DEFAULT_SETTINGS, "corrupt_acknowledged": _good})
        ok(m._load_settings().get("corrupt_acknowledged") == _good and not _shown(_reloaded()),
           "while a well-formed one survives a load")
        os.remove(_corrupt)
        ok(not hasattr(m, "_settings_recovered"), "and no flag in memory decides it any more")
    finally:
        m.SETTINGS_FILE = _real_settings
    _ix = open("src/index.tsx").read()
    ok("status?.settings_recovered" in _ix and "{ERROR_MESSAGES.parse_error}" in _ix
       and "backend.acknowledgeSettingsRecovered()" in _ix,
       "the panel shows the notice and can dismiss it")
    ok("settings.json.corrupt" in open("src/types.ts").read(),
       "and the notice says where the old file was kept")
    ok('"acknowledge_settings_recovered"' in open("src/backend.ts").read(),
       "the panel's call names the backend method")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("settings: the defaults are never handed out to be changed")
_defaults_before = json.loads(json.dumps(m.DEFAULT_SETTINGS))
try:
    _real_settings = m.SETTINGS_FILE
    try:
        m.SETTINGS_FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
        _s = m._load_settings()
        _s["bssid_lock_uuids"].append(A6); _s["ap_move_failures"]["x"] = 1
        _s["streaming_apps"]["moonlight"] = False
        ok(m.DEFAULT_SETTINGS["bssid_lock_uuids"] == [] and m.DEFAULT_SETTINGS["ap_move_failures"] == {}
           and m.DEFAULT_SETTINGS["streaming_apps"] == {},
           "changing a list or dict in defaults loaded with no file leaves DEFAULT_SETTINGS alone")
        m._save_settings({"driver": "rtw88"})
        _s = m._load_settings()
        _s["ap_move_failures"]["x"] = 1; _s["sysctl_baseline"]["kernel"] = "x"
        ok(m.DEFAULT_SETTINGS["ap_move_failures"] == {} and m.DEFAULT_SETTINGS["sysctl_baseline"] == {},
           "and so does one filled in for a key the saved file lacks")
        m._save_settings({"ap_move_failures": "not a dict"})
        _s = m._load_settings(); _s["ap_move_failures"]["x"] = 1
        ok(m.DEFAULT_SETTINGS["ap_move_failures"] == {},
           "and one put back in place of a value of the wrong type")
    finally:
        m.SETTINGS_FILE = _real_settings
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
finally:
    m.DEFAULT_SETTINGS.clear(); m.DEFAULT_SETTINGS.update(_defaults_before)

section("Reset: asks before undoing everything")
try:
    _ix = open("src/index.tsx").read()
    _hr = _ix[_ix.index("const handleResetSettings"):]
    _hr = _hr[:_hr.index("const runReset")]
    ok("showModal(" in _hr and "<ConfirmModal" in _hr and "onOK={() => void runReset()}" in _hr,
       "the Reset button opens a confirmation, and only its OK runs the reset")
    ok('strOKButtonText="Reset"' in _hr and 'strCancelButtonText="Cancel"' in _hr,
       "with buttons named Reset and Cancel")
    ok("backend.resetSettings()" not in _hr and "onReset={handleResetSettings}" in _ix,
       "and nothing resets on the first press")
    _rct = _ix[_ix.index("const RESET_CONFIRM_TEXT"):]
    _rct = _rct[:_rct.index(";")]
    ok(all(w in _rct for w in ("Buffer tuning", "CAKE", "lock", "band preference", "IPv6", "DNS")),
       "the confirmation says what a reset undoes")
    ok("Reset WiFi Optimizer?" in open("dist/index.js").read(), "and the built bundle carries it")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Reset: everything off, nothing lost, nothing alongside")
try:
    import threading as _thr3
    R1, R2, R3 = ("eeeeeeee-0000-0000-0000-0000000000a1", "eeeeeeee-0000-0000-0000-0000000000a2",
                  "eeeeeeee-0000-0000-0000-0000000000a3")
    class Resetting(m.Plugin):
        def __init__(self, refuse=(), slow=0.0, installs=True):
            super().__init__(); self.cmds = []; self.mods = []; self.refuse = set(refuse)
            self.slow = slow; self.installs = installs; self.installed = None
            self.on_loop = []; self.depth = []; self.ps_at_revert = []
        async def get_device_info(self):
            return {"success": True, "model": "lcd", "driver": "rtw88", "device_family": "deck_lcd",
                    "device_label": "Steam Deck LCD", "chip_label": "WiFi 5 (RTL8822CE)",
                    "supports_6ghz": False}
        def _detect_distro(self): return {"id": "steamos", "name": "SteamOS"}
        def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
        def _get_wifi_interface(self): return "wlan0"
        def _valve_powersave(self): return "3"
        def _nmcli_modify(self, uuid, key, value, timeout=5):
            self.on_loop.append(_thr3.current_thread() is _thr3.main_thread())
            self.depth.append(getattr(self, "_profile_change_depth", 0))
            if self.slow: time.sleep(self.slow)
            self.mods.append((uuid, key, value))
            if uuid in self.refuse:
                return {"success": False, "stdout": "", "stderr": "permission denied", "returncode": 1}
            return _ok()
        def _apply_driver_fixes(self, enable, persist=True, record_all=False):
            self.ps_at_revert.append(m._load_settings()["power_save_disabled"])
        def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
        def _install_dispatcher(self): self.installed = self.installs; return self.installs
        def _remove_dispatcher(self): return True
        def _release_generic_backend_conf(self): pass
    def _before(**extra):
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True, "auto_fix_on_wake": False,
                          "bssid_lock_enabled": True, "bssid_lock_uuids": [R1, R2],
                          "bssid_lock_connection_uuid": R1, "dns_enabled": True, "dns_uuids": [R3],
                          "pins_owned": ["802-11-wireless.bssid"], "cake_enabled": True, **extra})
    _before()
    rs = Resetting(refuse=[R2])
    r = asyncio.run(rs.reset_settings())
    _after = m._load_settings()
    ok(r.get("success") is True and _after["power_save_disabled"] is False,
       "Reset saves power save off: it has just turned it back on, so on would read as drifted "
       "and the dispatcher would put it back off by itself")
    ok(rs.ps_at_revert and all(v is False for v in rs.ps_at_revert),
       f"and saves it before anything comes off, so nothing reapplies it part way ({rs.ps_at_revert})")
    ok(_after["bssid_lock_uuids"] == [R2] and _after["pins_owned"] == ["802-11-wireless.bssid"]
       and _after["dns_uuids"] == [] and _after["bssid_lock_enabled"] is False,
       "a pin that would not come off stays tracked, with what this install owns, for the cleanup to finish")
    ok(rs.installed is True and _after["auto_fix_on_wake"] is True,
       "auto-fix reads on, as the defaults have it, with the script actually put in place")
    _before()
    rs = Resetting(installs=False); asyncio.run(rs.reset_settings())
    ok(m._load_settings()["auto_fix_on_wake"] is False,
       "and reads off when the script could not be installed")
    ok(rs.on_loop and not any(rs.on_loop), "the profile writes run off the event loop")
    ok(rs.depth and all(d == 1 for d in rs.depth),
       "holding the profile-change guard, so no lock, band, DNS or IPv6 change runs alongside")
    _before(); rs = Resetting(); rs._pin_released_at = time.time(); rs._released_at = {"x": 1.0}
    asyncio.run(rs.reset_settings())
    ok(not getattr(rs, "_pin_released_at", 0) and not getattr(rs, "_released_at", {}),
       "a pin released before the reset is not still announced after it")
    # Out of time: what is left stays tracked, rather than Reset taking minutes.
    _before(bssid_lock_uuids=[f"eeeeeeee-0000-0000-0000-0000000001{i:02d}" for i in range(8)],
            bssid_lock_connection_uuid="")
    rs = Resetting(slow=0.2); rs._RESET_PIN_BUDGET = 1.0
    _t = time.monotonic(); asyncio.run(rs.reset_settings()); _took = time.monotonic() - _t
    ok(_took < 1.5 and 0 < len(m._load_settings()["bssid_lock_uuids"]) < 8,
       f"Reset's pin clearing stops at its budget and keeps the rest tracked ({_took:.1f}s, "
       f"{len(m._load_settings()['bssid_lock_uuids'])} left)")
    # Refused while something else is changing the same things.
    for what, setup in [
        ("another profile change is mid-flight", lambda p: setattr(p, "_profile_change_depth", 1)),
        ("the WiFi backend is being switched",
         lambda p: (p._ensure_backend_switch_state(), p._backend_switch.update(in_progress=True))),
    ]:
        _before(); rs = Resetting(); setup(rs)
        r = asyncio.run(rs.reset_settings())
        ok(r.get("error") == "busy" and not rs.mods and m._load_settings()["power_save_disabled"] is True,
           f"Reset refuses while {what}, touching nothing")
    # A streaming pass applying or taking off the fixes finishes first.
    async def _reset_waits():
        _before(); rs = Resetting()
        guard = rs._stream_guard(); await guard.acquire()
        task = asyncio.ensure_future(rs.reset_settings())
        await asyncio.sleep(0.05)
        early = (list(rs.mods), m._load_settings()["power_save_disabled"])
        guard.release(); await task
        return early
    _early = asyncio.run(_reset_waits())
    ok(_early == ([], True), f"Reset waits for a streaming pass in progress ({_early})")

    # The pass itself: auto mode turned off while it looked for a stream.
    class LateOff(m.Plugin):
        def __init__(self): super().__init__(); self.applied = 0; self.reverted = 0
        def _find_stream(self, patterns):
            m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": False})
            return {"app": "Moonlight", "process": "moonlight"}
        async def reapply_volatile(self): self.applied += 1; return {"success": True}
        def _revert_volatile_fixes(self, settings): self.reverted += 1
    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})
    for _fl in (m.STREAM_FLAG_FILE, m.STREAM_HOLD_FILE):
        if os.path.exists(_fl): os.remove(_fl)
    lo = LateOff(); asyncio.run(lo._stream_pass())
    ok(lo.applied == 0 and not os.path.exists(m.STREAM_FLAG_FILE),
       "a streaming pass that finds auto mode gone after its search applies nothing")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("diagnostics: no network names or addresses")
try:
    _IW_DEV = ("phy#0\n\tInterface wlan0\n\t\tifindex 3\n\t\twdev 0x1\n"
               "\t\taddr 3c:e9:f7:12:34:56\n\t\tssid Smith Family 5G\n\t\ttype managed\n"
               "\t\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz\n"
               "\t\ttxpower 22.00 dBm\n")
    _IW_LINK = "Connected to 62:e9:31:0d:1f:e7 (on wlan0)\n\tSSID: Smith Family 5G\n"

    class Diag(m.Plugin):
        def _get_wifi_interface(self): return "wlan0"
        async def get_device_info(self): return {"success": True, "driver": "rtw89"}
        def _detect_distro(self): return {"id": "steamos", "name": "SteamOS"}
        def _get_support_tier(self): return 1
        def _run_cmd(self, cmd, timeout=5):
            if cmd[-1:] == ["dev"]: return _ok(_IW_DEV + _IW_LINK + "addr 192.168.1.23 fe80::1c2b:3cff:fe4d:5e6f")
            if cmd[-2:] == ["reg", "get"]: return _ok("global\ncountry US: DFS-FCC\n\t(2400 - 2472 @ 40), (N/A, 30), (N/A)\n")
            if cmd[-1:] == ["-r"]: return _ok("6.11.11-valve24-2-neptune-611\n")
            return _ok()
    _info = asyncio.run(Diag().get_diagnostic_info())
    _flat = json.dumps(_info)
    ok(_info.get("success") is True and "Smith Family" not in _flat,
       "the diagnostics do not carry the network's name, wherever it appears")
    ok(not re.search(r"(?i)3c:e9:f7|62:e9:31", _flat),
       "nor the device's or the access point's hardware address")
    ok("192.168.1.23" not in _flat and "fe80::1c2b" not in _flat, "nor any IP address")
    ok("ssid <ssid>" in _info.get("iw_dev", "") and "addr <mac>" in _info.get("iw_dev", ""),
       "each is replaced by a placeholder, so the shape of the output still reads")
    ok("channel 36 (5180 MHz), width: 80 MHz" in _info.get("iw_dev", "")
       and "6.11.11-valve24-2-neptune-611" in _info.get("kernel", "")
       and "country US" in _info.get("iw_reg", ""),
       "while the channel, kernel and region stay readable")
    _real_settings = m.SETTINGS_FILE
    try:
        m.SETTINGS_FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
        _sv = asyncio.run(Diag().save_diagnostic_info())
        _txt = open(_sv["path"]).read() if _sv.get("success") else ""
        ok(_txt and "Smith Family" not in _txt and "3c:e9:f7" not in _txt,
           "the saved diagnostics file is redacted the same way")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        asyncio.run(Sysctl(STOCK_618).reset_settings())
        ok(not os.path.exists(_sv["path"]), "Reset removes the diagnostics file")
        asyncio.run(Diag().save_diagnostic_info())
        asyncio.run(Sysctl(STOCK_618)._uninstall())
        ok(not os.path.exists(_sv["path"]), "and so does uninstalling")
    finally:
        m.SETTINGS_FILE = _real_settings
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("diagnostics: enough to read a WiFi that never came back after sleep")
try:
    def _journal(lines, host):
        # What journalctl -k prints: with --no-hostname the host column is gone.
        out = []
        for stamp, ident, msg in lines:
            out.append(f"[{stamp:>12}] {ident}: {msg}" if host is None
                       else f"[{stamp:>12}] {host} {ident}: {msg}")
        return "\n".join(out) + "\n"

    _THIS = [
        ("0.500000", "systemd[1]", "Hostname set to <AERO-deck>."),
        ("12.345678", "kernel", "rtw88_8822ce 0000:01:00.0: failed to leave lps state"),
        ("12.400000", "kernel", "usb 1-1: new high-speed USB device number 2"),
        ("13.000000", "kernel", "PM: suspend exit"),
        ("13.100000", "kernel", "wlan0: authenticate with 62:e9:31:0d:1f:e7 (local address=3c:e9:f7:12:34:56)"),
        ("13.200000", "kernel", "ieee80211 phy0: Hardware restart was requested"),
        ("13.300000", "kernel", "wlan0: deauthenticated from 62:e9:31:0d:1f:e7: (Reason: 4=DISASSOC_DUE_TO_INACTIVITY)"),
    ]
    _words = [a + b for a in "abcdefghij" for b in "abcdefghij"]
    _PREV = ([(f"{100 + i}.000000", "kernel", f"rtw88_8822ce 0000:01:00.0: early note {_words[i]}") for i in range(60)]
             + [("500.000000", "kernel", "PM: suspend entry (s2idle)"),
                ("900.000000", "kernel", "PM: suspend exit"),
                ("900.100000", "kernel", "rtw88_8822ce 0000:01:00.0: failed to leave lps state")]
             + [(f"{901 + i}.000000", "kernel", f"rtw88_8822ce 0000:01:00.0: failed to send h2c command {i}") for i in range(120)]
             + [(f"{1100 + i}.000000", "kernel", "wlan0: send auth to 62:e9:31:0d:1f:e7 (try 2/3)") for i in range(80)]
             + [(f"{1300 + i}.000000", "kernel", f"rtw88_8822ce 0000:01:00.0: late note {_words[i]}") for i in range(60)])
    _seen = []

    class WakeDiag(Diag):
        async def get_device_info(self): return {"success": True, "driver": "rtw88"}
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            _seen.append(cmd)
            if cmd[0] == "/usr/bin/journalctl" and "-t" in cmd:
                return _ok("[  3.100000] wifi-optimizer[812]: driver parameters before applying: "
                           "disable_lps_deep=N disable_aspm=N\n")
            if cmd[0] == "/usr/bin/journalctl" and "-u" in cmd:
                return _ok("[  9.000000] NetworkManager[700]: <info>  device (wlan0): Activation: "
                           "starting connection 'Smith Family 5G' (1b2c)\n"
                           "[  9.100000] wpa_supplicant[650]: wlan0: SME: Trying to authenticate with "
                           "62:e9:31:0d:1f:e7 (SSID=SmithNet freq=5180 MHz)\n"
                           "[  9.200000] iwd[600]: event: connect-info, ssid: Smith Home Guest, "
                           "bss: 62:e9:31:0d:1f:e7, signal: -52, load: 3/255\n")
            if cmd[0] == "/usr/bin/journalctl":
                host = None if "--no-hostname" in cmd else "AERO-deck"
                return _ok(_journal(_THIS if cmd[cmd.index("-b") + 1] == "0" else _PREV, host))
            if cmd[0] == "/usr/bin/nmcli":
                if "-e" in cmd and cmd[cmd.index("-e") + 1] == "no":
                    return _ok("wlan0:wifi:disconnected\nAA:BB:CC:DD:EE:F1:bluetooth:disconnected\n")
                return _ok("wlan0:wifi:disconnected\nAA\\:BB\\:CC\\:DD\\:EE\\:F1:bluetooth:disconnected\n")
            return Diag._run_cmd(self, cmd, timeout)

    _real_settings, _real_plugin_dir = m.SETTINGS_FILE, getattr(m.decky, "DECKY_PLUGIN_DIR", None)
    _home = tempfile.mkdtemp()
    for _p in ("WiFi Optimizer", "decky-wifi-locker", "CSS Loader"):
        os.makedirs(os.path.join(_home, "plugins", _p))
    try:
        m.SETTINGS_FILE = os.path.join(tempfile.mkdtemp(), "settings.json")
        m.decky.DECKY_PLUGIN_DIR = os.path.join(_home, "plugins", "WiFi Optimizer")
        m._save_settings(dict(m.DEFAULT_SETTINGS, band_preference_enabled=True,
                              band_preference_ssid="Smith Family 5G",
                              power_save_disabled=True, update_channel="beta"))
        _info = asyncio.run(WakeDiag().get_diagnostic_info())
        _none = m.Plugin._diagnostic_extras(WakeDiag(), "rtw88", "none")
        os.rename(os.path.join(_home, "plugins"), os.path.join(_home, "real-plugins"))
        os.symlink(os.path.join(_home, "real-plugins"), os.path.join(_home, "plugins"))
        _linked = m.Plugin._diagnostic_extras(WakeDiag(), "rtw88", "wlan0")
    finally:
        m.SETTINGS_FILE = _real_settings
        m.decky.DECKY_PLUGIN_DIR = _real_plugin_dir
    _flat = json.dumps(_info)
    ok("plugin_version" in _info and "decky_version" in _info,
       "the plugin's and Decky's versions are in the report")
    ok(_info.get("settings", {}).get("power_save_disabled") is True
       and _info["settings"].get("update_channel") == "beta"
       and _info["settings"].get("band_preference_enabled") is True,
       "so are the feature toggles")
    ok("Smith Family" not in _flat and "band_preference_ssid" not in _flat,
       "but not the network name the band preference is scoped to")
    ok(_info.get("wifi_backend") == "wpa_supplicant", "and whether it is iwd or wpa_supplicant")
    ok(set(_info.get("driver_params", {})) == {
           "/sys/module/rtw88_core/parameters/disable_lps_deep",
           "/sys/module/rtw88_pci/parameters/disable_aspm"},
       "the driver's power parameters are read for the radio actually present")
    ok("modprobe_conf" in _info and "modprobe_conf_before_this_boot" in _info
       and "pcie" in _info and "rfkill" in _info and "dispatcher_installed" in _info,
       "with the modprobe file and whether it predates this boot, the PCIe link state and rfkill")
    ok(_info.get("decky_plugins") == ["CSS Loader", "WiFi Optimizer", "decky-wifi-locker"],
       "the other Decky plugins are listed")
    ok(_linked.get("decky_plugins") == [],
       "but not through a plugins folder swapped for a link")
    ok(_none.get("pcie") == {}, "with no interface there is no made-up PCIe section")
    _nm = [c for c in _seen if c[0] == "/usr/bin/nmcli"]
    ok(_nm and "CONNECTION" not in " ".join(_nm[0]) and "wlan0:wifi:disconnected" in _info.get("nm_devices", ""),
       "NetworkManager's device states are read without the column that names the network")
    # Pass 4 D11: rows other than wifi, ethernet, loopback and wifi-p2p go.
    ok("AA:BB:CC" not in _flat and "AA\\\\:BB" not in _flat and "bluetooth" not in _info.get("nm_devices", ""),
       "and a paired phone named by its address is not in it at all, colons escaped or not")
    _this = _info.get("kernel_log_this_boot", [])
    ok("AERO-deck" not in _flat and not any("systemd" in l or "Hostname" in l for l in _this),
       "no hostname, even one that looks like a kernel term, and no lines that are not the kernel's")
    ok(not any("usb 1-1" in l for l in _this) and not any("authenticate with" in l for l in _this),
       "USB lines and connection chatter are left out")
    ok(any(l.startswith("[   12.345678] rtw88_8822ce") for l in _this)
       and any("PM: suspend exit" in l for l in _this)
       and any("Hardware restart was requested" in l for l in _this),
       "while the driver, suspend and firmware restart lines stay, with their timestamps")
    ok(not re.search(r"(?i)62:e9:31|3c:e9:f7", _flat) and any("<mac>" in l for l in _this),
       "and addresses are redacted, including one followed by a colon")
    _prev = _info.get("kernel_log_previous_boot", [])
    ok(len(_prev) <= m.Plugin._DIAG_KERNEL_LINES + 2
       and any("PM: suspend entry" in l for l in _prev)
       and any("PM: suspend exit" in l for l in _prev)
       and any("failed to leave lps state" in l for l in _prev),
       "a long previous boot still shows the last suspend, the wake and the first error after it")
    ok(any("failed to send h2c" in l and l.endswith("(repeated 119 more times)") for l in _prev)
       and sum("failed to send h2c" in l for l in _prev) == 1,
       "a repeated driver error is folded into a count on its own line, so trimming cannot part them")
    ok(_info.get("plugin_log_previous_boot") and "disable_aspm=N" in _info["plugin_log_previous_boot"][-1],
       "the dispatcher's note of the driver parameters during the boot that failed is in the report")
    _netlog = " ".join(_info.get("network_log_previous_boot", []))
    ok("Activation" in _netlog and "Smith" not in _netlog and "62:e9:31" not in _netlog,
       "and so is the previous boot's NetworkManager/iwd/wpa_supplicant log, with network names and addresses out")
    ok("pcie_baseline" in _info and "mem_sleep" in _info,
       "with this boot's baseline and the sleep mode")
    ok(any("late note fj" in l for l in _prev[-3:]) and not any("send auth" in l for l in _prev),
       "the end of the boot is kept and the chatter is not")
    def _klog(msgs): return "".join(f"[{i:>5}.000000] kernel: {t}\n" for i, t in enumerate(msgs))
    _near = _klog([f"rtw88_8822ce: note {_words[i]}" for i in range(80)]
                  + ["PM: suspend entry (deep)", "PM: suspend exit", "rtw88_8822ce: failed to read ASPM, ret=-5"])
    _nl = m.Plugin._diag_kernel_lines(_near, m.Plugin._DIAG_KERNEL_TERMS)
    ok(len(_nl) == m.Plugin._DIAG_KERNEL_LINES + 1 and any("suspend entry" in l for l in _nl)
       and "failed to read ASPM" in _nl[-1],
       f"a last suspend near the end still gets the whole line budget ({len(_nl)} lines)")
    _warn = _klog(["------------[ cut here ]------------",
                   "WARNING: CPU: 2 PID: 9 at drivers/net/wireless/realtek/rtw88/pci.c:1385 rtw_pci_link_ps",
                   "Modules linked in: rtw88_8822ce rtw88_pci rtw88_core " + "x " * 400,
                   "Call Trace:"] + [f" rtw_frame_{_words[i]}+0x10/0x20 [rtw88_core]" for i in range(40)]
                  + ["---[ end trace 0000000000000000 ]---", "rtw88_8822ce: firmware failed to leave lps state"])
    _wl = m.Plugin._diag_kernel_lines(_warn, m.Plugin._DIAG_KERNEL_TERMS)
    ok(not any("Modules linked in" in l for l in _wl)
       and sum("rtw_frame_" in l for l in _wl) == m.Plugin._DIAG_TRACE_FRAMES
       and "leave lps" in _wl[-1] and all(len(l) <= m.Plugin._DIAG_LINE_MAX for l in _wl),
       "a WARN's trace is cut to its first frames and no line runs on")
    ok(len(m.Plugin._diag_kernel_lines(_klog(["wlan0: disassociated from <mac> (Reason: 4)"]),
                                       m.Plugin._DIAG_KERNEL_TERMS)) == 1,
       "a disassociation is kept: it is what a failed wake looks like from the radio")
    ok(len(m.Plugin._diag_kernel_lines(_klog(["ACPI FADT declares the system doesn't support PCIe ASPM, so disable it"]),
                                       m.Plugin._DIAG_KERNEL_TERMS)) == 1,
       "and so is the firmware saying ASPM is off")
    _wlp = m.Plugin._diag_kernel_lines(
        "[  5.000000] kernel: wlp1s0: deauthenticated from <mac> (Reason: 3)\n",
        m.Plugin._DIAG_KERNEL_TERMS + "|" + re.escape("wlp1s0"))
    ok(len(_wlp) == 1, "an interface named wlp1s0 is covered when it is the one present")
    ok("168.1.5" not in m.Plugin._redact_diagnostics("peer ::ffff:192.168.1.5 seen"),
       "an IPv4-mapped IPv6 address is redacted whole")
    _bad = asyncio.run(type("Raises", (Diag,), {
        "get_device_info": lambda self: (_ for _ in ()).throw(RuntimeError("bad 62:e9:31:0d:1f:e7"))})().get_diagnostic_info())
    ok(_bad.get("success") is False and "62:e9:31" not in json.dumps(_bad),
       "an error report is redacted too")

    class BrokenExtras(Diag):
        def _diagnostic_extras(self, driver, iface): raise RuntimeError("boom")
    _info = asyncio.run(BrokenExtras().get_diagnostic_info())
    ok(_info.get("success") is True and _info.get("extras_error") == "boom"
       and "6.11.11-valve24-2-neptune-611" in _info.get("kernel", ""),
       "and if the new parts fail, the rest of the report still arrives")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("dispatcher: the same tuning as the plugin, and valid once filled in")
try:
    _tmpl = open("defaults/dispatcher.sh.tmpl").read()
    _src_all = open("main.py").read()
    _buf = _tmpl[_tmpl.index("# Apply buffer tuning"):_tmpl.index("# Apply CAKE")]
    _cake = _tmpl[_tmpl.index("# Apply CAKE"):_tmpl.index("# Record enforcement")]
    _ps = _tmpl[_tmpl.index("# Apply power save"):_tmpl.index("# Apply buffer tuning")]
    ok(dict(re.findall(r"sysctl -w ([\w.]+)=(\S+)", _buf)) == m.SYSCTL_PARAMS,
       "the dispatcher's sysctl values are exactly SYSCTL_PARAMS")

    class TxLog(Sysctl):
        def txq(self):
            return [c[-1] for c in self.cmds if c[:2] == ["/usr/bin/ip", "link"] and "txqueuelen" in c]
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _tx = TxLog(STOCK_618); asyncio.run(_tx.set_buffer_tuning(True))
    ok(re.findall(r"txqueuelen (\d+)", _buf) == _tx.txq()[-1:],
       f"the dispatcher's buffer tuning queue length matches the plugin's ({_tx.txq()[-1:]})")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _tx = TxLog(STOCK_618); asyncio.run(_tx.set_cake(True))
    _tc = [c for c in _tx.cmds if c[:3] == ["/usr/bin/tc", "qdisc", "replace"]]
    _tmpl_tc = re.search(r"tc qdisc replace dev \"\$IFACE\" (root .*?) 2>/dev/null", _cake)
    ok(bool(_tc) and _tmpl_tc is not None
       and _tmpl_tc.group(1).split() == _tc[0][_tc[0].index("root"):],
       "the dispatcher's CAKE arguments match the plugin's")
    ok(re.findall(r"txqueuelen (\d+)", _cake) == _tx.txq()[-1:],
       f"and so does the queue length that goes with CAKE ({_tx.txq()[-1:]})")
    _drv = {}
    # Pass 4 D4: the list is set before the streaming exit, outside _ps.
    for _name, _body in re.findall(r'"\$DRIVER" = "(\w+)" \]; then\n(.*?)(?=\n\s*(?:elif|fi)\b)', _tmpl, re.S):
        _drv[_name] = re.findall(r"(/sys/module/\S+?)[\s\"]", _body)
    ok(_drv == {n: p["sysfs_power_fixes"] for n, p in m.DRIVER_PROFILES.items() if p["sysfs_power_fixes"]},
       "the dispatcher writes each driver's sysfs fixes from DRIVER_PROFILES, no more, no fewer")
    _case = re.search(r'case "\$DRIVER" in ([\w|]+)\)', _tmpl)
    ok(_case is not None and set(_case.group(1).split("|")) == set(m.DRIVER_PROFILES),
       "and accepts exactly the drivers the plugin has profiles for")
    _py_links = re.search(r"for aspm_file in \[(.*?)\]", _src_all, re.S)
    _sh_links = re.search(r"for f in ([\w ]+); do", _ps)
    ok(_py_links is not None and _sh_links is not None
       and re.findall(r'"(\w+)"', _py_links.group(1)) == _sh_links.group(1).split(),
       "the PCIe power state files are the same in both")

    import subprocess as _sp
    _filled = _tmpl.replace("__SETTINGS_PATH__",
                            m._sh_quote("/home/deck/homebrew/settings/WiFi Optimizer/settings.json"))
    _filled = _filled.replace("__PLUGIN_DIR__", m._sh_quote("/home/deck/homebrew/plugins/WiFi Optimizer"))
    _sh = os.path.join(tempfile.mkdtemp(), "99-wifi-optimizer"); open(_sh, "w").write(_filled)
    _syn = _sp.run(["/bin/bash", "-n", _sh], capture_output=True, text=True)
    ok("__" not in _filled and _syn.returncode == 0,
       f"the dispatcher, filled in with paths that contain a space, parses ({_syn.stderr.strip()[:120]})")
    _assign = "\n".join(l for l in _filled.splitlines() if l.startswith(("SETTINGS_FILE=", "PLUGIN_DIR=")))
    _vals = _sp.run(["/bin/bash", "-c", _assign + '\nprintf "%s|%s" "$SETTINGS_FILE" "$PLUGIN_DIR"'],
                    capture_output=True, text=True).stdout
    ok(_vals == "/home/deck/homebrew/settings/WiFi Optimizer/settings.json|"
               "/home/deck/homebrew/plugins/WiFi Optimizer",
       "and reads both paths back whole")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("custom DNS: drift on the network it was set for")
try:
    class DnsDrift(Connected):
        def __init__(self, out, read_ok=True):
            super().__init__(); self.out = out; self.read_ok = read_ok
            self.modifies = []; self.reconnects = 0
        def _run_cmd(self, cmd, timeout=5):
            if any(str(c).startswith("ipv4.dns,ipv4.ignore-auto-dns") for c in cmd):
                return {"success": self.read_ok, "stdout": self.out if self.read_ok else "",
                        "stderr": "", "returncode": 0 if self.read_ok else 1}
            return super()._run_cmd(cmd, timeout)
        def _nmcli_modify(self, *a, **k):
            self.modifies.append(a); return _ok()
        def _hard_reconnect(self, uuid=None):
            self.reconnects += 1; return True
    HOLDS = "ipv4.dns:1.1.1.1,1.0.0.1\nipv4.ignore-auto-dns:yes"
    def dns_drift(out, read_ok=True, **extra):
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "ath11k_pci", "distro_id": "steamos",
                          "dns_enabled": True, "dns_provider": "cloudflare",
                          "dns_servers": "1.1.1.1 1.0.0.1", "dns_uuids": [U], **extra})
        dd = DnsDrift(out, read_ok)
        return bool(asyncio.run(dd.get_status()).get("drift", {}).get("dns")), dd
    ok(dns_drift(HOLDS)[0] is False, "custom DNS still in place is not drift")
    ok(dns_drift("ipv4.dns:1.1.1.1 1.0.0.1\nipv4.ignore-auto-dns:yes")[0] is False,
       "nor is it when nmcli lists the servers with spaces")
    _hit, _dd = dns_drift("ipv4.dns:\nipv4.ignore-auto-dns:no")
    ok(_hit is True, "the servers gone from the profile is drift")
    ok(not [a for a in _dd.modifies if str(a[1]).startswith("ipv4.")] and _dd.reconnects == 0,
       "reported only: the poll writes no DNS and does not reconnect")
    ok(dns_drift("ipv4.dns:1.1.1.1,1.0.0.1\nipv4.ignore-auto-dns:no")[0] is True,
       "the router's DNS let back in beside ours is drift too")
    ok(dns_drift("ipv4.dns:9.9.9.9\nipv4.ignore-auto-dns:yes")[0] is True,
       "and so are other servers in place of ours")
    ok(dns_drift("ipv4.dns:\nipv4.ignore-auto-dns:no",
                 dns_uuids=["dddddddd-0000-0000-0000-0000000000d5"])[0] is False,
       "a network DNS was never set on is not drift")
    ok(dns_drift("ipv4.dns:\nipv4.ignore-auto-dns:no", dns_uuids=[])[0] is True,
       "a setting from before the list existed is checked on the network in use")
    ok(dns_drift("", read_ok=False)[0] is False and dns_drift("ipv4.dns:1.1.1.1")[0] is False,
       "a read that failed or came back incomplete is not drift")
    ok(dns_drift("ipv4.dns:\nipv4.ignore-auto-dns:no", dns_enabled=False)[0] is False,
       "nothing is drift with custom DNS off")

    class DriftedDns(Drifted):
        async def set_dns(self, enabled, provider="cloudflare", servers=""):
            self.dns_args = (enabled, provider, servers); return await self._call("dns")
    m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": True, "dns_provider": "custom",
                      "dns_servers": "9.9.9.9 1.1.1.1"})
    _dr = DriftedDns({"dns": True}); _r = asyncio.run(_dr.repair_drifted())
    ok(_dr.calls == ["dns"] and getattr(_dr, "dns_args", None) == (True, "custom", "9.9.9.9 1.1.1.1"),
       "Fix now puts the saved DNS servers back")
    _ix = open("src/index.tsx").read()
    ok('getBadge("dns", status' in _ix, "and the DNS row shows the drift")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("frontend: no error text or badge that nothing uses")
try:
    _types = open("src/types.ts").read()
    _em = _types[_types.index("export const ERROR_MESSAGES"):]
    _em = _em[:_em.index("};")]
    _keys = re.findall(r"^\s+(\w+):", _em, re.M)
    _src_ts = "".join(open(os.path.join(dp, f)).read() for dp, _dn, fs in os.walk("src") for f in fs)
    _unused = [k for k in _keys if f'"error": "{k}"' not in _src_all
               and f"ERROR_MESSAGES.{k}" not in _src_ts]
    ok(_keys and not _unused, f"every error message is one the backend can send or the panel shows (unused: {_unused})")
    _bs = _types[_types.index("export type BadgeStatus"):]
    _bs = _bs[:_bs.index(";")]
    _members = re.findall(r'"(\w+)"', _bs)
    _used_src = _src_ts.replace(_bs, "")
    _unused_b = [b for b in _members if f'badge: "{b}"' not in _used_src]
    ok(_members and not _unused_b, f"every badge kind is one the panel shows (unused: {_unused_b})")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("streaming apps: named the way people know them")
try:
    _labels = {i: l for i, l, _p in m.STREAMING_APPS}
    ok(_labels.get("chiaki") == "Chiaki (PS Remote Play)" and _labels.get("greenlight") == "Greenlight (Xbox)",
       "Chiaki and Greenlight say what they stream")
    _rd = open("README.md").read()
    ok("Chiaki (PS Remote Play)" in _rd and "Greenlight (Xbox)" in _rd, "and the README lists them the same way")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("repository: CI pinned, security policy present")
try:
    _wf = open(".github/workflows/ci.yml").read()
    _uses = re.findall(r"uses:\s*(\S+)(.*)", _wf)
    ok(_uses and all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", u) and re.search(r"#\s*v\d+\.\d+\.\d+", c)
                     for u, c in _uses),
       "every action in CI is pinned to a full commit, with its release noted")
    ok("persist-credentials: false" in _wf and re.search(r"^permissions:\n\s+contents: read", _wf, re.M),
       "CI keeps no credentials and can only read")
    ok(os.path.isfile("SECURITY.md"), "there is a security policy")
    for _f in ("SECURITY.md", ".github/workflows/ci.yml"):
        _t = open(_f, encoding="utf-8").read()
        ok(all(ord(ch) < 128 for ch in _t), f"{_f} is plain ASCII")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("J5 CI runs on a release tag, never on a push to beta or main")
# A push to beta is a release for plugins up to 0.20.0-beta on the beta
# channel (they install the branch as it stands), and install.sh installs main
# when GitHub's API does not answer. CI used to run only on those pushes, so
# every release was out before it was checked or signed. CI now runs on a
# pushed v* tag, on pull requests and by hand; the branches move to the tag
# only after release.sh has published it.
try:
    _wf = open(".github/workflows/ci.yml").read()
    _on = re.search(r"^on:\n((?:[ #].*\n|\n)*)", _wf, re.M)
    _on = _on.group(1) if _on else ""
    _push = re.search(r"^  push:\n((?:    .*\n)*)", _on, re.M)
    _push = _push.group(1) if _push else ""
    ok(re.search(r"^    tags: \[\s*'v\*'\s*\]\s*$", _push, re.M) is not None,
       "CI runs when a v* tag is pushed")
    ok("branches" not in _push, "and never on a push to a branch")
    ok(re.search(r"^  workflow_dispatch:", _on, re.M) is not None, "it can also be run by hand")
    ok(re.search(r"^  pull_request:", _on, re.M) is not None, "and on pull requests")
    ok("release.sh" in _wf[:_wf.index("\non:")], "the workflow says why, next to its triggers")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("R1 install.sh never leaves the plugin folder missing")
# A closed terminal or Ctrl+C between moving the installed copy aside and
# moving the new one into place used to leave no plugin folder; at the next
# connect the dispatcher then removed itself and the conf files. Run for real
# with stubbed root, network and systemctl, and a SIGHUP in that window.
try:
    import shutil, subprocess as _rsp
    _inst = open("install.sh").read()
    ok(re.search(r'rm -rf "\$OLD_DIR" \|\| true', _inst) is not None,
       "a previous copy that will not delete does not stop the restart")
    _bash, _mv = shutil.which("bash"), shutil.which("mv")
    if not (_bash and _mv and shutil.which("tar")):
        ok(False, "bash, mv and tar are needed to run install.sh here")
    else:
        _t = tempfile.mkdtemp(); _bin = os.path.join(_t, "bin"); os.mkdir(_bin)
        _pay = os.path.join(_t, "pay", "WifiOptimizer-9.9.9")
        for _f in ("plugin.json", "package.json", "main.py", "decky.pyi", "dist/index.js",
                   "defaults/dispatcher.sh.tmpl"):
            os.makedirs(os.path.dirname(os.path.join(_pay, _f)), exist_ok=True)
            open(os.path.join(_pay, _f), "w").write("new\n")
        _tgz = os.path.join(_t, "payload.tgz")
        _rsp.run(["tar", "czf", _tgz, "-C", os.path.dirname(_pay), "WifiOptimizer-9.9.9"], check=True)
        _home = os.path.join(_t, "home")
        _stubs = {
            "id": "echo 0\n",
            "getent": f'echo "deck:x:1000:1000::{_home}:/bin/bash"\n',
            "systemctl": 'echo "systemctl $*" >> "$STUB_LOG"\n',
            # The release lookup answers with a tag; the download writes the payload.
            "curl": ('out=""; prev=""\nfor a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
                     f'if [ -n "$out" ]; then cp "{_tgz}" "$out"; else echo \'  "tag_name": "v9.9.9",\'; fi\n'),
            # Hangs up on the installer right after it moves the installed copy aside.
            "mv": (f'"{_mv}" "$@" || exit $?\n'
                   'case "$2" in *.wifi-optimizer-previous) [ -n "$HUP_AFTER_ASIDE" ] && kill -HUP $PPID ;; esac\n'
                   'exit 0\n'),
        }
        for _n, _body in _stubs.items():
            _p = os.path.join(_bin, _n); open(_p, "w").write("#!" + _bash + "\n" + _body); os.chmod(_p, 0o755)
        _plug = os.path.join(_home, "homebrew", "plugins", "WiFi Optimizer")
        def _install(hup):
            shutil.rmtree(_home, ignore_errors=True); os.makedirs(_plug)
            open(os.path.join(_plug, "main.py"), "w").write("old\n")
            _env = dict(os.environ, PATH=_bin + os.pathsep + os.environ.get("PATH", ""), SUDO_USER="deck",
                        STUB_LOG=os.path.join(_t, "log"), HUP_AFTER_ASIDE="1" if hup else "")
            _r = _rsp.run([_bash, os.path.abspath("install.sh")], env=_env, capture_output=True, text=True, timeout=60)
            _main = os.path.join(_plug, "main.py")
            return _r, (open(_main).read().strip() if os.path.isfile(_main) else None)
        _r, _got = _install(hup=True)
        ok(_got == "old", "hung up between the two moves: the installed copy is put back")
        ok(not os.path.exists(os.path.join(_home, "homebrew", ".wifi-optimizer-previous")),
           "and nothing is left aside")
        _r, _got = _install(hup=False)
        ok(_r.returncode == 0 and _got == "new", "an uninterrupted install still installs the new copy")
        shutil.rmtree(_t, ignore_errors=True)
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("R2 the VS Code deploy works against a root-owned plugin folder")
# The plugin folder belongs to root after install.sh, the updater or Decky
# 3.1.10+, the folder name has a space, and sudo over ssh needs a terminal.
try:
    _tasks = {t["label"]: t for t in json.load(open(".vscode/tasks.json"))["tasks"]}
    _defs = json.load(open(".vscode/defsettings.json"))
    ok("pnpm@10" in _tasks["pnpmsetup"]["command"] and "pnpm@9" not in _tasks["pnpmsetup"]["command"],
       "the setup task installs pnpm 10, as the README and CI use")
    _dep = _tasks["deploy"]["command"]
    ok('\\"${config:deckdir}/homebrew/plugins/${config:pluginname}/\\"' in json.dumps(_dep)
       or '"${config:deckdir}/homebrew/plugins/${config:pluginname}/"' in _dep,
       "the plugin folder, whose name has a space, is quoted")
    ok("ssh -t" in _dep and "sudo rsync" in _dep and "sudo chown -R root:root" in _dep,
       "it installs as root over an ssh session with a terminal, so sudo can ask for the password")
    ok("--exclude='*'" in _dep and all(x not in _dep for x in (".claude", ".mcp.json", ".kijito_persona", "--exclude node_modules")),
       "only the plugin's own files are copied, nothing else from the working folder")
    ok("ssh -t" in _tasks["restartdecky"]["command"], "restarting Decky also runs sudo with a terminal")
    ok("deckpass" not in _defs and "deckpass" not in json.dumps(_tasks), "no unused password setting")
    _rd = open("README.md").read()
    _bfs = _rd[_rd.index("## Building from source"):]
    _bfs = _bfs[:_bfs.index("\n## ", 5)]
    ok("pnpm 10" in _bfs and "password" in _bfs and "IP and password" not in _bfs and "sudo" in _bfs,
       "the README's build section says what the deploy needs")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("R3 install.sh's own usage line fails on an HTTP error")
try:
    _usage = [l for l in open("install.sh").read().splitlines() if l.startswith("# Usage:")]
    ok(len(_usage) == 1 and "curl -fsSL " in _usage[0] and "curl -sL " not in _usage[0],
       "the usage line uses curl -fsSL, so an error page is never run by sudo bash")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("release signatures: checked strictly, and required for every update")
try:
    import base64, hashlib
    _b64 = lambda raw: base64.b64encode(raw).decode("ascii")

    # The test's own signer, so a signature can be made for any input. Not in
    # main.py: the plugin never signs. Built on main.py's point arithmetic,
    # which the RFC vectors below check independently, and checked itself by
    # reproducing the RFC's signatures from its secret keys.
    def _ed_sign(seed, msg):
        h = hashlib.sha512(seed).digest()
        a = int.from_bytes(h[:32], "little") & ((1 << 254) - 8) | (1 << 254)
        pub = m._ed_encode(m._ed_mul(a, m._ED_B))
        r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % m._ED_L
        big_r = m._ed_encode(m._ed_mul(r, m._ED_B))
        k = int.from_bytes(hashlib.sha512(big_r + pub + msg).digest(), "little") % m._ED_L
        return pub, big_r + ((r + k * a) % m._ED_L).to_bytes(32, "little")

    def _quiet(fn, *a, **k):
        """fn's answer, or a failure that says it raised."""
        try:
            return fn(*a, **k)
        except Exception as e:
            return {"ok": None, "raised": repr(e)}

    def _flip(raw, i):
        raw = bytearray(raw); raw[i] ^= 0x01; return bytes(raw)

    # RFC 8032 section 7.1, TEST 1 to 3: (secret key, public key, message, signature).
    _RFC = [
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
         "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
         "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
         "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
         "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ]
    for _n, (_sk, _pk, _msg, _sig) in enumerate(_RFC, 1):
        _sk, _pk, _msg, _sig = (bytes.fromhex(x) for x in (_sk, _pk, _msg, _sig))
        ok(m._ed25519_verify(_pk, _msg, _sig) is True, f"RFC 8032 TEST {_n} verifies")
        _bad_msg = _msg + b"\x00" if not _msg else _flip(_msg, 0)
        ok(m._ed25519_verify(_pk, _bad_msg, _sig) is False, f"TEST {_n} with the message changed does not")
        ok(m._ed25519_verify(_pk, _msg, _flip(_sig, 5)) is False
           and m._ed25519_verify(_pk, _msg, _flip(_sig, 40)) is False,
           f"TEST {_n} with a byte of R or of S changed does not")
        ok(m._ed25519_verify(_flip(_pk, 3), _msg, _sig) is False, f"TEST {_n} under a changed key does not")
        ok(_ed_sign(_sk, _msg) == (_pk, _sig), f"the test signer reproduces TEST {_n}")

    _pk1, _sig1 = bytes.fromhex(_RFC[0][1]), bytes.fromhex(_RFC[0][3])
    _s1 = int.from_bytes(_sig1[32:], "little")
    ok(m._ed25519_verify(_pk1, b"", _sig1[:32] + (_s1 + m._ED_L).to_bytes(32, "little")) is False,
       "S + L, which is the same signature mathematically, is refused (S must be below L)")
    _p_plus_1 = (m._ED_P + 1).to_bytes(32, "little")
    ok(m._ed_decode(_p_plus_1) is None and m._ed_decode((1).to_bytes(32, "little")) is not None,
       "a y written past p is refused even where its reduction is a point")
    ok(m._ed_decode(((1 << 255) | 1).to_bytes(32, "little")) is None,
       "and so is x = 0 with its sign bit set")
    _off = next(y for y in range(2, 50) if m._ed_decode(y.to_bytes(32, "little")) is None)
    ok(m._ed25519_verify(_off.to_bytes(32, "little"), b"", _sig1) is False
       and m._ed25519_verify(_pk1, b"", _off.to_bytes(32, "little") + _sig1[32:]) is False,
       "a key or an R that is not on the curve is refused")
    ok(m._ed25519_verify(_pk1, b"", _sig1[:63]) is False and m._ed25519_verify(_pk1[:31], b"", _sig1) is False
       and m._ed25519_verify("x" * 32, b"", _sig1) is False,
       "short or wrongly typed inputs are refused")

    # A release of a fork of this plugin, signed with stock minisign
    # (prehashed, key id 72A411FD74FD614A). Interop: what minisign writes,
    # this accepts.
    _FORK_PUB = ("untrusted comment: minisign public key 72A411FD74FD614A\n"
                 "RWRypBH9dP1hSud4CEL0adABUoisOgrxh+9UrPX+DX1larLQNfIRGTgm\n")
    _FORK_SUMS = (b"aa9dbf2aa399593a6da26f9698cafc5fc7d493cf5ed6dafbadbf00ebe6674257"
                  b"  wifi-optimizer-streaming-0.15.1.zip\n")
    _FORK_SIG = (
        "untrusted comment: signature from wifi-optimizer-streaming CI\n"
        "RURypBH9dP1hSg+b1b791+S3B7U7Sh0PIaIlVYBHpZ377DqdtETw8Gm2pOztb+zFPy8y6Xg8zvjM4+M3/h9CLCJkDYsAoyGAzgU=\n"
        "trusted comment: wifi-optimizer-streaming v0.15.1\n"
        "8gNDLHgzLd4nxYpnuxaQZaSaazlXxo+5ifALNgt0OtXd/92RbuKlidb+NclYVJs0eYIv62fxA48GCSFo/jtkAQ==\n")
    _fork_key = _FORK_PUB.split("\n")[1]
    _fv = _quiet(m._minisign_verify, _FORK_SUMS, _FORK_SIG, (_FORK_PUB,))
    ok(_fv == {"ok": True, "trusted_comment": "wifi-optimizer-streaming v0.15.1"},
       f"a release signed by stock minisign verifies, both signatures and the key id ({_fv})")
    ok(_quiet(m._minisign_verify, _FORK_SUMS, _FORK_SIG, (_fork_key,)).get("ok") is True,
       "with the key given bare as well as as the whole .pub file")
    ok(m._parse_minisign_key(_FORK_PUB) == (bytes.fromhex("72a411fd74fd614a"), base64.b64decode(_fork_key)[10:]),
       "the key id read is the one minisign prints")
    _fr = _quiet(m.verify_release_manifest, _FORK_SUMS, _FORK_SIG, "0.15.1", keys=(_fork_key,))
    ok(_fr == {"ok": False, "error": "signature is for a different release"},
       f"but its trusted comment is not ours, so it is no release of this plugin ({_fr})")

    def _edit_line(text, idx, fn):
        lines = text.split("\n"); lines[idx] = fn(lines[idx]); return "\n".join(lines)
    def _edit_b64(text, idx, at):
        return _edit_line(text, idx, lambda l: _b64(_flip(base64.b64decode(l), at)))
    def _fail(r, why):
        return r.get("ok") is False and (why is None or r.get("error") == why)

    _tampered = [
        ("a byte of SHA256SUMS", _FORK_SUMS.replace(b"aa9d", b"aa9e"), _FORK_SIG, "signature does not verify"),
        ("a byte of the signature", _FORK_SUMS, _edit_b64(_FORK_SIG, 1, 30), "signature does not verify"),
        ("a byte of the global signature", _FORK_SUMS, _edit_b64(_FORK_SIG, 3, 10),
         "trusted comment signature does not verify"),
        ("the trusted comment", _FORK_SUMS, _FORK_SIG.replace("v0.15.1", "v0.15.2"),
         "trusted comment signature does not verify"),
        ("the key id in the signature", _FORK_SUMS, _edit_b64(_FORK_SIG, 1, 4), "signature is not from a pinned key"),
        ("the algorithm, prehashed to legacy", _FORK_SUMS,
         _edit_line(_FORK_SIG, 1, lambda l: _b64(b"Ed" + base64.b64decode(l)[2:])), "signature does not verify"),
    ]
    for _what, _d, _s, _why in _tampered:
        _r = _quiet(m._minisign_verify, _d, _s, (_fork_key,))
        ok(_fail(_r, _why), f"changing {_what} fails ({_r})")

    _test_seed = hashlib.sha256(b"wifi-optimizer regress test key").digest()
    _test_id = bytes.fromhex("0102030405060708")
    _test_pub = _ed_sign(_test_seed, b"")[0]
    _test_key = _b64(b"Ed" + _test_id + _test_pub)

    def _minisign(data, comment, legacy=False, seed=_test_seed, key_id=_test_id):
        signed = data if legacy else hashlib.blake2b(data, digest_size=64).digest()
        sig = _ed_sign(seed, signed)[1]
        gsig = _ed_sign(seed, sig + comment.encode("utf-8"))[1]
        return ("untrusted comment: signature from a test key\n"
                + _b64((b"Ed" if legacy else b"ED") + key_id + sig)
                + "\ntrusted comment: " + comment + "\n" + _b64(gsig) + "\n")

    _r = _quiet(m._minisign_verify, _FORK_SUMS, _FORK_SIG, (_test_key,))
    ok(_fail(_r, "signature is not from a pinned key"), f"a different pinned key does not accept it ({_r})")
    # The fork's key under another id: the signature itself is good, so only
    # matching the id refuses it.
    _relabelled = _b64(b"Ed" + _test_id + base64.b64decode(_fork_key)[10:])
    _r = _quiet(m._minisign_verify, _FORK_SUMS, _FORK_SIG, (_relabelled,))
    ok(_fail(_r, "signature is not from a pinned key"), f"the key id must match, not only the key ({_r})")
    # And the other way: the fork's id on a key that did not sign it.
    _wrong_key = _b64(b"Ed" + bytes.fromhex("72a411fd74fd614a") + _test_pub)
    _r = _quiet(m._minisign_verify, _FORK_SUMS, _FORK_SIG, (_wrong_key,))
    ok(_fail(_r, "signature does not verify"), f"an id matched to the wrong key does not verify ({_r})")
    ok(_quiet(m._minisign_verify, _FORK_SUMS, _FORK_SIG, (_test_key, _fork_key)).get("ok") is True,
       "with more than one key pinned, the one named by the id is used")

    _SUMS = (b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef  WifiOptimizer-0.20.0-beta.tar.gz\n"
             b"fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210  wifi-optimizer.zip\n")
    _good = _minisign(_SUMS, "wifi-optimizer v0.20.0-beta")
    _r = _quiet(m.verify_release_manifest, _SUMS, _good, "0.20.0-beta", keys=(_test_key,))
    ok(_r == {"ok": True, "sha256": {
        "WifiOptimizer-0.20.0-beta.tar.gz": "0123456789abcdef" * 4,
        "wifi-optimizer.zip": "fedcba9876543210" * 4}},
       f"a release signed for this version gives back its checksums ({_r})")
    _legacy = _minisign(_SUMS, "wifi-optimizer v0.20.0-beta", legacy=True)
    ok(_legacy.split("\n")[1].startswith("RWQBAgME"), "the legacy test signature really is the Ed form")
    ok(_quiet(m._minisign_verify, _SUMS, _legacy, (_test_key,)).get("ok") is True,
       "a legacy signature over the file itself is understood")
    _r = _quiet(m.verify_release_manifest, _SUMS, _legacy, "0.20.0-beta", keys=(_test_key,))
    ok(_fail(_r, "signature does not verify"),
       f"but a release manifest must use the prehashed form releases are signed with ({_r})")
    # The algorithm is outside both signatures. Relabelled as legacy, a real
    # signature is valid over the BLAKE2b hash of the file, so this is the
    # form an attacker can produce without the key.
    _relabel = _edit_line(_good, 1, lambda l: _b64(b"Ed" + base64.b64decode(l)[2:]))
    _hashed = hashlib.blake2b(_SUMS, digest_size=64).digest()
    ok(_quiet(m._minisign_verify, _hashed, _relabel, (_test_key,)).get("ok") is True,
       "a prehashed signature relabelled as legacy really does verify over the file's hash")
    _r = _quiet(m.verify_release_manifest, _hashed, _relabel, "0.20.0-beta", keys=(_test_key,))
    ok(_fail(_r, "signature does not verify"),
       f"and is refused as a manifest because of its form, not left to the checksum parser ({_r})")
    _r = _quiet(m._minisign_verify, _SUMS + b"x", _legacy, (_test_key,))
    ok(_fail(_r, "signature does not verify"), "and fails once the file changes")
    _r = _quiet(m._minisign_verify, _SUMS, _edit_line(_legacy, 1, lambda l: _b64(b"ED" + base64.b64decode(l)[2:])),
                (_test_key,))
    ok(_fail(_r, "signature does not verify"), "a legacy signature relabelled as prehashed fails")
    for _ver, _comment in [("0.20.0", "wifi-optimizer v0.20.0-beta"),
                           ("0.20.0-beta", "wifi-optimizer v0.20.0-beta "),
                           ("0.20.0-beta", "wifi-optimizer 0.20.0-beta"),
                           ("0.20.0-beta", "wifi-optimizer-streaming v0.20.0-beta"),
                           ("0.20.0-beta", "")]:
        _r = _quiet(m.verify_release_manifest, _SUMS, _minisign(_SUMS, _comment), _ver, keys=(_test_key,))
        ok(_fail(_r, "signature is for a different release"),
           f"a trusted comment of {_comment!r} is no signature for v{_ver} ({_r})")
    for _ver in ["", "0.20.0;rm", "../0.20", None, 20]:
        _r = _quiet(m.verify_release_manifest, _SUMS, _good, _ver, keys=(_test_key,))
        ok(_fail(_r, "version malformed"), f"an expected version of {_ver!r} is refused ({_r})")

    for _what, _sums in [
        ("uppercase hex", _SUMS.replace(b"abcdef", b"ABCDEF")),
        ("one space", _SUMS.replace(b"  wifi", b" wifi")),
        ("binary mode", _SUMS.replace(b"  wifi", b" *wifi")),
        ("a path", _SUMS.replace(b"  wifi", b"  dist/wifi")),
        ("a name with ..", _SUMS.replace(b"wifi-optimizer.zip", b"wifi..zip")),
        ("a name that is ..", _SUMS.replace(b"wifi-optimizer.zip", b"..")),
        ("a name starting with -", _SUMS.replace(b"wifi-optimizer.zip", b"-wifi.zip")),
        ("a duplicate", _SUMS + _SUMS.split(b"\n")[0] + b"\n"),
        ("a blank line", _SUMS + b"\n"),
        ("a short digest", _SUMS.replace(b"0123456789abcdef0123", b"0123456789abcdef012")),
        ("CRLF", _SUMS.replace(b"\n", b"\r\n")),
        ("nothing", b""),
        ("non-ASCII", _SUMS.replace(b"wifi-optimizer.zip", b"wifi-\xc3\xa9.zip")),
    ]:
        _r = _quiet(m.verify_release_manifest, _sums, _minisign(_sums, "wifi-optimizer v0.20.0-beta"),
                    "0.20.0-beta", keys=(_test_key,))
        ok(_fail(_r, "checksum list malformed"), f"a signed checksum list with {_what} is still refused ({_r})")
    _big = _SUMS * (m.RELEASE_SUMS_MAX // len(_SUMS) + 1)
    _r = _quiet(m.verify_release_manifest, _big, _minisign(_big, "wifi-optimizer v0.20.0-beta"),
                "0.20.0-beta", keys=(_test_key,))
    ok(_fail(_r, "checksum list too large"), f"an oversized checksum list is refused before anything else ({_r})")

    _lines = _good[:-1].split("\n")
    _ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    _malformed = [
        ("three lines", "\n".join(_lines[:3]) + "\n"),
        ("five lines", _good + "extra\n"),
        ("a blank line between", _good.replace("\ntrusted", "\n\ntrusted")),
        ("no untrusted comment prefix", _good.replace("untrusted comment: ", "comment: ")),
        ("no trusted comment prefix", _good.replace("trusted comment: wifi", "trusted: wifi")),
        ("CRLF line ends", _good.replace("\n", "\r\n")),
        ("a bad base64 character", _edit_line(_good, 1, lambda l: "!" + l[1:])),
        ("base64 with whitespace", _edit_line(_good, 3, lambda l: l[:10] + " " + l[10:])),
        # The last character before the padding carries bits that must be
        # zero; setting one decodes to the same bytes in a lenient decoder.
        ("non-canonical base64", _edit_line(_good, 3, lambda l: l[:-3] + _ALPHABET[_ALPHABET.index(l[-3]) ^ 1] + "==")),
        ("a signature line one byte short", _edit_line(_good, 1, lambda l: _b64(base64.b64decode(l)[:-1]))),
        ("a signature line one byte long", _edit_line(_good, 1, lambda l: _b64(base64.b64decode(l) + b"\x00"))),
        ("a global signature one byte short", _edit_line(_good, 3, lambda l: _b64(base64.b64decode(l)[:-1]))),
        ("an unknown algorithm", _edit_line(_good, 1, lambda l: _b64(b"Ex" + base64.b64decode(l)[2:]))),
        ("an oversized comment", _good.replace("a test key", "x" * m.RELEASE_SIG_MAX)),
        ("nothing", ""),
        ("bytes, not text", _good.encode()),
        ("None", None),
        ("a lone surrogate in the comment", _good.replace("v0.20.0-beta", "v0.20.0-beta\udc80")),
    ]
    for _what, _s in _malformed:
        _r = _quiet(m.verify_release_manifest, _SUMS, _s, "0.20.0-beta", keys=(_test_key,))
        ok(_fail(_r, None), f"a signature file with {_what} fails without raising ({_r})")
    for _what, _keys in [("a key that is not base64 of the right length", ("RWQ" + "A" * 52,)),
                         ("a key of zeros", ("RWQ" + "A" * 53,)), ("a key of the wrong type", (b"RWQ",)),
                         ("a key with a three-line file", ("a\nb\n" + _test_key,)), ("keys not iterable", 5)]:
        _r = _quiet(m.verify_release_manifest, _SUMS, _good, "0.20.0-beta", keys=_keys)
        ok(_fail(_r, None), f"{_what} fails without raising ({_r})")
    ok(_fail(_quiet(m.verify_release_manifest, "text", _good, "0.20.0-beta", keys=(_test_key,)), None),
       "a checksum list given as text fails without raising")

    # The keys releases are checked against. Pinned as strings so a person
    # can compare them with the ones SECURITY.md publishes.
    _MAIN_KEY = "RWT6RHA7kc0N/vvpQCVTRBrSXNZpv7hBFPZpSRX9m5v55Mrl15BB1heh"
    _RESCUE_KEY = "RWRD+69SF7mzDbaCBVP3Pj5/5sOngkf/rVfOxCocbSTf+H8OP115i+8g"
    ok(m.TRUSTED_RELEASE_KEYS == (_MAIN_KEY, _RESCUE_KEY),
       "exactly the release key and the rescue key are pinned, in that order")
    _ids = [m._parse_minisign_key(k) for k in m.TRUSTED_RELEASE_KEYS]
    ok(all(_ids) and [k[0][::-1].hex().upper() for k in _ids] == ["FE0DCD913B7044FA", "0DB3B91752AFFB43"],
       "each parses, with the key id minisign prints for it")
    _sec = open("SECURITY.md").read()
    ok(all(k in _sec for k in (_MAIN_KEY, _RESCUE_KEY, "FE0DCD913B7044FA", "0DB3B91752AFFB43")),
       "and SECURITY.md publishes both, with their ids")

    # Real minisign 0.12 output from a throwaway key whose secret is gone:
    # what the release script will write, for version 9.9.9.
    _MS_PUB = "RWR6Nddm88NQqXySQcM4BmPoVCdHizp6GZ4SH+tQc0O5G83P2KNMJrcN"
    _MS_SUMS = b"a" * 64 + b"  wifi-optimizer-v9.9.9.tar.gz\n"
    _MS_SIG = (
        "untrusted comment: signature from minisign secret key\n"
        "RUR6Nddm88NQqVx4Pf2QGttkF6nwIZURt4EVgcox3s4vEkXFLx9MMQNX4QMAZOYovSKYdsArO/QyR7NAaQWXUx9XHuc+fymGPQI=\n"
        "trusted comment: wifi-optimizer v9.9.9\n"
        "Cgqm3jP786tXixb9l2myCOKJhya+Y5a6Yn8VwerdwZKtUohAjCrr9pQprdWtGBE6kDmnp3fwAs79t2IuftsgDg==\n")
    _r = _quiet(m.verify_release_manifest, _MS_SUMS, _MS_SIG, "9.9.9", keys=(_MS_PUB,))
    ok(_r == {"ok": True, "sha256": {"wifi-optimizer-v9.9.9.tar.gz": "a" * 64}},
       f"a release manifest made by minisign 0.12 verifies, in the format the release script writes ({_r})")
    _r = _quiet(m.verify_release_manifest, _MS_SUMS, _MS_SIG, "9.9.9")
    ok(_fail(_r, "signature is not from a pinned key"), f"but not under the pinned keys ({_r})")

    for _r in (_quiet(m.verify_release_manifest, _SUMS, _good, "0.20.0-beta"),
               _quiet(m.verify_release_manifest, _FORK_SUMS, _FORK_SIG, "0.15.1")):
        ok(_fail(_r, "signature is not from a pinned key"),
           f"nor does anything signed by another key ({_r})")
    _r = _quiet(m.verify_release_manifest, _SUMS, _good, "0.20.0-beta", keys=())
    ok(_r == {"ok": False, "error": "no release keys pinned"}, f"with no keys pinned, everything fails closed ({_r})")
    _real_keys = m.TRUSTED_RELEASE_KEYS
    try:
        m.TRUSTED_RELEASE_KEYS = ()
        _r = _quiet(m.verify_release_manifest, _SUMS, _good, "0.20.0-beta")
        ok(_r == {"ok": False, "error": "no release keys pinned"},
           f"including when the pinned list itself is empty, read at call time ({_r})")
    finally:
        m.TRUSTED_RELEASE_KEYS = _real_keys

    # Releases from 0.24.3 list the Decky zip beside the tarball. Every
    # installed updater reads this file, so a second line must stay normal.
    _two = (b"a" * 64 + b"  wifi-optimizer-v0.24.3.tar.gz\n"
            + b"b" * 64 + b"  wifi-optimizer.zip\n")
    ok(m._parse_sha256sums(_two) == {"wifi-optimizer-v0.24.3.tar.gz": "a" * 64,
                                     "wifi-optimizer.zip": "b" * 64},
       "a checksum list with the tarball and the zip parses, each by its name")

    # Used, and only where it should be: the updater, through _signed_release.
    _vtree = ast.parse(open("main.py").read())
    _vfuncs = {"verify_release_manifest", "_minisign_verify", "_parse_minisig", "_parse_minisign_key",
               "_parse_sha256sums", "_ed25519_verify", "_b64_exact",
               "_ed_add", "_ed_mul", "_ed_encode", "_ed_decode"}
    _defined = {n.name for n in _vtree.body if isinstance(n, ast.FunctionDef)}
    ok(_vfuncs <= _defined, f"the verifier's functions are all at module level ({sorted(_vfuncs - _defined)})")
    _users = set()
    for _cls in (n for n in _vtree.body if isinstance(n, ast.ClassDef)):
        for _fn in (n for n in _cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            if any(isinstance(s, ast.Name) and s.id == "verify_release_manifest" for s in ast.walk(_fn)):
                _users.add(_fn.name)
    ok(_users == {"_signed_release"}, f"the plugin checks signatures in one place ({sorted(_users)})")
    _main_src = open("main.py").read()
    _au = _main_src[_main_src.index("    async def apply_update"):]
    _au = _au[:_au.index("\n    def _launch_updater")]
    ok("await self._signed_release(latest)" in _au and 'if not signed["ok"]:' in _au
       and _au.index("await self._signed_release(latest)") < _au.index('if not signed["ok"]:')
       < _au.index("self._launch_updater"),
       "apply_update checks the signature, and stops on a failure, before it launches anything")
    ok('download_url = signed["url"]' in _au and 'sha256 = signed["sha256"]' in _au
       and "latest, sha256]" in _au,
       "and what it downloads and the sha256 it hands the script both come from the signed list")
    ok("archive/refs/heads" not in _main_src and "archive/refs/tags" not in _main_src
       and "contents/package.json" not in _main_src,
       "no download of a branch or tag archive, and no version read from a branch, is left")
    _scr = m.UPDATE_SCRIPT
    ok('EXPECTED_SHA="${7:-}"' in _scr
       and _scr.index('if [ "${#EXPECTED_SHA}" != 64 ]') < _scr.index("set_state downloading"),
       "the update script refuses to start without a sha256 to check against")
    ok(_scr.index('if [ "$SUM" != "$EXPECTED_SHA" ]') < _scr.index("tar xzf"),
       "and compares the payload with it before extracting anything")
    _elsewhere = open("install.sh").read() + "".join(
        open(os.path.join(dp, f)).read() for dp, _dn, fs in os.walk("src") for f in fs)
    ok("minisig" not in _elsewhere and "verify_release" not in _elsewhere,
       "the panel and the installer do not handle signatures: the updater in the backend does")
    _vblock = _main_src[_main_src.index("# Release signatures."):_main_src.index("# The update script, run as root")]
    ok(all(ord(ch) < 128 for ch in _vblock), "the verifier is plain ASCII")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("updates: the beta channel takes the highest release")
try:
    class Releases(m.Plugin):
        """GitHub's API answering with a fixed list of releases."""
        def __init__(self, releases, raw=None):
            super().__init__(); self.releases = releases; self.raw = raw; self.cmds = []
        def _run_cmd(self, cmd, timeout=5):
            self.cmds.append(cmd)
            out = self.raw if self.raw is not None else json.dumps(self.releases)
            return {"success": True, "stdout": out, "stderr": "", "returncode": 0}

    def _rel(tag, draft=False, pre=None):
        return {"tag_name": tag, "draft": draft, "prerelease": ("-" in str(tag)) if pre is None else pre}

    def _beta_check(releases, current="0.20.0-beta", raw=None):
        m.decky.DECKY_PLUGIN_VERSION = current
        m._save_settings({**m.DEFAULT_SETTINGS, "update_channel": "beta"})
        rc = Releases(releases, raw)
        return asyncio.run(rc.check_for_update()), rc

    _real_ver = m.decky.DECKY_PLUGIN_VERSION
    try:
        _r, _rc = _beta_check([_rel("v0.20.0-beta")])
        _c = _rc.cmds[0] if _rc.cmds else []
        ok(_c[-1:] == ["https://api.github.com/repos/ArcadaLabs-Jason/WifiOptimizer/releases?per_page=30"]
           and "Accept: application/vnd.github.v3+json" in _c,
           f"the beta channel asks for the list of releases ({_c[-1:]})")
        ok(_c[:1] == ["/usr/bin/curl"] and "=https" in _c and "--max-time" in _c and "--max-filesize" in _c,
           "over https only, with a time limit and a size limit")
        ok(_r.get("success") is True and _r.get("update_available") is False,
           "the version already installed is not offered again")

        _set = [_rel("v0.20.0-beta"), _rel("v0.21.0-beta"), _rel("v0.19.2"), _rel("v0.20.1-beta")]
        for _order in (_set, _set[::-1], _set[2:] + _set[:2]):
            _r, _ = _beta_check(_order)
            ok(_r.get("latest_version") == "0.21.0-beta" and _r.get("update_available") is True,
               f"the highest version wins whatever order GitHub lists them in ({[x['tag_name'] for x in _order]})")
        _r, _ = _beta_check([_rel("v0.21.0-beta"), _rel("v0.21.0"), _rel("v0.20.5-beta")])
        ok(_r.get("latest_version") == "0.21.0" and not _r.get("is_downgrade"),
           "a stable release newer than every beta is the one picked")
        _r, _ = _beta_check([_rel("v9.0.0", draft=True), _rel("v0.21.0-beta")])
        ok(_r.get("latest_version") == "0.21.0-beta", "a draft is ignored")
        _bad = [_rel("0.30.0"), _rel("v0.30;rm"), _rel("v" + "1." * 40 + "1"), _rel(None), _rel(5),
                _rel("latest"), _rel("v0.30.0-"), _rel("v../0.30"), _rel("V0.30.0"), "v0.30.0", None,
                {"draft": False}]
        _r, _ = _beta_check(_bad + [_rel("v0.21.0-beta")])
        ok(_r.get("latest_version") == "0.21.0-beta", f"tags that are not v and a version are ignored ({_r})")
        _r, _ = _beta_check(_bad)
        ok(_r.get("success") is False and _r.get("update_available") is False,
           "and a list with nothing usable offers nothing")
        _r, _ = _beta_check([])
        ok(_r.get("success") is False and _r.get("update_available") is False
           and "didn't list a release" in (_r.get("message") or ""),
           f"an empty list is handled, and said ({_r.get('message')!r})")
        _r, _ = _beta_check(None, raw=json.dumps(
            {"message": "API rate limit exceeded for 203.0.113.7. (But here's the good news...)"}))
        ok(_r.get("success") is False and "203.0.113" not in (_r.get("message") or "")
           and "limiting update checks" in (_r.get("message") or ""),
           f"GitHub's rate limit is said plainly, without the address it names ({_r.get('message')!r})")
        _r, _ = _beta_check(None, raw="[" + " " * (m.RELEASES_LIST_MAX + 1) + "]")
        ok(_r.get("success") is False and _r.get("update_available") is False,
           "an answer too large to be real is not parsed")
        _r, _ = _beta_check([_rel("v0.19.0")], current="0.20.0-beta")
        ok(_r.get("update_available") is True and _r.get("is_downgrade") is True,
           "when only an older release is published, beta still offers it, labelled as going back")

        m.decky.DECKY_PLUGIN_VERSION = "0.19.0"
        m._save_settings({**m.DEFAULT_SETTINGS, "update_channel": "stable"})
        _rc = Releases(None, raw=json.dumps({"tag_name": "v0.20.0", "draft": False}))
        _r = asyncio.run(_rc.check_for_update())
        ok(_rc.cmds and _rc.cmds[0][-1] == "https://api.github.com/repos/ArcadaLabs-Jason/WifiOptimizer/releases/latest"
           and _r.get("latest_version") == "0.20.0" and _r.get("update_available") is True,
           "the stable channel still asks for the latest release, which GitHub never answers with a beta")
        _rc = Releases(None, raw=json.dumps({"tag_name": "v0.21.0-beta"}))
        _r = asyncio.run(_rc.check_for_update())
        ok(_r.get("update_available") is False, "and still refuses a prerelease that comes back anyway")
        _rc = Releases(None, raw=json.dumps([_rel("v0.21.0")]))
        _r = asyncio.run(_rc.check_for_update())
        ok(_r.get("success") is False and _r.get("update_available") is False,
           "a list where one release was expected offers nothing, without raising")
    finally:
        m.decky.DECKY_PLUGIN_VERSION = _real_ver
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("updates: nothing is installed without a valid signature")
try:
    import shutil
    _DL = "https://github.com/ArcadaLabs-Jason/WifiOptimizer/releases/download"
    _VER = "0.21.0-beta"
    _TAR = f"wifi-optimizer-v{_VER}.tar.gz"
    _TAR_SHA = hashlib.sha256(b"the release tarball").hexdigest()
    _REL_SUMS = f"{_TAR_SHA}  {_TAR}\n".encode()
    _REL_SIG = _minisign(_REL_SUMS, f"wifi-optimizer v{_VER}")

    class Release(m.Plugin):
        """A release on GitHub, served by a curl that writes where it is told."""
        def __init__(self, files, channel="beta", version=_VER):
            super().__init__(); self.files = files; self.channel = channel; self.version = version
            self.cmds = []; self.launched = []
        async def check_for_update(self):
            return {"update_available": True, "channel": self.channel, "latest_version": self.version}
        def _run_cmd(self, cmd, timeout=5):
            self.cmds.append(cmd)
            body = self.files.get(cmd[-1])
            if body is None:
                return {"success": False, "stdout": "404", "stderr": "error: 404", "returncode": 22}
            if body == "down":
                return {"success": False, "stdout": "000", "stderr": "could not resolve", "returncode": 6}
            open(cmd[cmd.index("-o") + 1], "wb").write(body)
            return {"success": True, "stdout": "200", "stderr": "", "returncode": 0}
        def _launch_updater(self, argv):
            self.launched.append(argv)

    def _files(sums=_REL_SUMS, sig=_REL_SIG, version=_VER):
        out = {}
        if sums is not None: out[f"{_DL}/v{version}/SHA256SUMS"] = sums
        if sig is not None:
            out[f"{_DL}/v{version}/SHA256SUMS.minisig"] = sig.encode() if isinstance(sig, str) and sig != "down" else sig
        return out

    def _apply(rel, approved=None):
        r = asyncio.run(rel.apply_update(rel.version if approved is None else approved))
        if getattr(rel, "_update_dir", None) and os.path.isdir(rel._update_dir):
            shutil.rmtree(rel._update_dir)
        rel._update_dir = None
        return r

    _real_keys = m.TRUSTED_RELEASE_KEYS
    _real_delay = m.RELEASE_FETCH_RETRY_DELAY
    try:
        m.TRUSTED_RELEASE_KEYS = (_test_key,)
        m.RELEASE_FETCH_RETRY_DELAY = 0
        _rel_ok = Release(_files())
        _r = _apply(_rel_ok)
        _argv = _rel_ok.launched[0] if _rel_ok.launched else []
        ok(_r.get("success") is True and _r.get("version") == _VER, f"a correctly signed release starts ({_r})")
        ok(_argv[:1] == ["/bin/bash"] and _argv[4:9] == [f"{_DL}/v{_VER}/{_TAR}", f"WifiOptimizer-{_VER}",
                                                         f"beta v{_VER}", _VER, _TAR_SHA],
           f"the script gets the release's tarball, its directory and its signed sha256 ({_argv[4:]})")
        _gets = [c for c in _rel_ok.cmds if c[:1] == ["/usr/bin/curl"]]
        ok([c[-1] for c in _gets] == [f"{_DL}/v{_VER}/SHA256SUMS", f"{_DL}/v{_VER}/SHA256SUMS.minisig"],
           "the checksum list and its signature are fetched for that release's tag")
        ok(all("--fail" in c and "=https" in c and "--max-filesize" in c and "--max-time" in c for c in _gets),
           "each over https only, failing on an HTTP error, bounded in size and time")
        ok(not _rel_ok._update_running(), "and the claim is released afterwards")
        _st = Release(_files(), channel="stable")
        _r = _apply(_st)
        # The label follows the release, not the channel (L27), so only the
        # asset and the directory are compared here.
        ok(_st.launched and _st.launched[0][4:6] == [f"{_DL}/v{_VER}/{_TAR}", f"WifiOptimizer-{_VER}"],
           "stable fetches the same release asset and unpacks the same directory")

        # What installs is what the user approved. GitHub is asked again
        # when they press the button, and a different answer, an older
        # signed release above all, must not be installed in its place.
        _changed = "The available update changed. Check again and retry."
        _again = "Check for updates again, then retry."
        for _what, _approved, _msg in [
            ("an approval for an older version", "0.20.0-beta", _changed),
            ("an approval for a newer version", "0.22.0", _changed),
            ("an empty approval", "", _again),
            ("a malformed approval", "0.21.0-beta;rm", _again),
            ("an overlong approval", "1." * 40 + "1", _again),
            ("an approval that is not text", 21, _again),
        ]:
            _rel = Release(_files())
            _r = _apply(_rel, _approved)
            ok(_r.get("success") is False and _r.get("message") == _msg and not _rel.launched
               and not _rel.cmds and not _rel._update_running() and _rel._update_starting is False,
               f"{_what}: refused before anything is fetched, the claim released ({_r.get('message')!r})")
        _rel = Release(_files())
        _r = asyncio.run(_rel.apply_update())
        ok(_r.get("success") is False and _r.get("message") == _again and not _rel.launched
           and not _rel._update_running(), "no approval at all: refused the same way")
        _tsx = open("src/index.tsx").read()
        ok('backend.applyUpdate(updateInfo?.latest_version ?? "")' in _tsx
           and 'callable<[version: string], MethodResult>("apply_update")' in open("src/backend.ts").read()
           and 'applyUpdate(updateInfo?.latest_version ?? "")' in open("dist/index.js").read(),
           "the panel passes the version it showed, and the built bundle does too")

        _wrong_comment = _minisign(_REL_SUMS, "wifi-optimizer v0.20.0-beta")
        _no_tar = f"{_TAR_SHA}  wifi-optimizer-v0.20.0-beta.tar.gz\n".encode()
        _cases = [
            ("no signature file", _files(sig=None), "This release isn't signed, so it wasn't installed."),
            ("no checksum list", _files(sums=None), "This release isn't signed, so it wasn't installed."),
            ("a signature that does not verify", _files(sig=_edit_b64(_REL_SIG, 1, 30)),
             "This release's signature didn't check out, so it wasn't installed."),
            ("a checksum list changed after signing", _files(sums=_REL_SUMS.replace(b"  ", b"  x")),
             "This release's signature didn't check out, so it wasn't installed."),
            ("a signature for another version", _files(sig=_wrong_comment),
             "This release's signature didn't check out, so it wasn't installed."),
            ("a signature by a key that is not pinned", _files(sig=_MS_SIG),
             "This release's signature didn't check out, so it wasn't installed."),
            ("a signature file that is not text", _files(sig=b"\xff\xfe" + _REL_SIG.encode()),
             "This release's signature didn't check out, so it wasn't installed."),
            ("a signed list without the tarball", _files(sums=_no_tar, sig=_minisign(_no_tar, f"wifi-optimizer v{_VER}")),
             "This release's signed checksums don't cover the update, so it wasn't installed."),
            ("the signature not downloadable", _files(sig="down"),
             "Couldn't download the release's signature. Try again later."),
            ("an oversized checksum list", _files(sums=b"x" * (m.RELEASE_SUMS_MAX + 1)),
             "Couldn't download the release's signature. Try again later."),
        ]
        for _what, _fs, _msg in _cases:
            _rel = Release(_fs)
            _r = _apply(_rel)
            ok(_r.get("success") is False and _r.get("message") == _msg and not _rel.launched
               and not _rel._update_running() and _rel._update_starting is False,
               f"{_what}: refused, nothing launched, the claim released ({_r.get('message')!r})")
        ok(all(ord(ch) < 128 for _w, _f, _msg in _cases for ch in _msg),
           "every refusal is plain ASCII")

        _rel = Release(_files(), version="0.21;rm")
        _r = _apply(_rel)
        ok(_r.get("success") is False and not _rel.launched and not _rel.cmds,
           "a version that is not one is refused before anything is fetched")

        # A slow first connection to GitHub made a real update fail once
        # (connect timeout 5 s, no second try). Each try now waits as long
        # as the payload download does, and a network error gets one more.
        _sig_url = f"{_DL}/v{_VER}/SHA256SUMS.minisig"
        ok(all(c[c.index("--connect-timeout") + 1] == "15" and int(c[c.index("--max-time") + 1]) <= 60
               for c in _gets),
           "each fetch waits 15 s to connect, like the payload download, and at most a minute in all")

        class Flaky(Release):
            """GitHub's download host failing the first tries of some files."""
            def __init__(self, files, fails):
                super().__init__(files); self.fails = dict(fails)
            def _run_cmd(self, cmd, timeout=5):
                left = self.fails.get(cmd[-1])
                if left:
                    self.fails[cmd[-1]] = (left[0] - 1, left[1])
                    if left[0] > 0:
                        self.cmds.append(cmd)
                        return left[1]
                return super()._run_cmd(cmd, timeout)

        _timed_out = {"success": False, "stdout": "000", "returncode": 28,
                      "stderr": "curl: (28) Connection timed out after 15001 milliseconds"}
        _rel = Flaky(_files(), {_sig_url: (1, _timed_out)})
        _r = _apply(_rel)
        ok(_r.get("success") is True and [c[-1] for c in _rel.cmds].count(_sig_url) == 2,
           f"a connect timeout on the signature is tried once more, and the update goes ahead ({_r})")
        _rel = Flaky(_files(), {_sig_url: (5, _timed_out)})
        _r = _apply(_rel)
        ok(_r.get("message") == "Couldn't download the release's signature. Try again later."
           and [c[-1] for c in _rel.cmds].count(_sig_url) == 2 and not _rel.launched,
           "but only once: a host still unreachable is refused after the second try")
        for _what, _fail in [
            ("a 404, which means the release is unsigned", {"success": False, "stdout": "404", "returncode": 22, "stderr": ""}),
            ("a server error", {"success": False, "stdout": "500", "returncode": 22, "stderr": ""}),
            ("a file over the size cap", {"success": False, "stdout": "200", "returncode": 63, "stderr": ""}),
        ]:
            _rel = Flaky(_files(), {_sig_url: (5, _fail)})
            _r = _apply(_rel)
            ok(_r.get("success") is False and [c[-1] for c in _rel.cmds].count(_sig_url) == 1
               and not _rel.launched, f"{_what}: not tried again ({_r.get('message')!r})")
        _rel = Flaky(_files(), {_sig_url: (5, _timed_out)})
        _got = _rel._fetch_release_file(_sig_url, m.RELEASE_SIG_MAX, time.monotonic() + 5)
        _c = _rel.cmds[0] if _rel.cmds else []
        ok(_got == {"data": None, "missing": False} and len(_rel.cmds) == 1
           and _c[_c.index("--max-time") + 1] == "10",
           "with the budget nearly spent there is no second try, and the last one still gets 10 s")
        _ui_ms = int(re.search(r"UPDATE_TIMEOUT_MS = (\d+) \* (\d+) \* (\d+);", _tsx).group(1)) * 60 * 1000
        ok(m.RELEASE_FETCH_BUDGET + 10 + 15 <= _ui_ms / 1000 / 2,
           f"the whole signature step ({m.RELEASE_FETCH_BUDGET} s + a last try) fits in half the panel's timeout")

        m.TRUSTED_RELEASE_KEYS = ()
        _rel = Release(_files())
        _r = _apply(_rel)
        ok(_r.get("success") is False and not _rel.launched and not _rel._update_running(),
           "with no key pinned, even a well-formed release is refused: the updater fails closed")
    finally:
        m.TRUSTED_RELEASE_KEYS = _real_keys
        m.RELEASE_FETCH_RETRY_DELAY = _real_delay
    _rel = Release(_files())
    _r = _apply(_rel)
    ok(_r.get("success") is False and not _rel.launched,
       "and a release signed by anything but the pinned keys is refused by the plugin as shipped")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("updates: telling people one is out, from a check that runs with the panel closed")
try:
    class Notice(m.Plugin):
        """check_for_update answering as given, and a stream running or not."""
        def __init__(self, info, stream=False):
            super().__init__(); self.info = info; self.stream = stream; self.checks = 0
        async def check_for_update(self):
            self.checks += 1
            return dict(self.info)
        def _stream_running(self, settings): return self.stream

    _new = {"success": True, "update_available": True, "is_downgrade": False,
            "latest_version": "0.24.0", "current_version": "0.23.0", "channel": "stable"}
    ok(m.DEFAULT_SETTINGS.get("update_notify") is True and m.DEFAULT_SETTINGS.get("update_notified_version") == "",
       "on by default, with nothing announced yet")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _n = Notice(_new)
    _r = asyncio.run(_n.update_notice())
    ok(_r == {"notify": True, "version": "0.24.0", "busy": False}, f"a newer release is announced ({_r})")
    ok(asyncio.run(_n.mark_update_notified("0.24.0")).get("success") is True
       and m._load_settings().get("update_notified_version") == "0.24.0",
       "and once shown it is recorded")
    _r = asyncio.run(_n.update_notice())
    ok(_r.get("notify") is False, "so the same version is not announced again, after a reload or a reboot")
    _r = asyncio.run(Notice({**_new, "latest_version": "0.25.0"}).update_notice())
    ok(_r.get("notify") is True and _r.get("version") == "0.25.0", "but the next release is")
    for _what, _info in [
        ("nothing newer", {**_new, "update_available": False}),
        ("a beta channel offering to go back", {**_new, "is_downgrade": True, "latest_version": "0.22.0"}),
    ]:
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _r = asyncio.run(Notice(_info).update_notice())
        ok(_r.get("notify") is False and not _r.get("retry"), f"{_what}: nothing to announce")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _r = asyncio.run(Notice({"success": False, "update_available": False}).update_notice())
    ok(_r == {"notify": False, "retry": True}, "a failed check is retried sooner, without a notice")
    _r = asyncio.run(Notice(_new, stream=True).update_notice())
    ok(_r.get("notify") is True and _r.get("busy") is True, "during a stream it says so, so the toast waits")
    m._save_settings({**m.DEFAULT_SETTINGS, "update_notify": False})
    _n = Notice(_new)
    _r = asyncio.run(_n.update_notice())
    ok(_r == {"notify": False} and _n.checks == 0, "turned off, GitHub is not even asked")
    ok(asyncio.run(_n.set_update_notify(True)).get("success") is True
       and m._load_settings().get("update_notify") is True, "and the toggle turns it back on")
    for _bad in ["", "0.24.0;rm", "1." * 40 + "1", 24, None]:
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _r = asyncio.run(Notice(_new).mark_update_notified(_bad))
        ok(_r.get("success") is False and m._load_settings().get("update_notified_version") == "",
           f"{_bad!r} is not recorded as a version")
    ok(m._clean_settings({"update_notified_version": "0.24.0-beta"})["update_notified_version"] == "0.24.0-beta"
       and m._clean_settings({"update_notified_version": "v0.24;x"})["update_notified_version"] == ""
       and m._clean_settings({"update_notify": "yes"})["update_notify"] is True,
       "the settings file keeps a real version and a real on/off, and nothing else")

    _uw = open("src/updateWatch.ts").read(); _ix = open("src/index.tsx").read(); _dist = open("dist/index.js").read()
    _once = _uw[_uw.index("async function noticeOnce"):_uw.index("export function startUpdateNotices")]
    ok(_once.index("notice.busy || gameRunning()") < _once.index("toaster.toast(")
       < _once.index("backend.markUpdateNotified(notice.version)"),
       "the toast waits for a game or stream to end, and the version is recorded only once it is shown")
    ok("shared().noticeShown === notice.version" in _once and "onClick: openPanel" in _once,
       "a second copy of the module does not toast twice, and tapping it opens the panel")
    ok("NOTICE_EVERY_MS = 12 * 60 * 60 * 1000" in _uw and "NOTICE_FIRST_MS = 2 * 60 * 1000" in _uw,
       "checked a couple of minutes after load, then twice a day")
    ok("const stopUpdateNotices = startUpdateNotices();" in _ix and "stopUpdateNotices();" in _ix,
       "started with the plugin, not the panel, and stopped when Decky unloads it")
    ok("Update available: v" in _dist and "Tell me about updates" in _dist,
       "the built bundle has the notice and the toggle")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("panel copy found in the visual review")
try:
    _ix = open("src/index.tsx").read(); _sg = open("src/components/StatsGrid.tsx").read()
    _uw = open("src/updateWatch.ts").read(); _dist = open("dist/index.js").read()
    ok("the band preference, the IPv6 setting" in _ix and "the 5 GHz preference" not in _ix,
       "the reset confirmation names the band preference, which can now be 6 GHz")
    ok("speedLabel(live.tx_bitrate)" in _sg and "Mbit/s" in _dist,
       "the speed tile shows the rate, not iw's whole modulation string")
    _body = _uw[_uw.index("toaster.toast({", _uw.index("async function noticeOnce")):]
    _body = _body[:_body.index("});")]
    ok("body: `Update available: v${notice.version}`" in _body,
       "the update toast fits on the one line Steam gives it")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("QA gate fixes (2026-10-20)")
try:
    _ix = open("src/index.tsx").read(); _uw = open("src/updateWatch.ts").read()
    _src = open("main.py").read(); _script = m.UPDATE_SCRIPT
    ok(_script.index("trap cleanup EXIT") < _script.index("TMP=$(mktemp -d) || exit 1")
       and '[ -n "$TMP" ] && rm -rf "$TMP"' in _script,
       "the update script sets its cleanup before its first step that can fail, so no stray directory says an update is running")
    ok("--max-filesize 52428800" in _script, "the payload download is bounded in size, not only in time")
    ok(not m.VERSION_RE.match("١.٢.٣") and m.VERSION_RE.match("0.24.1-beta"),
       "a version is ASCII digits only")
    ok('handleToggle("band_5ghz"' in _ix and 'handleToggle("band_6ghz"' in _ix
       and 'handleToggle("band_preference"' not in _ix and _ix.count("clearBandMessages();") == 2,
       "each band toggle reports under its own key and clears the other's stale result")
    ok("isBusy={isBusy || updating}" in _ix, "Reset and Force Reapply wait while an update installs")
    ok("UPDATE_TIMEOUT_MS = 15 * 60 * 1000" in _ix, "the panel's update timeout outlasts a slow update")
    _catch = _ix[_ix.index("const handleApplyUpdate"):]
    _catch = _catch[_catch.index("} catch {"):]
    _catch = _catch[:_catch.index("\n  };")]
    ok("watchUpdate(updateInfo.latest_version, true)" in _catch,
       "a lost reply still follows the update, so the plugin is reloaded rather than Steam restarted")
    ok("current = null;" in _uw[_uw.index('if (progress.phase === "failed" || progress.phase === "done") {'):],
       "a finished update's result is told once, not replayed to every panel opened later")

    class Busy(m.Plugin):
        def __init__(self): super().__init__(); self.checked = 0
        def _update_running(self): return True
        async def check_for_update(self):
            self.checked += 1; return {"success": True, "update_available": True, "latest_version": "9.9.9"}
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _b = Busy()
    ok(asyncio.run(_b.update_notice()) == {"notify": False} and _b.checked == 0,
       "no update notice while an update installs")
    _r = asyncio.run(_b.reset_settings())
    ok(_r.get("success") is False and os.path.exists(m.SETTINGS_FILE),
       f"reset waits for an update to finish instead of being cut off by its reload ({_r.get('message')!r})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("update script: the payload must match its signed checksum, run for real")
try:
    import io, tarfile, subprocess as _sp, shutil
    # The real script under bash, with the commands that would touch the
    # network or the system replaced by shell functions: curl copies a local
    # tarball, tar notes that it ran, logger writes to a file. The paths it
    # keeps in /run are moved into a scratch directory, the one edit made to
    # the script's text.
    _fx = tempfile.mkdtemp()
    _payload = os.path.join(_fx, "payload.tar.gz")
    with tarfile.open(_payload, "w:gz") as _tf:
        for _name, _body in [("plugin.json", "{}"), ("main.py", "# new\n"), ("decky.pyi", ""),
                             ("dist/index.js", "// new\n"), ("dist/index.js.map", "{}"),
                             ("defaults/dispatcher.sh.tmpl", "#!/bin/sh\n"),
                             ("package.json", '{\n  "version": "9.9.9"\n}\n')]:
            _data = _body.encode()
            _info = tarfile.TarInfo(f"WifiOptimizer-9.9.9/{_name}"); _info.size = len(_data)
            _tf.addfile(_info, io.BytesIO(_data))
    _payload_sha = hashlib.sha256(open(_payload, "rb").read()).hexdigest()
    _STUBS = r'''
curl() { : > "$FX/curl-ran"; local o=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && o="$2"; shift; done; cp "$FX/payload.tar.gz" "$o"; }
tar() { : > "$FX/tar-ran"; command tar "$@"; }
logger() { echo "$*" >> "$FX/log"; }
sleep() { :; }
systemctl() { echo "$*" >> "$FX/systemctl"; return 0; }
pgrep() { [ -n "${PGREP_HOOK:-}" ] && eval "$PGREP_HOOK"; [ "${PGREP_RC:-1}" = 0 ] && echo 99999; return "${PGREP_RC:-1}"; }
export -f curl tar logger sleep systemctl pgrep
exec /bin/bash "$0" "$@"
'''

    def _run_script(sha_args, installed=True, edits=(), env=()):
        """installed: the plugin's directory still has its plugin.json, as
        one Decky has not uninstalled does. edits: (old, new) changes to the
        script's text. env: extra variables, for the pgrep stub."""
        for _f in ("curl-ran", "tar-ran", "log", "systemctl"):
            if os.path.exists(os.path.join(_fx, _f)): os.remove(os.path.join(_fx, _f))
        run_dir = tempfile.mkdtemp()
        self_dir = tempfile.mkdtemp(); plugin = tempfile.mkdtemp()
        open(os.path.join(plugin, "main.py"), "w").write("# old\n")
        if installed:
            open(os.path.join(plugin, "plugin.json"), "w").write("{}")
        script = os.path.join(self_dir, "update.sh")
        text = m.UPDATE_SCRIPT.replace("/run/wifi-optimizer", os.path.join(run_dir, "wo"))
        for old, new in edits:
            text = text.replace(old, new)
        open(script, "w").write(text)
        p = _sp.run(["/bin/bash", "-c", _STUBS, script, self_dir, plugin,
                     "https://example.invalid/wifi-optimizer-v9.9.9.tar.gz", "WifiOptimizer-9.9.9",
                     "v9.9.9", "9.9.9", *sha_args],
                    capture_output=True, text=True, timeout=60,
                    env={**os.environ, "FX": _fx, "PLUGIN": plugin, **dict(env)})
        log = open(os.path.join(_fx, "log")).read() if os.path.exists(os.path.join(_fx, "log")) else ""
        state_file = os.path.join(run_dir, "wo", "update-state")
        state = open(state_file).read().strip() if os.path.exists(state_file) else ""
        restarted = os.path.exists(os.path.join(_fx, "systemctl")) and \
            "restart plugin_loader" in open(os.path.join(_fx, "systemctl")).read()
        out = {"rc": p.returncode, "log": log, "state": state, "restarted": restarted,
               "curl": os.path.exists(os.path.join(_fx, "curl-ran")),
               "tar": os.path.exists(os.path.join(_fx, "tar-ran")),
               "main": open(os.path.join(plugin, "main.py")).read(),
               "self_left": os.path.exists(self_dir), "err": p.stderr[-300:]}
        shutil.rmtree(plugin, ignore_errors=True); shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(self_dir, ignore_errors=True)
        return out

    _o = _run_script([_payload_sha])
    ok(_o["rc"] == 0 and _o["main"] == "# new\n" and _o["state"] == "done 9.9.9",
       f"a payload matching its signed sha256 installs ({_o['rc']}, {_o['state']!r}, {_o['err']!r})")
    ok(f"Update payload sha256={_payload_sha}" in _o["log"] and not _o["self_left"],
       "logging its sha256, and the script removes itself")
    _other = hashlib.sha256(b"something else").hexdigest()
    _o = _run_script([_other])
    ok(_o["rc"] != 0 and "Update failed: payload does not match its signed checksum" in _o["log"],
       f"a payload that does not match exits non-zero, saying why ({_o['rc']}, {_o['log'][-120:]!r})")
    ok(_o["curl"] and not _o["tar"] and _o["main"] == "# old\n",
       "after the download but before anything is extracted, and the installed files are untouched")
    ok(_o["state"] == "failed 9.9.9" and not _o["self_left"], "the panel is told it failed")
    for _what, _args in [("uppercase hex", [_payload_sha.upper()]), ("63 characters", [_payload_sha[:-1]]),
                         ("65 characters", [_payload_sha + "0"]), ("not hex", ["z" * 64]),
                         ("a space inside", [_payload_sha[:32] + " " + _payload_sha[33:]]),
                         ("empty", [""]), ("missing", [])]:
        _o = _run_script(_args)
        ok(_o["rc"] != 0 and not _o["curl"] and not _o["tar"] and _o["main"] == "# old\n"
           and "no signed checksum" in _o["log"],
           f"a 7th argument that is {_what} is refused before any download ({_o['rc']}, {_o['log'][-80:]!r})")

    # Uninstalled while it ran: Decky removed the plugin's directory.
    _o = _run_script([_payload_sha], installed=False)
    ok(_o["main"] == "# old\n" and not _o["restarted"]
       and "Update stopped: the plugin was uninstalled" in _o["log"] and _o["state"] != "failed 9.9.9",
       f"an update whose plugin was uninstalled installs nothing and restarts nothing ({_o['log'][-120:]!r})")
    _o = _run_script([_payload_sha], env=[("PGREP_HOOK", 'rm -f "$PLUGIN/plugin.json"')])
    ok(not _o["restarted"] and "Update stopped: the plugin was uninstalled" in _o["log"],
       "and one uninstalled after its files went in does not restart Decky for it")
    # A game still running at the script's own deadline: installed, and no
    # restart that would end the game. The deadline is cut short for the test.
    _o = _run_script([_payload_sha], edits=[("BUDGET=840", "BUDGET=2")], env=[("PGREP_RC", "0")])
    ok(_o["rc"] != 0 and _o["main"] == "# new\n" and not _o["restarted"]
       and _o["state"] == "failed 9.9.9" and "was still running, so Decky was not restarted" in _o["log"],
       f"a game still running at the deadline leaves the update installed, says so, and restarts nothing "
       f"({_o['rc']}, {_o['state']!r}, {_o['log'][-120:]!r})")
    _o = _run_script([_payload_sha])
    ok(_o["restarted"] and _o["state"] == "done 9.9.9", "with nothing running, Decky is restarted as before")
    _bud = re.search(r"^BUDGET=(\d+)$", m.UPDATE_SCRIPT, re.M)
    ok(bool(_bud) and int(_bud.group(1)) + 15 < m.UPDATE_STATE_MAX_AGE and '"$SECONDS" -lt "$BUDGET"' in m.UPDATE_SCRIPT,
       "the script's own deadline, and the wait that ends on it, fall inside the time its systemd unit is given")
    ok('IFS= read -r STREAM_PATTERNS < "$SELF_DIR/streams"' in m.UPDATE_SCRIPT
       and '"steamlaunch appid=${STREAM_PATTERNS:+|$STREAM_PATTERNS}"' in m.UPDATE_SCRIPT,
       "and a game is not the only thing it waits for: the streaming apps the user watches, "
       "handed over by the plugin, count too (L26)")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("access point lock: saying when it holds you on a weaker access point")
try:
    # The owner carried the Deck through the house with the lock on. The link
    # went bad, a much stronger node of the same network was right there, and
    # nothing said so, so he turned the lock off suspecting it. The lock still
    # never moves on its own (that would reconnect mid-stream); it says so,
    # and offers a move.
    LW = "eeeeeeee-0000-0000-0000-0000000000f1"
    LW_WINDOW = getattr(m.Plugin, "_LOCK_WEAKER_CHECK_SECONDS", 30)
    ok(LW_WINDOW >= 30, "the finding needs two reads at least 30 s apart")
    ok(getattr(m.Plugin, "_LOCK_WEAKER_MARGIN", 0) > m.Plugin._AP_UPGRADE_MARGIN,
       "and a margin larger than the one the lock moves on, since it nags while the lock is on")
    PINNED = "02:00:00:00:00:11"
    HALL = [  # pinned node far away, the router close by on both bands
        ("02:00:00:00:00:51", 88, 5180, "WPA2 WPA3"),
        ("02:00:00:00:00:24", 80, 2462, "WPA2 WPA3"),
        ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3"),
    ]
    ONLY_ROUTER_5 = [("02:00:00:00:00:51", 88, 5180, "WPA2 WPA3"),
                     ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")]

    LW_SCAN = getattr(m.Plugin, "_LOCK_SCAN_SECONDS", 60)
    LW_SETTLE = getattr(m.Plugin, "_LOCK_SCAN_SETTLE_SECONDS", 8)

    class LockReach(m.Plugin):
        def __init__(self, aps, km="wpa-psk", stream=None):
            super().__init__(); self.aps = aps; self.km = km; self.reads = 0
            self.cached_only = None; self.scans = []; self.stream = stream
        def _profile_key_mgmt(self, uuid): return self.km
        def _visible_access_points(self, iface, ssid, rescan=False, cached_only=False):
            self.reads += 1; self.cached_only = cached_only; return list(self.aps)
        def _run_cmd(self, cmd, timeout=5):
            if "rescan" in cmd: self.scans.append(list(cmd))
            return _ok()
        def _find_stream(self, patterns): return self.stream

    def lw_settings(**extra):
        return {**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True,
                "bssid_lock_connection_uuid": LW, **extra}

    def lw_live(pin=PINNED, on=PINNED):
        # nmcli upper-case for the pin, iw lower-case for the link.
        return {"bssid_lock": pin.upper(), "connected_bssid": on.lower()}

    def lw_read(r, settings=None, live=None, uuid=LW, ssid="Net", link=""):
        r._recheck_lock_weaker(settings or lw_settings(), live or lw_live(),
                               uuid, "wlan0", ssid, link)
        return getattr(r, "_lock_weaker", None)

    def lw_shift(r, back, scan=None):
        # Moves every remembered time `back` seconds into the past, and the
        # last scan request by `scan` instead when given. Relative to now,
        # never zero: the monotonic clock starts at boot, so on a runner that
        # has just started, zero is still inside every window.
        for name in ("_lock_weaker_checked_at", "_lock_weaker_since"):
            at = getattr(r, name, None)
            if at is not None:
                setattr(r, name, at - back)
        asked = getattr(r, "_lock_scan_at", None)
        if asked is not None:
            r._lock_scan_at = asked - (back if scan is None else scan)
            # Still the same scan, so a read already made of it stays made.
            if getattr(r, "_lock_scan_used", None) == asked:
                r._lock_scan_used = r._lock_scan_at

    def lw_fresh(r, **kw):
        # One scan and one read of it: the read that asks for the scan cannot
        # use it, the next one, past the settle time and before the next scan
        # is due, can.
        lw_shift(r, LW_SCAN + 1)
        lw_read(r, **kw)
        lw_shift(r, LW_WINDOW + 1)
        return lw_read(r, **kw)

    def lw_later(r):
        lw_shift(r, LW_WINDOW + 1)

    def lw_two_reads(r, **kw):
        return lw_fresh(r, **kw), lw_fresh(r, **kw)

    r = LockReach(HALL)
    first, second = lw_two_reads(r)
    ok(first is None, "one read, however lopsided, is not enough to say anything")
    ok(second == {"signal": 88, "current_signal": 31},
       "the same finding on a second read 30 s later is reported, with both signals")
    ok(r.cached_only is True, "and it only ever reads nmcli's cached scan")

    r = LockReach(HALL)
    lw_fresh(r)
    reads, scans = r.reads, len(r.scans)
    lw_read(r)
    ok(r._lock_weaker is None and r.reads == reads and len(r.scans) == scans,
       "a second poll inside the window neither reports, reads nor scans")

    r = LockReach([("02:00:00:00:00:51", 31 + 15, 5180, "WPA2 WPA3"),
                   ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")])
    ok(lw_two_reads(r)[1] is None,
       "15 points stronger is past the lock's own margin but not this one, so no notice")
    r = LockReach([("02:00:00:00:00:51", 31 + 20, 5180, "WPA2 WPA3"),
                   ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")])
    ok(lw_two_reads(r)[1] == {"signal": 51, "current_signal": 31},
       "20 points stronger is reported")

    r = LockReach(HALL)
    ok(lw_two_reads(r, settings=lw_settings(bssid_lock_enabled=False))[1] is None,
       "not with the lock off")
    r = LockReach(HALL)
    ok(lw_two_reads(r, uuid="eeeeeeee-0000-0000-0000-0000000000f2")[1] is None,
       "not on a profile the lock was not written for")
    r = LockReach(HALL)
    ok(lw_two_reads(r, live=lw_live(on="02:00:00:00:00:24"))[1] is None,
       "not when the device is not on the pinned access point")
    r = LockReach(HALL)
    ok(lw_two_reads(r, live=lw_live(pin=""))[1] is None,
       "not when the profile carries no pin")
    r = LockReach(HALL)
    ok(lw_two_reads(r, link="Connected to 02:00:00:00:00:11\n\tsignal: -50 dBm\n")[1] is None,
       "not on a link healthy enough that the lock would not move off it")

    r = LockReach(ONLY_ROUTER_5)
    ok(lw_two_reads(r, settings=lw_settings(
        ap_move_failures={"02:00:00:00:00:51": int(time.time())}))[1] is None,
       "not for an access point that refused us and is still in its cooldown")
    r = LockReach([("02:00:00:00:00:51", 88, 5180, "WPA2"),
                   ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")], km="sae")
    ok(lw_two_reads(r)[1] is None,
       "not for an access point this profile's security cannot join")
    _band = lw_settings(band_preference_enabled=True, band_preference="a",
                        band_preference_ssid="Net")
    r = LockReach([("02:00:00:00:00:24", 88, 2462, "WPA2 WPA3"),
                   ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")])
    ok(lw_two_reads(r, settings=_band)[1] is None,
       "not for an access point on the other band while a band preference is set")
    r = LockReach([("02:00:00:00:00:24", 88, 2462, "WPA2 WPA3"),
                   ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")])
    ok(lw_two_reads(r, settings=lw_settings(band_preference_enabled=True,
                                           band_preference="a",
                                           band_preference_ssid="Elsewhere"))[1] is not None,
       "while a preference set for another network does not constrain this one")

    r = LockReach(HALL)
    lw_two_reads(r)
    r.aps = [("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")]
    ok(lw_fresh(r) is None, "it clears when a fresh scan no longer has the stronger access point")
    r = LockReach(HALL)
    lw_two_reads(r)
    r.aps = [("02:00:00:00:00:51", 88, 5180, "WPA2 WPA3"),
             ("02:00:00:00:00:11", 80, 5745, "WPA2 WPA3")]
    ok(lw_fresh(r) is None, "and when the pinned one recovers")
    r = LockReach(HALL)
    lw_two_reads(r)
    ok(lw_read(r, settings=lw_settings(bssid_lock_enabled=False)) is None,
       "and at once when the lock is turned off, without waiting out the window")
    # Two weak nodes and the router: the finding for one pin must not carry
    # over to the next, or a fresh pin is reported on a single read.
    r = LockReach([("02:00:00:00:00:51", 88, 5180, "WPA2 WPA3"),
                   ("02:00:00:00:00:22", 31, 5220, "WPA2 WPA3"),
                   ("02:00:00:00:00:11", 31, 5745, "WPA2 WPA3")])
    ok(lw_two_reads(r)[1] is not None, "(the first pin's finding is reported)")
    ok(lw_fresh(r, live=lw_live(pin="02:00:00:00:00:22", on="02:00:00:00:00:22")) is None,
       "a different pin starts over rather than inheriting the old finding")

    for what, setup in [
        ("another profile change is mid-flight", lambda p: setattr(p, "_profile_change_depth", 1)),
        ("an update is installing", lambda p: setattr(p, "_update_starting", True)),
        ("a backend switch is running", lambda p: setattr(p, "_backend_switch", {"in_progress": True})),
    ]:
        r = LockReach(HALL)
        lw_two_reads(r)
        setup(r)
        ok(lw_read(r) is None, f"nothing is reported while {what}")

    r = LockReach(HALL)
    lw_two_reads(r)
    _st = {"live": lw_live()}
    r._report_lock_weaker(lw_settings(), _st, LW, "wlan0", "Net", "")
    ok(_st.get("lock_weaker") == {"signal": 88, "current_signal": 31},
       "the status carries it as lock_weaker")

    # With the lock on there is no background scanning: measured on a Deck,
    # every other access point read the same for four minutes, then expired.
    r = LockReach(HALL)
    lw_read(r)
    ok(r.scans == [["/usr/bin/nmcli", "dev", "wifi", "rescan", "ifname", "wlan0"]],
       "holding on a weak link with no stream, it asks nmcli for a scan")
    ok(r._lock_weaker is None and getattr(r, "_lock_weaker_since", None) is None,
       "and the read that asked cannot use it")
    for what, kw in [("the lock is off", {"settings": lw_settings(bssid_lock_enabled=False)}),
                     ("the link is healthy", {"link": "Connected to x\n\tsignal: -50 dBm\n"}),
                     ("it is not on the pinned access point", {"live": lw_live(on="02:00:00:00:00:24")})]:
        r = LockReach(HALL); lw_read(r, **kw)
        ok(not r.scans, f"no scan when {what}")

    for what, setup in [
        ("auto mode's stream flag is up", lambda: open(m.STREAM_FLAG_FILE, "w").close()),
        ("the user holds streaming on", lambda: open(m.STREAM_HOLD_FILE, "w").close()),
        ("a streaming app is running with auto mode off", lambda: None),
    ]:
        for _f in (m.STREAM_FLAG_FILE, m.STREAM_HOLD_FILE):
            if os.path.exists(_f): os.remove(_f)
        setup()
        r = LockReach(HALL, stream={"app": "Moonlight", "process": "moonlight"}
                      if "app is running" in what else None)
        got = [lw_fresh(r) for _ in range(3)]
        ok(not r.scans and all(g is None for g in got)
           and getattr(r, "_lock_weaker_since", None) is None,
           f"never a scan while {what}, so nothing is said on the stale list")
    for _f in (m.STREAM_FLAG_FILE, m.STREAM_HOLD_FILE):
        if os.path.exists(_f): os.remove(_f)

    r = LockReach(HALL)
    lw_read(r)
    lw_shift(r, LW_WINDOW + 1)
    lw_read(r)
    ok(len(r.scans) == 1, "inside the scan window no second scan is asked for")
    lw_shift(r, LW_SCAN + 1)
    lw_read(r)
    ok(len(r.scans) == 2, "past it, one is")

    r = LockReach(HALL)
    lw_read(r)
    lw_shift(r, LW_WINDOW + 1, scan=LW_SETTLE - 3)
    lw_read(r)
    ok(getattr(r, "_lock_weaker_since", None) is None,
       "a read sooner than the settle time after the scan does not start a finding")
    r = LockReach(HALL)
    lw_fresh(r)
    lw_shift(r, LW_WINDOW + 1, scan=LW_SETTLE)
    ok(lw_read(r) is None and len(r.scans) == 1,
       "a second read of the same scan does not confirm one")
    r = LockReach(HALL)
    lw_fresh(r)
    lw_shift(r, LW_SCAN + 1)
    lw_read(r)
    lw_shift(r, LW_WINDOW + 1, scan=LW_SETTLE - 3)
    ok(lw_read(r) is None, "nor does a read too soon after the next scan")
    lw_shift(r, LW_WINDOW + 1, scan=LW_SETTLE)
    ok(lw_read(r) == {"signal": 88, "current_signal": 31},
       "while a read of that scan once it has settled does")
    _coll = _src_all[_src_all.index("    def _collect_status"):]
    _coll = _coll[:_coll.index("\n    def ", 10)]
    ok("self._report_lock_weaker(" in _coll, "and the status poll is what runs it")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("access point lock: moving it when asked")
try:
    _logged = []
    _real_info = m.decky.logger.info
    m.decky.logger.info = staticmethod(lambda msg: _logged.append(msg))
    try:
        # A real UUID: settings validation drops anything else, and a lock
        # recorded for no profile is a lock for no network.
        MU = "eeeeeeee-0000-0000-0000-0000000000f3"
        class LockedMesh(Mesh):
            def _get_active_connection_uuid(self): return MU

        def lock_on(**extra):
            m._save_settings({**base_ap, "bssid_lock_enabled": True,
                              "bssid_lock_value": "02:00:00:00:00:11",
                              "bssid_lock_connection_uuid": MU,
                              "bssid_lock_uuids": [MU], **extra})

        def mesh_pinned(scan=MESH, **kw):
            g = LockedMesh(scan, "02:00:00:00:00:11", **kw)
            g.pinned = "02:00:00:00:00:11"
            g._ASSOCIATION_WAIT_SECONDS = 1.0
            g._lock_weaker = {"signal": 100, "current_signal": 45}
            return g

        lock_on(); _logged.clear()
        g = mesh_pinned()
        r = asyncio.run(g.move_bssid_lock())
        _s = m._load_settings()
        ok(r.get("success") is True and g.pinned.upper() == "02:00:00:00:00:51"
           and g.on.upper() == "02:00:00:00:00:51",
           "moving writes the stronger access point as the pin and lands on it")
        ok(_s["bssid_lock_value"] == "02:00:00:00:00:51" and _s["bssid_lock_enabled"] is True
           and _s["bssid_lock_connection_uuid"] == MU,
           "and records it in settings the way turning the lock on does")
        ok(any("Moving the BSSID lock from 02:00:00:00:00:11 (signal 45) to "
               "02:00:00:00:00:51 (signal 100)" in l for l in _logged),
           "the move is logged with both access points and both signals")
        ok(g._lock_weaker is None and getattr(g, "_profile_change_depth", 0) == 0,
           "the notice is cleared and the in-flight claim released")

        lock_on()
        g = mesh_pinned(reachable={"02:00:00:00:00:11"})
        r = asyncio.run(g.move_bssid_lock())
        _s = m._load_settings()
        ok(g.pinned.upper() == "02:00:00:00:00:11" and _s["bssid_lock_value"].upper() == "02:00:00:00:00:11",
           "an access point that will not have us is rolled back to the previous pin")
        ok("02:00:00:00:00:51" in _s.get("ap_move_failures", {}),
           "and remembered, so the notice does not offer it again during its cooldown")
        ok(r.get("success") is False and r.get("error") == "move_failed"
           and "went back" in r.get("message", "") and r.get("reconnected") is True,
           "the user is told the move did not happen and why")
        ok(g._lock_weaker is None and getattr(g, "_profile_change_depth", 0) == 0,
           "with the notice cleared and the claim released")

        class NoPutBack(LockedMesh):
            """Writing the previous pin back fails, and maybe clearing it too."""
            def __init__(self, *a, clear_fails=False, **kw):
                super().__init__(*a, **kw); self.clear_fails = clear_fails
            def _nmcli_modify(self, u, k, v, timeout=5):
                if k == "802-11-wireless.bssid" and (
                        v.upper() == "02:00:00:00:00:11" or (v == "" and self.clear_fails)):
                    self.mods.append((k, v))
                    return {"success": False, "stdout": "", "stderr": "failed", "returncode": 1}
                return super()._nmcli_modify(u, k, v, timeout)
        for _clear_fails in (False, True):
            lock_on()
            g = NoPutBack(MESH, "02:00:00:00:00:11", reachable={"02:00:00:00:00:11"},
                          clear_fails=_clear_fails)
            g.pinned = "02:00:00:00:00:11"; g._ASSOCIATION_WAIT_SECONDS = 1.0
            g._lock_weaker = {"signal": 100, "current_signal": 45}
            r = asyncio.run(g.move_bssid_lock())
            _s = m._load_settings()
            if not _clear_fails:
                ok(r.get("error") == "move_failed" and "turned off" in r.get("message", "")
                   and g.pinned == "" and _s["bssid_lock_enabled"] is False
                   and _s["bssid_lock_value"] == "",
                   "if the previous pin cannot be put back, the pin comes off and the lock reads off")
            else:
                ok(r.get("error") == "move_failed" and "Turn the lock off" in r.get("message", "")
                   and _s["bssid_lock_enabled"] is True
                   and _s["bssid_lock_value"].upper() == "02:00:00:00:00:51",
                   "and if even that fails, the settings say where it is really pinned, and the user is told")

        lock_on()
        g = mesh_pinned(scan=NEAR)
        r = asyncio.run(g.move_bssid_lock())
        ok(r.get("error") == "no_better_ap" and not any(k == "802-11-wireless.bssid" for k, _ in g.mods),
           "nothing clearly stronger on a fresh look writes nothing and does not reconnect")
        ok(g._lock_weaker is None and getattr(g, "_profile_change_depth", 0) == 0,
           "and still clears the notice and releases the claim")

        m._save_settings({**base_ap, "bssid_lock_enabled": False})
        g = mesh_pinned()
        r = asyncio.run(g.move_bssid_lock())
        ok(r.get("error") == "not_locked" and not g.mods and g.scans == 0,
           "with the lock off it refuses, touching nothing")

        class Elsewhere(Mesh):
            def _get_profile_ssid(self, uuid, timeout=5):
                return {MU: "Net"}.get(uuid, "Cafe")
            def _get_active_connection_uuid(self):
                return "eeeeeeee-0000-0000-0000-0000000000f4"
        lock_on()
        g = Elsewhere(MESH, "02:00:00:00:00:11")
        r = asyncio.run(g.move_bssid_lock())
        ok(r.get("error") == "not_locked" and not g.mods,
           "and it never pins a network the lock was not turned on for")

        class IwdMesh(LockedMesh):
            def _backend_is_iwd(self): return True
        lock_on()
        g = IwdMesh(MESH, "02:00:00:00:00:11")
        r = asyncio.run(g.move_bssid_lock())
        ok(r.get("error") == "needs_wpa_supplicant" and not g.mods
           and getattr(g, "_profile_change_depth", 0) == 0,
           "under iwd it refuses with the reason and releases the claim")

        class Boom(LockedMesh):
            def _require_wifi(self): raise RuntimeError("boom")
        lock_on()
        g = Boom(MESH, "02:00:00:00:00:11")
        r = asyncio.run(g.move_bssid_lock())
        ok(r.get("success") is False and getattr(g, "_profile_change_depth", 0) == 0,
           "an unexpected error is reported and the claim is still released")

        for what, setup in [
            ("another profile change is mid-flight", lambda p: setattr(p, "_profile_change_depth", 1)),
            ("an update is installing", lambda p: setattr(p, "_update_starting", True)),
            ("a backend switch is running", lambda p: setattr(p, "_backend_switch", {"in_progress": True})),
        ]:
            lock_on()
            g = mesh_pinned()
            setup(g)
            depth = getattr(g, "_profile_change_depth", 0)
            r = asyncio.run(g.move_bssid_lock())
            ok(r.get("error") == "busy" and not g.mods
               and getattr(g, "_profile_change_depth", 0) == depth,
               f"it refuses while {what}, leaving that claim alone")

        # Turning the lock off said nothing in the log, and after a bad
        # stretch the owner had to ask whether he had done it himself.
        class Unlock(m.Plugin):
            def __init__(self, err=None): super().__init__(); self.err = err
            async def _release_lock_from_all_profiles(self): return self.err
        _logged.clear()
        r = asyncio.run(Unlock().set_bssid_lock(False))
        ok(r.get("success") is True and "BSSID lock turned off" in _logged,
           "turning the lock off is logged")
        _logged.clear()
        asyncio.run(Unlock({"success": False, "error": "nmcli_failed", "message": "x"}).set_bssid_lock(False))
        ok("BSSID lock turned off" not in _logged, "but not when it did not actually come off")
    finally:
        m.decky.logger.info = _real_info

    _tsx = open("src/index.tsx").read()
    # F8 renamed the condition: the finding is shown unless dismissed.
    _ban = _tsx[_tsx.index("{(lockWeakerShown || errors.move_bssid_lock) && ("):]
    _ban = _ban[:_ban.index("Move the lock") + 40]
    ok("The access point lock is holding you on a weaker access point" in _ban
       and "A stronger one is in" in _ban and "status.lock_weaker.current_signal" in _ban
       and "status.lock_weaker.signal" in _ban,
       "the panel shows a warning with both signals while lock_weaker is reported")
    ok("onClick={handleMoveLock}" in _ban and "disabled={isBusy || updating}" in _ban,
       "with a Move the lock button that is held while busy or updating")
    ok("errors.move_bssid_lock" in _ban, "and a failed move's message is shown there")
    ok('handleToggle("move_bssid_lock", () => backend.moveBssidLock())' in _tsx,
       "the button calls the backend's move")
    ok('callable<[], MethodResult>("move_bssid_lock")' in open("src/backend.ts").read()
       and "lock_weaker?:" in open("src/types.ts").read(),
       "the callable and the status field are declared")
    ok(all(ord(ch) < 128 for ch in _ban.replace('icon="⚠"', "")),
       "the wording is plain ASCII")
    _dist = open("dist/index.js").read()
    ok('"move_bssid_lock"' in _dist and "Move the lock" in _dist
       and "The access point lock is holding you on a weaker access point" in _dist,
       "and the built bundle carries the notice and the button")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

# ---- QA pass 3, BACKEND-LIFECYCLE (L1-L36, J1, J2) ----
# Each section is named after its item in session-artifacts/qa-0.24.4/
# pass3-triage.md. Shared stubs first: the system files these touch, a Reset,
# an uninstall and a startup with everything around them stubbed.
import errno as _l_errno, threading as _l_thr, builtins as _l_bi, shutil as _l_sh


def _l_paths():
    """Point the system files Reset, uninstall and startup touch at a scratch
    directory. Returns (directory, restore)."""
    d = tempfile.mkdtemp()
    os.mkdir(os.path.join(d, "conf.d")); os.mkdir(os.path.join(d, "run"))
    places = {
        "SETTINGS_FILE": os.path.join(d, "settings", "settings.json"),
        "LEGACY_ENFORCED_FILE": os.path.join(d, "settings", "last_enforced"),
        "NM_CONF_PATH": os.path.join(d, "conf.d", "99-wifi-optimizer.conf"),
        "GENERIC_BACKEND_CONF": os.path.join(d, "conf.d", "99-wifi-optimizer-backend.conf"),
        "WIFI_BACKEND_CONF": os.path.join(d, "conf.d", "99-valve-wifi-backend.conf"),
        "BAZZITE_IWD_CONF": os.path.join(d, "conf.d", "iwd.conf"),
        "NM_DEFAULT_CONF": os.path.join(d, "usr-10-steamos-defaults.conf"),
        "HOLO_NM_DEFAULT_CONF": os.path.join(d, "usr-10-holo-defaults.conf"),
        "MODPROBE_CONF_PATH": os.path.join(d, "99-modprobe.conf"),
        "DISPATCHER_PATH": os.path.join(d, "99-dispatcher"),
        "ENFORCED_FILE": os.path.join(d, "run", "last_enforced"),
    }
    os.mkdir(os.path.join(d, "settings"))
    saved = {name: getattr(m, name) for name in places if hasattr(m, name)}
    for name in saved:
        setattr(m, name, places[name])

    def restore():
        for name, value in saved.items():
            setattr(m, name, value)
        for path in (m.STREAM_FLAG_FILE, m.STREAM_HOLD_FILE, m.BACKEND_SWITCH_MARKER):
            if os.path.exists(path):
                os.remove(path)
    return d, restore


def _l_valve(value=None):
    """Valve's NetworkManager file, with wifi.powersave set to value or absent."""
    text = "[device]\nwifi.backend=wpa_supplicant\n"
    if value is not None:
        text = f"[connection]\nwifi.powersave={value}\n" + text
    open(m.WIFI_BACKEND_CONF, "w").write(text)


class _LReset(m.Plugin):
    """Reset with the system around it stubbed, recording the order of its steps."""
    driver = "rtw88"

    def __init__(self, left=None, real_drivers=False):
        super().__init__(); self.order = []; self.cmds = []; self.fixes = []
        self._left = left; self._real_drivers = real_drivers
    async def get_device_info(self):
        return {"success": True, "model": "lcd", "driver": self.driver,
                "device_family": "deck_lcd", "device_label": "Steam Deck LCD",
                "chip_label": "unknown" if self.driver == "unknown" else "WiFi 5 (RTL8822CE)",
                "supports_6ghz": False}
    def _detect_distro(self): return {"id": "steamos", "name": "SteamOS"}
    def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
    def _get_wifi_interface(self): return None
    def _install_dispatcher(self): return True
    def _remove_dispatcher(self): return True
    def _release_generic_backend_conf(self): self.order.append("backend")
    def _release_regdomain(self, settings=None): self.order.append("region")
    def _revert_queue_tuning(self, settings): self.order.append("queue")
    def _apply_driver_fixes(self, enable, persist=True, record_all=False):
        self.fixes.append(enable)
        if self._real_drivers:
            return m.Plugin._apply_driver_fixes(self, enable, persist, record_all)
    def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
    def _clear_profile_pins(self, deadline=None, settings=None):
        self.order.append("pins")
        if self._left is not None:
            return self._left
        return m.Plugin._clear_profile_pins(self, deadline, settings)
    def _release_power_save(self, settings=None, reload_conf=False):
        self.order.append("power save")
        return m.Plugin._release_power_save(self, settings, reload_conf)


class _LTear(m.Plugin):
    """Uninstall with the system around it stubbed."""
    def __init__(self): super().__init__(); self.cmds = []
    def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
    def _get_wifi_interface(self): return None
    def _remove_dispatcher(self): return True
    def _apply_driver_fixes(self, enable, persist=True, record_all=False): pass
    def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
    def _release_generic_backend_conf(self): pass


class _LBoot(m.Plugin):
    """A plugin load with the system stubbed; the setters record their calls."""
    backend_method = "none"

    def __init__(self): super().__init__(); self.calls = []; self.cmds = []
    async def get_device_info(self):
        return {"success": True, "driver": "rtw88", "model": "lcd", "device_family": "deck_lcd",
                "device_label": "Steam Deck LCD", "chip_label": "WiFi 5 (RTL8822CE)",
                "supports_6ghz": False}
    def _nm_version(self): return (1, 52, 1)
    def _detect_distro(self): return {"id": "steamos", "name": "SteamOS"}
    def _acknowledge_update(self): pass
    def _rotate_logs(self, keep=10): pass
    def _reconcile_backend_switch(self): pass
    def _run_cmd(self, cmd, timeout=5): self.cmds.append(cmd); return _ok()
    def _install_dispatcher(self): return True
    def _remove_dispatcher(self): return True
    def _heal_orphaned_iwd_conf(self): return False
    async def _unpin_if_iwd(self): pass
    def _get_wifi_interface(self): return "wlan0"
    def _get_backend_method(self): return self.backend_method
    async def _stranded_watch(self): return
    def _find_stream(self, patterns): return None
    def _apply_driver_fixes(self, enable, persist=True, record_all=False): pass
    def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
    def _revert_queue_tuning(self, settings): pass
    async def set_power_save(self, disabled):
        self.calls.append(("power_save", disabled)); return {"success": True}
    async def set_buffer_tuning(self, enabled):
        self.calls.append(("buffer_tuning", enabled)); return {"success": True}
    async def set_cake(self, enabled):
        self.calls.append(("cake", enabled)); return {"success": True}


def _l_main(plugin):
    """Run _main; whether auto mode's watch was started."""
    async def go():
        await plugin._main()
        return getattr(plugin, "_stream_task", None) is not None
    return asyncio.run(go())


section("L1 startup and auto mode go on past a settings save that fails")
try:
    _d, _restore = _l_paths()
    _real_write = m._write_settings
    try:
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "settings_version": 2,
                          "streaming_mode_enabled": True, "power_save_disabled": True})
        # The user holds the fixes on, so the startup apply has an open gate.
        open(m.STREAM_HOLD_FILE, "w").write("1\n")
        def _enospc(data): raise OSError(_l_errno.ENOSPC, "No space left on device")
        m._write_settings = _enospc
        _b = _LBoot()
        try:
            _watch = _l_main(_b)
        finally:
            m._write_settings = _real_write
        ok(_watch, "with every settings save failing, auto mode's watch still starts at load")
        ok(_b.calls.count(("power_save", True)) == 2,
           f"and both the stream pickup and the startup apply run ({_b.calls})")
        os.remove(m.STREAM_HOLD_FILE)
        if os.path.exists(m.STREAM_FLAG_FILE): os.remove(m.STREAM_FLAG_FILE)

        # The toggle: its own save lands, the one recording Valve's value fails.
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "settings_version": 2})
        _n = {"writes": 0}
        def _second_fails(data):
            _n["writes"] += 1
            if _n["writes"] >= 2:
                raise OSError(_l_errno.ENOSPC, "No space left on device")
            _real_write(data)
        m._write_settings = _second_fails
        _b = _LBoot()
        async def _l1_toggle(p):
            await p.set_streaming_mode(True)
            return getattr(p, "_stream_task", None) is not None
        try:
            _watch = asyncio.run(_l1_toggle(_b))
        finally:
            m._write_settings = _real_write
        ok(_watch and m._load_settings().get("streaming_mode_enabled") is True,
           "turning auto mode on starts the watch though a later save failed")
    finally:
        m._write_settings = _real_write
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L2 Reset keeps the driver it knew when the radio is missing")
try:
    _d, _restore = _l_paths()
    _mod_root = tempfile.mkdtemp()
    _LPS = "/sys/module/rtw88_core/parameters/disable_lps_deep"
    _ASPM = "/sys/module/rtw88_pci/parameters/disable_aspm"
    _real_open = _l_bi.open
    def _sys(path): return os.path.join(_mod_root, path[len("/sys/module/"):])
    def _open_sys(path, *a, **k):
        if isinstance(path, str) and path.startswith("/sys/module/"):
            path = _sys(path)
        return _real_open(path, *a, **k)
    _saved_dirs = (m.MODPROBE_DIRS, m.PROC_CMDLINE)
    m.MODPROBE_DIRS = (tempfile.mkdtemp(),)
    m.PROC_CMDLINE = os.path.join(_d, "cmdline"); open(m.PROC_CMDLINE, "w").write("")
    for _p in (_LPS, _ASPM):
        os.makedirs(os.path.dirname(_sys(_p)), exist_ok=True)
        _real_open(_sys(_p), "w").write("Y")
    if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)
    _l_bi.open = _open_sys
    try:
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "chip_label": "WiFi 5 (RTL8822CE)",
                          "power_save_disabled": True})
        _r = _LReset(real_drivers=True); _r.driver = "unknown"
        _res = asyncio.run(_r.reset_settings())
        _st = m._load_settings()
        ok(_res.get("success") and _st.get("driver") == "rtw88"
           and _st.get("chip_label") == "WiFi 5 (RTL8822CE)",
           f"Reset with the radio missing keeps rtw88 rather than record unknown ({_st.get('driver')})")
        ok(_real_open(_sys(_LPS)).read() == "N" and _real_open(_sys(_ASPM)).read() == "N",
           "and takes the driver's parameters off, back to what the system gives them")
    finally:
        _l_bi.open = _real_open
        m.MODPROBE_DIRS, m.PROC_CMDLINE = _saved_dirs
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L3 Reset then uninstall leaves the user's own Steam power management setting alone")
try:
    _d, _restore = _l_paths()
    try:
        # Steam's own WiFi power management off (2), recorded as theirs, with
        # Prevent lag spikes on (our file in place).
        _l_valve("2")
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                          "valve_powersave_before": "2"})
        asyncio.run(_LReset(left={}).reset_settings())
        _after_reset = m.Plugin()._valve_powersave()
        _LTear()._teardown(time.monotonic() + 4)
        ok(_after_reset == "2" and m.Plugin()._valve_powersave() == "2",
           f"Reset, then uninstall, leave Valve's line at the user's 2 ({_after_reset}, "
           f"{m.Plugin()._valve_powersave()})")

        # Prevent lag spikes off first, then the user turns Steam's setting off, then Reset.
        _l_valve("3")
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                          "valve_powersave_before": "3"})
        class _L3Off(_LTear):
            def _reload_nm_conf(self): pass
        asyncio.run(_L3Off().set_power_save(False))
        ok(m._load_settings().get("valve_powersave_before") == "3",
           "turning it off records what Valve's file says once ours is out of it")
        _l_valve("2")
        asyncio.run(_LReset(left={}).reset_settings())
        ok(m.Plugin()._valve_powersave() == "2" and m._load_settings().get("valve_powersave_before") == "2",
           "a 2 set in Steam after that survives Reset, and Reset records it as the user's")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L4 custom DNS left by Reset is retried, and never left with no DNS")
try:
    _U4 = "dddddddd-0000-0000-0000-0000000000f4"
    class _L4Nm(m.Plugin):
        def __init__(self, fail=()):
            super().__init__(); self.mods = []; self.fail = set(fail)
        def _nmcli_modify(self, uuid, key, value, timeout=5):
            self.mods.append((uuid, key, value))
            if key in self.fail:
                return {"success": False, "stdout": "", "stderr": "busy", "returncode": 1}
            return _ok()
        def _get_wifi_interface(self): return None
        def _get_active_connection_uuid(self): return None
    _n4 = _L4Nm(fail=("ipv4.ignore-auto-dns",))
    ok(_n4._clear_dns(_U4) is False and _n4.mods == [(_U4, "ipv4.ignore-auto-dns", "no")],
       f"when the router's DNS cannot be let back in, the servers stay and it reports failure ({_n4.mods})")
    _d, _restore = _l_paths()
    try:
        m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": False, "dns_uuids": [_U4]})
        _n4 = _L4Nm()
        # One nmcli call per poll since L9 (pass 4): the three writes take
        # three polls, in _clear_dns's order.
        _per_poll = []
        for _ in range(3):
            _before = len(_n4.mods); asyncio.run(_n4.get_status()); _per_poll.append(len(_n4.mods) - _before)
        ok([k for _u, k, _v in _n4.mods] == ["ipv4.ignore-auto-dns", "ipv6.ignore-auto-dns", "ipv4.dns"]
           and _per_poll == [1, 1, 1] and m._load_settings().get("dns_uuids") == [],
           f"DNS off with a profile still listed: status polls give it its DNS back and let it go ({_n4.mods})")
        m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": True, "dns_uuids": [_U4]})
        _n4 = _L4Nm(); asyncio.run(_n4.get_status())
        ok(not _n4.mods and m._load_settings().get("dns_uuids") == [_U4],
           "while DNS is on the list is the live one and nothing is cleared")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L5 power save files left by a cut-off change are removed at load, and Reset takes them off first")
try:
    _d, _restore = _l_paths()
    try:
        for _on in (False, True):
            open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
            open(m.MODPROBE_CONF_PATH, "w").write("options rtw88_pci disable_aspm=Y\n")
            m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "settings_version": 2,
                              "power_save_disabled": _on})
            _b = _LBoot(); _l_main(_b)
            _reloads = [c for c in _b.cmds if c[-3:] == ["general", "reload", "conf"]]
            if not _on:
                ok(not os.path.exists(m.NM_CONF_PATH) and not os.path.exists(m.MODPROBE_CONF_PATH)
                   and len(_reloads) == 1,
                   f"Prevent lag spikes off: both files are gone after a load, with one config reload ({len(_reloads)})")
            else:
                ok(os.path.exists(m.NM_CONF_PATH) and os.path.exists(m.MODPROBE_CONF_PATH),
                   "Prevent lag spikes on: both stay")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _r = _LReset(left={}); asyncio.run(_r.reset_settings())
        ok("power save" in _r.order and "pins" in _r.order
           and _r.order.index("power save") < _r.order.index("pins"),
           f"Reset takes power save off before the pins, as uninstall does ({_r.order})")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L6 Optimize Safe leaves a lock that is already holding alone")
try:
    _U6 = "dddddddd-0000-0000-0000-0000000000f6"
    class _L6(m.Plugin):
        def __init__(self, on="02:00:00:00:00:61"):
            super().__init__(); self.on = on; self.lock_calls = 0; self.reconnects = 0
        def _backend_is_iwd(self): return False
        def _get_active_connection_uuid(self): return _U6
        def _get_wifi_interface(self): return "wlan0"
        def _get_profile_ssid(self, uuid, timeout=5): return "Home"
        def _read_profile_field(self, uuid, field, timeout=5):
            return "02\\:00\\:00\\:00\\:00\\:61" if field == "802-11-wireless.bssid" else ""
        def _run_cmd(self, cmd, timeout=5):
            if cmd[-1] == "link":
                return _ok(f"Connected to {self.on} (on wlan0)\n\tfreq: 5180\n")
            return _ok()
        async def set_auto_fix(self, e): return {"success": True}
        async def set_buffer_tuning(self, e): return {"success": True}
        async def set_power_save(self, d): return {"success": True}
        async def set_bssid_lock(self, e):
            self.lock_calls += 1; return {"success": True, "reconnected": True}
        def _hard_reconnect(self, uuid=None): self.reconnects += 1; return True
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True,
                      "bssid_lock_value": "02:00:00:00:00:61", "bssid_lock_connection_uuid": _U6})
    _p6 = _L6(); _r6 = asyncio.run(_p6.optimize_safe())
    ok(_p6.lock_calls == 0 and _p6.reconnects == 0 and not _r6.get("reconnected")
       and _r6.get("applied") == _r6.get("total") == 4,
       f"lock on and holding: no lock rewrite, no reconnect, and all applied ({_r6.get('applied')}/{_r6.get('total')})")
    _p6 = _L6(on="02:00:00:00:00:62"); _r6 = asyncio.run(_p6.optimize_safe())
    ok(_p6.lock_calls == 1 and _r6.get("reconnected") is True,
       "on another access point than the one pinned, the lock is set again, and says it reconnected")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L7 a tool stuck in the kernel is not started again until it exits")
try:
    class _StuckProc:
        spawned = 0
        exited = False
        def __init__(self, cmd, **kw):
            _StuckProc.spawned += 1; self.stdout = None; self.stderr = None; self.pid = 4242424
        def communicate(self, timeout=None):
            raise m.subprocess.TimeoutExpired("iw", timeout)
        def kill(self): pass
        def poll(self): return 0 if _StuckProc.exited else None
    _real_popen = m.subprocess.Popen
    _logged7 = []
    _real_info7, _real_err7 = m.decky.logger.info, m.decky.logger.error
    m.subprocess.Popen = _StuckProc
    m.decky.logger.info = lambda msg: _logged7.append(msg)
    m.decky.logger.error = lambda msg: _logged7.append(msg)
    try:
        _p7 = m.Plugin()
        _res7 = [_p7._run_cmd(["/usr/bin/iw", "dev", "wlan0", "link"], timeout=0.01) for _ in range(3)]
        ok(_StuckProc.spawned == 1 and not any(r["success"] for r in _res7)
           and "still stuck" in _res7[1]["stderr"],
           f"three passes start iw once while the first is stuck ({_StuckProc.spawned} started)")
        _iw_lines = [l for l in _logged7 if "iw" in l and "left it behind" in l]
        ok(len(_iw_lines) == 1, f"and log it once ({_logged7})")
        ok(getattr(_p7, "_stuck_tools", lambda: None)() == {"iw"}, "_stuck_tools names it")
        _p7._run_cmd(["/usr/bin/tc", "qdisc", "show", "dev", "wlan0"], timeout=0.01)
        ok(_StuckProc.spawned == 2, "another tool is not held back by it")
        _StuckProc.exited = True
        ok(getattr(_p7, "_stuck_tools", lambda: None)() == set(), "once they exit, nothing counts as stuck")
        _p7._run_cmd(["/usr/bin/iw", "dev", "wlan0", "link"], timeout=0.01)
        ok(_StuckProc.spawned == 3, "and iw runs again")
        # A wrapper that exits while what it started keeps its output open
        # (runuser, say) is not stuck, and is not said to be.
        _logged7.clear()
        _p7 = m.Plugin(); _p7._run_cmd(["/usr/bin/runuser", "-u", "deck", "true"], timeout=0.01)
        ok(getattr(_p7, "_stuck_tools", lambda: None)() == set() and any("exited, but" in l for l in _logged7)
           and not any("did not exit" in l for l in _logged7),
           f"a wrapper that exited is not reported as stuck ({_logged7})")
    finally:
        m.subprocess.Popen = _real_popen
        m.decky.logger.info, m.decky.logger.error = _real_info7, _real_err7
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L8 Reset keeps the update channel and notices")
try:
    _d, _restore = _l_paths()
    try:
        m._save_settings({**m.DEFAULT_SETTINGS, "update_channel": "beta", "update_notify": False,
                          "update_notified_version": "0.24.6-beta"})
        asyncio.run(_LReset(left={}).reset_settings())
        _st8 = m._load_settings()
        ok((_st8["update_channel"], _st8["update_notify"], _st8["update_notified_version"])
           == ("beta", False, "0.24.6-beta"),
           f"beta, notices off and the version announced all survive Reset "
           f"({_st8['update_channel']}, {_st8['update_notify']}, {_st8['update_notified_version']!r})")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L9 turning Prevent lag spikes off leaves power save off where Steam has it off")
try:
    _d, _restore = _l_paths()
    try:
        class _L9(_LTear):
            def _get_wifi_interface(self): return "wlan0"
            def _reload_nm_conf(self): pass
        _on_cmd = ["/usr/bin/iw", "dev", "wlan0", "set", "power_save", "on"]
        _l_valve("2")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                          "valve_powersave_before": "2"})
        _p9 = _L9(); asyncio.run(_p9.set_power_save(False))
        ok(_on_cmd not in _p9.cmds, f"Steam's own setting has it off: the radio's power save stays off ({_p9.cmds})")
        # Our 2, copied into Valve's file by a backend switch: it goes back
        # to the user's 3, so power save comes back on.
        _l_valve("2"); open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                          "valve_powersave_before": "3"})
        _p9 = _L9(); asyncio.run(_p9.set_power_save(False))
        ok(_on_cmd in _p9.cmds and m.Plugin()._valve_powersave() == "3",
           "a 2 that was ours is given back, and power save turned back on")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L10 Valve's line goes back before NetworkManager rereads its config, every time")
try:
    _d, _restore = _l_paths()
    try:
        class _L10(_LTear):
            def __init__(self): super().__init__(); self.events = []
            def _set_valve_powersave(self, value):
                self.events.append(("valve", _l_thr.current_thread() is _l_thr.main_thread()))
                return m.Plugin._set_valve_powersave(self, value)
            def _run_cmd(self, cmd, timeout=5):
                if cmd[-3:] == ["general", "reload", "conf"]:
                    self.events.append(("reload", _l_thr.current_thread() is _l_thr.main_thread()))
                return _ok()
        for _label, _call in (("turning it off", lambda p: asyncio.run(p.set_power_save(False))),
                              ("auto mode dropping the files", lambda p: p._remove_persistent_power_save())):
            _l_valve("2"); open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
            m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                              "valve_powersave_before": "3"})
            _p10 = _L10(); _call(_p10)
            _names = [e[0] for e in _p10.events]
            ok(_names == ["valve", "reload"], f"{_label}: Valve's line, then one reload ({_names})")
        _l_valve("2"); open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                          "valve_powersave_before": "3"})
        _p10 = _L10(); asyncio.run(_p10.set_power_save(False))
        ok(("reload", False) in _p10.events, "and the toggle's reload runs off the event loop")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L11 an off during a slow on is not undone by the on")
try:
    _d, _restore = _l_paths()
    try:
        class _L11(_LTear):
            def __init__(self): super().__init__(); self.fixes = []; self.reloads = 0
            def _apply_driver_fixes(self, enable, persist=True, record_all=False):
                self.fixes.append(enable)
            def _reload_nm_conf(self):
                self.reloads += 1
                if self.reloads == 1:
                    time.sleep(0.4)
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": False})
        _p11 = _L11()
        async def _l11_race():
            on = asyncio.create_task(_p11.set_power_save(True))
            await _real_sleep11(0.15)
            off = asyncio.create_task(_p11.set_power_save(False))
            await asyncio.gather(on, off)
        _real_sleep11 = asyncio.sleep
        asyncio.run(_l11_race())
        ok(m._load_settings()["power_save_disabled"] is False and not os.path.exists(m.NM_CONF_PATH)
           and _p11.fixes[-1:] == [False],
           f"the later off stands: setting off, our file gone, fixes last taken off ({_p11.fixes})")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L12 the startup apply follows a change made while startup waited")
try:
    _d, _restore = _l_paths()
    try:
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "settings_version": 2,
                          "power_save_disabled": True})
        class _L12(_LBoot):
            async def _unpin_if_iwd(self):
                cur = m._load_settings(); cur["power_save_disabled"] = False; m._save_settings(cur)
        _b12 = _L12(); _l_main(_b12)
        ok(("power_save", True) not in _b12.calls,
           f"Prevent lag spikes turned off during startup is not put back on ({_b12.calls})")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L13 the end of a stream takes every fix off, whatever one step does")
try:
    class _L13(m.Plugin):
        def __init__(self): super().__init__(); self.done = []
        def _get_wifi_interface(self): return None
        def _apply_driver_fixes(self, enable, persist=True, record_all=False):
            raise OSError(_l_errno.EROFS, "Read-only file system")
        def _apply_pcie_aspm_fix(self, enable, record_all=False): self.done.append("pcie"); return []
        def _revert_queue_tuning(self, settings): self.done.append("queue")
    _p13 = _L13()
    try:
        _p13._revert_volatile_fixes({**m.DEFAULT_SETTINGS, "power_save_disabled": True,
                                     "buffer_tuning_enabled": True}); _raised13 = None
    except Exception as _e13:
        _raised13 = _e13
    ok(_raised13 is None and _p13.done == ["pcie", "queue"],
       f"a driver parameter that will not take still lets PCIe, buffer tuning and CAKE come off ({_p13.done}, {_raised13!r})")
    # The same for Reset's steps: one that raised ended Reset there, with the
    # defaults saved and the backend setting and the rest never reached.
    _d, _restore = _l_paths()
    try:
        class _L13Reset(_LReset):
            def _revert_queue_tuning(self, settings):
                self.order.append("queue"); raise OSError(_l_errno.EIO, "Input/output error")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _rs13 = _L13Reset(left={}); _res13 = asyncio.run(_rs13.reset_settings())
        ok("backend" in _rs13.order and _res13.get("success") is False
           and "couldn't be undone" in (_res13.get("message") or ""),
           f"Reset goes on past a step that fails, and says not everything came off ({_rs13.order}, {_res13})")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L14 a lock move that failed is rolled back though its record cannot be saved")
try:
    _U14 = "dddddddd-0000-0000-0000-0000000000e4"
    class _L14(m.Plugin):
        def __init__(self): super().__init__(); self.mods = []
        async def _await_association(self, iface): return {"iface": iface, "bssid": "", "link": "", "error": ""}
        def _nmcli_modify(self, uuid, key, value, timeout=5): self.mods.append((uuid, key, value)); return _ok()
        def _hard_reconnect(self, uuid=None): return True
    _real_write14 = m._write_settings
    _n14 = {"writes": 0}
    def _first_fails(data):
        _n14["writes"] += 1
        if _n14["writes"] == 1:
            raise OSError(_l_errno.ENOSPC, "No space left on device")
        _real_write14(data)
    m._write_settings = _first_fails
    _p14 = _L14()
    try:
        asyncio.run(_p14._verify_lock_move("wlan0", _U14, "02:00:00:00:00:41", "02:00:00:00:00:42", True))
    except Exception:
        pass
    finally:
        m._write_settings = _real_write14
    ok((_U14, "802-11-wireless.bssid", "02:00:00:00:00:41") in _p14.mods,
       f"the previous access point is written back ({_p14.mods})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L15 nothing that writes the profile or Valve's file runs during a backend switch")
try:
    _U15 = "dddddddd-0000-0000-0000-0000000000e5"
    class _L15(m.Plugin):
        def __init__(self):
            super().__init__(); self.mods = []
            self._ensure_backend_switch_state(); self._backend_switch["in_progress"] = True
        def _nmcli_modify(self, uuid, key, value, timeout=5): self.mods.append((uuid, key, value)); return _ok()
    _st15 = {**m.DEFAULT_SETTINGS, "band_preference_enabled": True, "band_preference": "a",
             "bssid_lock_enabled": True}
    _p15 = _L15(); _status15 = {"live": {}, "drift": {}}
    _p15._apply_band({"uuid": _U15, "value": "a", "observed": ""}, _st15, _status15, {})
    _p15._apply_bssid_repoint({"uuid": _U15, "value": "02:00:00:00:00:51"}, _st15, _status15, {})
    ok(_p15.mods == [], f"a status pass writes no band and no BSSID while the backend switches ({_p15.mods})")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _r15 = asyncio.run(_L15().set_streaming_mode(True))
    ok(_r15.get("error") == "busy" and m._load_settings()["streaming_mode_enabled"] is False,
       "turning auto mode on waits for the switch")
    m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True})
    _r15 = asyncio.run(_L15().set_power_save(False))
    ok(_r15.get("error") == "busy" and m._load_settings()["power_save_disabled"] is True,
       "and so does turning Prevent lag spikes off, which can rewrite Valve's file")
    _d, _restore = _l_paths()
    _real_replace15 = os.replace
    _tmps15 = []
    def _spy_replace(src, dst, *a, **k):
        if dst == m.WIFI_BACKEND_CONF: _tmps15.append(src)
        return _real_replace15(src, dst, *a, **k)
    try:
        _l_valve("2")
        m.os.replace = _spy_replace
        m.Plugin()._set_valve_powersave("3"); m.Plugin()._set_valve_powersave("2")
    finally:
        m.os.replace = _real_replace15
        _restore()
    ok(len(_tmps15) == 2 and _tmps15[0] != _tmps15[1],
       f"two writers to Valve's file never share a temp name ({_tmps15})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L16 a process search that hangs does not hold up Reset")
try:
    _d, _restore = _l_paths()
    _ev16 = _l_thr.Event()
    try:
        class _L16(_LReset):
            def _find_stream(self, patterns):
                _ev16.wait(10); return None
        m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})
        _p16 = _L16(left={})
        async def _l16():
            search = asyncio.create_task(_p16._stream_pass())
            await asyncio.sleep(0.2)
            try:
                res = await asyncio.wait_for(_p16.reset_settings(), 3)
            except asyncio.TimeoutError:
                res = None
            _ev16.set(); await search
            return res
        _r16 = asyncio.run(_l16())
        ok(bool(_r16 and _r16.get("success")), f"Reset completes while the search is stuck ({_r16})")
    finally:
        _ev16.set(); _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L17 bounded waits on subprocesses run off the event loop")
try:
    _main_thread = _l_thr.main_thread()
    _U17 = "dddddddd-0000-0000-0000-0000000000e7"
    class _L17Assoc(m.Plugin):
        def __init__(self): super().__init__(); self.on_loop = []
        def _run_cmd(self, cmd, timeout=5):
            self.on_loop.append(_l_thr.current_thread() is _main_thread)
            return _ok("Connected to 02:00:00:00:00:71 (on wlan0)\n\tfreq: 5180\n")
    _a17 = _L17Assoc(); asyncio.run(_a17._await_association("wlan0"))
    ok(_a17.on_loop and not any(_a17.on_loop), f"waiting for an association reads the link off the loop ({_a17.on_loop})")

    _no_sleep_17_real = asyncio.sleep
    async def _no_sleep17(*_a, **_k): return None
    class _L17Band(m.Plugin):
        def __init__(self): super().__init__(); self.iw_on_loop = []
        def _backend_is_iwd(self): return False
        def _resolve_uuid(self, msg=None): return _U17, None
        def _get_profile_ssid(self, uuid, timeout=5): return "Home"
        def _band_unreachable(self, uuid, band): return None
        def _profile_band(self, uuid): return ""
        def _nmcli_modify(self, uuid, key, value, timeout=5): return _ok()
        def _hard_reconnect(self, uuid=None): return True
        def _get_wifi_interface(self): return "wlan0"
        def _get_active_connection_uuid(self): return _U17
        def _run_cmd(self, cmd, timeout=5):
            if cmd[-1] == "link":
                self.iw_on_loop.append(_l_thr.current_thread() is _main_thread)
            return _ok("Connected to 02:00:00:00:00:72 (on wlan0)\n\tfreq: 5180\n")
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True})
    asyncio.sleep = _no_sleep17
    try:
        _b17 = _L17Band(); asyncio.run(_b17.set_band_preference(True, "a"))
    finally:
        asyncio.sleep = _no_sleep_17_real
    ok(_b17.iw_on_loop and not any(_b17.iw_on_loop),
       f"re-locking after a band change reads the link off the loop ({_b17.iw_on_loop})")
    ok(m._load_settings().get("bssid_lock_value") == "02:00:00:00:00:72",
       "and records the access point it pinned")

    class _L17Reapply(m.Plugin):
        def __init__(self): super().__init__(); self.on_loop = []
        def _note(self, what): self.on_loop.append((what, _l_thr.current_thread() is _main_thread))
        def _get_active_connection_uuid(self): self._note("uuid"); return _U17
        def _get_profile_ssid(self, uuid, timeout=5): self._note("ssid"); return "Home"
        def _backend_is_iwd(self): self._note("iwd"); return False
        def _lock_is_for_this_network(self, settings, uuid, ssid, timeout=5): self._note("lock"); return False
    m._save_settings({**m.DEFAULT_SETTINGS, "auto_fix_on_wake": False, "power_save_disabled": False,
                      "bssid_lock_enabled": True, "bssid_lock_connection_uuid": "x",
                      "band_preference_enabled": True, "band_preference_ssid": "Other"})
    _ra17 = _L17Reapply(); asyncio.run(_ra17.reapply_all())
    ok({"uuid", "ssid", "iwd"} <= {k for k, _v in _ra17.on_loop}
       and not any(v for _k, v in _ra17.on_loop),
       f"Force Reapply reads the profile, network and backend off the loop ({_ra17.on_loop})")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L17 startup's backend check runs off the event loop")
try:
    _main_thread = _l_thr.main_thread()
    _d, _restore = _l_paths()
    try:
        class _L17Boot(_LBoot):
            backend_method = "steamos"
            def __init__(self): super().__init__(); self.on_loop = []
            def _get_current_backend(self):
                self.on_loop.append(("backend", _l_thr.current_thread() is _main_thread)); return "iwd"
            def _run_cmd(self, cmd, timeout=5):
                if "is-active" in cmd:
                    self.on_loop.append(("is-active", _l_thr.current_thread() is _main_thread))
                return _ok("inactive")
        m._save_settings({**m.DEFAULT_SETTINGS, "settings_version": 2})
        _bb17 = _L17Boot(); _l_main(_bb17)
        ok(_bb17.on_loop and not any(v for _k, v in _bb17.on_loop),
           f"startup's backend check runs off the loop ({_bb17.on_loop})")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L18 a busy refusal says what it is waiting for")
try:
    class _L18(m.Plugin):
        def _resolve_uuid(self, msg=None): return "dddddddd-0000-0000-0000-0000000000e8", None
    _p18 = _L18(); _p18._update_starting = True
    _r18 = asyncio.run(_p18.set_dns(True, "cloudflare", ""))
    ok(_r18.get("error") == "busy" and "update" in (_r18.get("message") or "").lower(),
       f"during an update the refusal names the update ({_r18.get('message')!r})")
    _p18 = _L18(); _p18._ensure_backend_switch_state(); _p18._backend_switch["in_progress"] = True
    _r18 = asyncio.run(_p18.set_ipv6(True))
    ok(_r18.get("error") == "busy" and "backend switch" in (_r18.get("message") or ""),
       f"during a backend switch it names the switch ({_r18.get('message')!r})")
    _p18 = _L18(); _p18._profile_change_depth = 1
    _r18 = asyncio.run(_p18.set_ipv6(True))
    ok(_r18.get("message") == m.Plugin._BUSY_RESULT["message"], "and otherwise is as before")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L19 uninstall finishes inside Decky's budget, and never sets our backend file aside")
try:
    class _SlowProc:
        """Every command takes 3 s, unless given less."""
        def __init__(self, cmd, **kw):
            self.cmd = cmd; self.stdout = None; self.stderr = None; self.killed = False
            self.returncode = None; self.pid = 4242425
        def communicate(self, timeout=None):
            if self.killed:
                self.returncode = -9; return "", ""
            if timeout is not None and timeout < 3:
                time.sleep(timeout); raise m.subprocess.TimeoutExpired(self.cmd, timeout)
            time.sleep(3); self.returncode = 0; return "", ""
        def kill(self): self.killed = True
        def poll(self): return self.returncode
    _d, _restore = _l_paths()
    _real_popen19 = m.subprocess.Popen
    _logged19 = []
    _real_err19 = m.decky.logger.error
    try:
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _aside19 = []
        class _L19(m.Plugin):
            def _merged_nm_backend(self, *a, **k):
                _aside19.append([f for f in os.listdir(os.path.dirname(m.GENERIC_BACKEND_CONF))
                                 if "aside" in f])
                return None
            def _get_wifi_interface(self): return None
            def _remove_dispatcher(self): return True
        m.subprocess.Popen = _SlowProc
        m.decky.logger.error = lambda msg: _logged19.append(msg)
        _t19 = time.monotonic()
        _L19()._teardown(time.monotonic() + 4.0)
        _took19 = time.monotonic() - _t19
    finally:
        m.subprocess.Popen = _real_popen19
        m.decky.logger.error = _real_err19
        _restore()
    ok(_took19 < 4.5, f"commands that take 3 s each do not carry uninstall past its budget ({_took19:.1f}s)")
    ok(any("ran out of time before" in l for l in _logged19),
       f"the steps it had no time for are logged as skipped ({_logged19[:3]})")
    ok(_aside19 and not any(_aside19), f"our backend file is never renamed aside ({_aside19})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L19 Reset and uninstall budget DNS as the three calls it makes")
try:
    _U19 = "dddddddd-0000-0000-0000-0000000000e9"
    class _L19Dns(m.Plugin):
        def _nmcli_modify(self, uuid, key, value, timeout=5):
            time.sleep(timeout); return _ok()
    _t19 = time.monotonic()
    _left19 = _L19Dns()._clear_profile_pins(time.monotonic() + 1.2,
                                            {**m.DEFAULT_SETTINGS, "dns_uuids": [_U19]})
    _took19 = time.monotonic() - _t19
    ok(_took19 < 1.3 and _left19.get("dns_uuids") == [_U19],
       f"DNS is budgeted as its three calls, and left for later when they do not fit ({_took19:.1f}s)")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L20 Reset forgets the kinds of pin it took off everywhere")
try:
    _d, _restore = _l_paths()
    try:
        _both = ["802-11-wireless.bssid", "802-11-wireless.band"]
        m._save_settings({**m.DEFAULT_SETTINGS, "pins_owned": _both})
        asyncio.run(_LReset(left={"bssid_lock_uuids": [], "band_preference_uuids": []}).reset_settings())
        ok(m._load_settings()["pins_owned"] == [], "every pin off: no kind is kept as ours")
        _U20 = "dddddddd-0000-0000-0000-0000000000a0"
        m._save_settings({**m.DEFAULT_SETTINGS, "pins_owned": _both})
        asyncio.run(_LReset(left={"bssid_lock_uuids": [_U20], "band_preference_uuids": []}).reset_settings())
        ok(m._load_settings()["pins_owned"] == ["802-11-wireless.bssid"],
           "a BSSID left on a profile keeps only that kind")
        # Uninstall keeps a settings file for what it could not take off, and
        # the same rule holds for the kinds it records as ours.
        class _L20Tear(_LTear):
            def _clear_profile_pins(self, deadline=None, settings=None):
                return {"bssid_lock_uuids": [], "band_preference_uuids": [],
                        "dns_uuids": ["dddddddd-0000-0000-0000-0000000000a1"]}
        m._save_settings({**m.DEFAULT_SETTINGS, "pins_owned": _both})
        _L20Tear()._teardown(time.monotonic() + 4)
        ok(os.path.exists(m.SETTINGS_FILE) and m._load_settings()["pins_owned"] == [],
           "uninstall leaving only DNS behind keeps no pin kind as ours")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L21 Reset saves the current settings version")
try:
    _d, _restore = _l_paths()
    try:
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        asyncio.run(_LReset(left={}).reset_settings())
        ok(json.load(open(m.SETTINGS_FILE)).get("settings_version") == m.Plugin._SETTINGS_VERSION,
           "so the next load does not migrate again")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L22 a settings file that cannot be read is kept, not overwritten")
try:
    _d, _restore = _l_paths()
    try:
        _big22 = json.dumps({**m.DEFAULT_SETTINGS, "bssid_lock_uuids": ["dddddddd-0000-0000-0000-0000000000b2"],
                             "streaming_custom_patterns": "x" * 71680})
        open(m.SETTINGS_FILE, "w").write(_big22)
        _l_main(_LBoot())
        _corrupt22 = m.SETTINGS_FILE + ".corrupt"
        ok(os.path.isfile(_corrupt22) and open(_corrupt22).read() == _big22,
           "a 70 KiB file is kept as settings.json.corrupt after a load")
        ok(sorted(m._load_settings()["pins_owned"]) == ["802-11-wireless.band", "802-11-wireless.bssid"],
           "and the pins it named count as ours, as for a file that does not parse")
        if os.path.exists(_corrupt22): os.remove(_corrupt22)
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": True})
            os.chmod(m.SETTINGS_FILE, 0)
            try:
                m._load_settings()
                try:
                    m._save_settings(dict(m.DEFAULT_SETTINGS)); _refused22 = False
                except OSError:
                    _refused22 = True
            finally:
                os.chmod(m.SETTINGS_FILE, 0o644)
            ok(_refused22 and json.load(open(m.SETTINGS_FILE)).get("dns_enabled") is True,
               "a file that could not be read is not replaced by the defaults that stood in for it")
            m._load_settings()
            m._save_settings(dict(m.DEFAULT_SETTINGS))
            ok(json.load(open(m.SETTINGS_FILE)).get("dns_enabled") is False,
               "and once it reads again, saves go through")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L23 Reset and uninstall clear an owned BSSID pin on the profile last in use")
try:
    _L23 = "dddddddd-0000-0000-0000-0000000000c3"
    class _L23Nm(m.Plugin):
        def __init__(self): super().__init__(); self.mods = []
        def _nmcli_modify(self, uuid, key, value, timeout=5): self.mods.append((uuid, key, value)); return _ok()
    _p23 = _L23Nm()
    _p23._clear_profile_pins(None, {**m.DEFAULT_SETTINGS, "last_connection_uuid": _L23,
                                    "pins_owned": ["802-11-wireless.bssid"]})
    ok((_L23, "802-11-wireless.bssid", "") in _p23.mods, f"its BSSID is cleared ({_p23.mods})")
    _p23 = _L23Nm()
    _p23._clear_profile_pins(None, {**m.DEFAULT_SETTINGS, "last_connection_uuid": _L23, "pins_owned": []})
    ok(not any(k == "802-11-wireless.bssid" for _u, k, _v in _p23.mods),
       "but not when this install never wrote one")
    class _L23Off(_L23Nm):
        def _require_wifi(self): return "wlan0", "dddddddd-0000-0000-0000-0000000000c4", None
        def _hard_reconnect(self, uuid=None): return True
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True, "last_connection_uuid": _L23,
                      "pins_owned": ["802-11-wireless.bssid"]})
    _p23 = _L23Off(); asyncio.run(_p23.set_bssid_lock(False))
    ok((_L23, "802-11-wireless.bssid", "") in _p23.mods,
       "and turning the lock off sweeps it too")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L24 an update channel that is not one of the two loads as stable")
try:
    ok(m._clean_settings({**m.DEFAULT_SETTINGS, "update_channel": "Beta"})["update_channel"] == "stable"
       and m._clean_settings({**m.DEFAULT_SETTINGS, "update_channel": "beta"})["update_channel"] == "beta",
       "\"Beta\" loads as stable, \"beta\" as beta")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L25 uninstall removes a backend switch marker it cut off")
try:
    _d, _restore = _l_paths()
    try:
        open(m.BACKEND_SWITCH_MARKER, "w").write("wpa_supplicant iwd\n")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _LTear()._teardown(time.monotonic() + 4)
        ok(not os.path.exists(m.BACKEND_SWITCH_MARKER), "a reinstall in the same boot finds no switch to settle")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L26 L27 the update script waits on the streams the user watches, and names the release right")
try:
    class _L26(m.Plugin):
        def __init__(self, version):
            super().__init__(); self.version = version; self.launched = []
        async def check_for_update(self):
            return {"update_available": True, "channel": "beta", "latest_version": self.version}
        async def _signed_release(self, version):
            return {"ok": True, "url": f"https://example.invalid/v{version}.tar.gz", "sha256": "0" * 64}
        def _launch_updater(self, argv): self.launched.append(argv)
    def _l26_apply(version):
        p = _L26(version)
        r = asyncio.run(p.apply_update(version))
        argv = list(p.launched[0]) if p.launched else []
        # The patterns travel in a file beside the script since L1 (pass 4),
        # read here and added to the end of the argv for the checks below.
        if getattr(p, "_update_dir", None) and os.path.isdir(p._update_dir):
            _sf = os.path.join(p._update_dir, "streams")
            argv.append(open(_sf).read().strip() if os.path.exists(_sf) else "")
            _l_sh.rmtree(p._update_dir)
        p._update_dir = None
        return r, argv
    _moon = [pt for app, _l, pts in m.STREAMING_APPS if app == "moonlight" for pt in pts]
    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_apps": {"moonlight": False},
                      "streaming_custom_patterns": "mystreamer"})
    _r26, _argv26 = _l26_apply("0.24.6-beta")
    ok(_r26.get("success") and _argv26 and "mystreamer" in _argv26[-1]
       and not any(pt.replace(".", "\\.") in _argv26[-1] for pt in _moon),
       f"the script is handed the user's own name and not an unticked app's ({_argv26[-1:]})")
    _r27, _argv27 = _l26_apply("0.24.5")
    ok(_r27.get("success") and _argv27[6:7] == ["v0.24.5"],
       f"a stable release offered on the beta channel is not called beta ({_argv27[6:7]})")
    ok(_argv26[6:7] == ["beta v0.24.6-beta"], "and a beta release is")
    # The script's own command line carries the patterns, so it must not
    # count itself as the stream it waits for.
    import subprocess as _l_sp
    _fn26 = m.UPDATE_SCRIPT[m.UPDATE_SCRIPT.index("playing() {"):]
    _fn26 = _fn26[:_fn26.index("\n}\n") + 3]
    def _l26_playing(pids):
        args = os.path.join(tempfile.mkdtemp(), "args")
        body = ('STREAM_PATTERNS="mystreamer"\nTMP=$(mktemp -d)\n'
                'pgrep() { echo "$*" > "$ARGS"; for p in ' + pids + '; do echo "$p"; done; }\n'
                + _fn26.replace("/run/wifi-optimizer/streaming", "/nonexistent/streaming")
                + "if playing; then echo yes; else echo no; fi\n")
        out = _l_sp.run(["/bin/bash", "-c", body], capture_output=True, text=True,
                        env={**os.environ, "ARGS": args}).stdout.strip()
        return out, (open(args).read().strip() if os.path.exists(args) else "")
    _self26, _args26 = _l26_playing("$$")
    ok(_self26 == "no" and "mystreamer" in _args26,
       f"the script looks for the names it was given, and does not count itself ({_self26}, {_args26!r})")
    ok(_l26_playing("1")[0] == "yes", "another process that matches counts")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L28 a refused process name is told why")
try:
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _r28 = asyncio.run(m.Plugin().set_streaming_patterns("x" * 70))
    ok("too long" in (_r28.get("message") or "") and "at least" not in (_r28.get("message") or ""),
       f"a 70-character name is too long, and only that is said ({_r28.get('message')!r})")
    _r28 = asyncio.run(m.Plugin().set_streaming_patterns("ab steam"))
    _msg28 = _r28.get("message") or ""
    ok("too short" in _msg28 and "always running" in _msg28 and "too long" not in _msg28,
       f"each name gets its own reason ({_msg28!r})")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L29 messages carry no system names and no escapes")
try:
    _d, _restore = _l_paths()
    try:
        class _L29Ps(_LTear):
            def _apply_pcie_aspm_fix(self, enable, record_all=False): return ["l1_2_aspm", "control"]
            def _reload_nm_conf(self): pass
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _msg29 = asyncio.run(_L29Ps().set_power_save(True)).get("message") or ""
        ok(_msg29 and "l1_2_aspm" not in _msg29 and "control" not in _msg29,
           f"PCIe states that did not take are said in words ({_msg29!r})")
    finally:
        _restore()
    class _L29Buf(m.Plugin):
        def _get_wifi_interface(self): return None
        def _run_cmd(self, cmd, timeout=5):
            if cmd[:2] == ["/usr/bin/sysctl", "-w"] and "rmem_max" in cmd[2]:
                return {"success": False, "stdout": "", "stderr": "nope", "returncode": 1}
            return _ok("1")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _msg29 = asyncio.run(_L29Buf().set_buffer_tuning(True)).get("message") or ""
    ok(_msg29 and "rmem" not in _msg29 and "net." not in _msg29,
       f"buffer settings that did not take likewise ({_msg29!r})")
    class _L29Band(m.Plugin):
        def _backend_is_iwd(self): return False
        def _resolve_uuid(self, msg=None): return "dddddddd-0000-0000-0000-0000000000d9", None
        def _get_profile_ssid(self, uuid, timeout=5): return "Home"
    m._save_settings({**m.DEFAULT_SETTINGS, "band_preference_enabled": True,
                      "band_preference_ssid": "Cafe\\:Guest"})
    _msg29 = asyncio.run(_L29Band().set_band_preference(True, "a")).get("message") or ""
    ok("Cafe:Guest" in _msg29 and "\\" not in _msg29,
       f"a network name is shown as people know it ({_msg29!r})")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L30 no iw on the system: Prevent lag spikes still applies the rest, and no drift")
try:
    _d, _restore = _l_paths()
    _U30 = "dddddddd-0000-0000-0000-0000000000d0"
    _NOIW = {"success": False, "stdout": "", "stderr": "Command not found: /usr/bin/iw", "returncode": -1}
    try:
        class _L30(_LTear):
            def __init__(self): super().__init__(); self.fixes = []
            def _get_wifi_interface(self): return "wlan0"
            def _reload_nm_conf(self): pass
            def _apply_driver_fixes(self, enable, persist=True, record_all=False): self.fixes.append(enable)
            def _run_cmd(self, cmd, timeout=5):
                return dict(_NOIW) if cmd[0].endswith("/iw") else _ok()
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _p30 = _L30(); _r30 = asyncio.run(_p30.set_power_save(True))
        ok(_r30.get("success") is True and "iw tool is missing" in (_r30.get("message") or "")
           and _p30.fixes == [True] and m._load_settings()["power_save_disabled"] is True,
           f"it says iw is missing and applies the driver fixes ({_r30})")
        class _L30Status(m.Plugin):
            def _get_wifi_interface(self): return "wlan0"
            def _get_active_connection_uuid(self): return _U30
            def _run_cmd(self, cmd, timeout=5):
                if cmd[0].endswith("/iw"):
                    return dict(_NOIW)
                if "802-11-wireless.ssid" in cmd: return _ok("802-11-wireless.ssid:Home")
                return _ok()
            def _nmcli_modify(self, *a, **k): return _ok()
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True, "driver": "rtw88",
                          "distro_id": "steamos"})
        _st30 = asyncio.run(_L30Status().get_status())
        ok(_st30.get("success") is True and not _st30.get("drift", {}).get("power_save"),
           f"and a power save read that could not run is not drift ({_st30.get('drift')}, {_st30.get('message')})")
    finally:
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L31 Allow WPA2 says when the lock could not move after it")
try:
    _W31 = "dddddddd-0000-0000-0000-0000000000d1"
    class _L31(m.Plugin):
        def __init__(self):
            super().__init__(); self._wpa2_blocked = {"signal": 80, "current_signal": 40}
            self._reach_checked_for = _W31; self.km = "sae"
        def _resolve_uuid(self, msg=None): return _W31, None
        def _get_profile_ssid(self, uuid, timeout=5): return "Home"
        def _profile_key_mgmt(self, uuid): return self.km
        def _nmcli_modify(self, uuid, key, value, timeout=5): self.km = value; return _ok()
        async def _reconnected_to(self, uuid): return True
        async def set_bssid_lock(self, enabled): return dict(m.Plugin._BUSY_RESULT)
    m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True})
    _r31 = asyncio.run(_L31().allow_wpa2())
    ok(_r31.get("success") is True and "stayed where it was" in (_r31.get("message") or ""),
       f"WPA2 is allowed, and the panel is told the lock stayed put ({_r31})")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L32 under iwd Optimize Safe says the lock was left out, and the refusal says what to do")
try:
    class _L32(m.Plugin):
        def _backend_is_iwd(self): return True
        async def set_auto_fix(self, e): return {"success": True}
        async def set_buffer_tuning(self, e): return {"success": True}
        async def set_power_save(self, d): return {"success": True}
    _r32 = asyncio.run(_L32().optimize_safe())
    ok("wpa_supplicant" in ((_r32.get("results") or {}).get("bssid_lock") or {}).get("message", "")
       and _r32.get("total") == 3 and _r32.get("applied") == 3,
       f"the lock's result says why it was left out ({(_r32.get('results') or {}).get('bssid_lock')})")
    ok(m.Plugin._NEEDS_WPA_SUPPLICANT.rstrip().endswith("Switch to wpa_supplicant in Advanced to use it."),
       "and the iwd refusal ends with what to do")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L33 turning custom DNS off keeps the servers for next time")
try:
    _U33 = "dddddddd-0000-0000-0000-0000000000d3"
    class _L33(m.Plugin):
        def __init__(self): super().__init__(); self.mods = []
        def _resolve_uuid(self, msg=None): return _U33, None
        def _read_profile_field(self, uuid, field, timeout=5): return ""
        def _nmcli_modify(self, uuid, key, value, timeout=5): self.mods.append((key, value)); return _ok()
        def _hard_reconnect(self, uuid=None): return True
    m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": True, "dns_provider": "custom",
                      "dns_servers": "10.0.0.2", "dns_uuids": [_U33]})
    asyncio.run(_L33().set_dns(False))
    _st33 = m._load_settings()
    ok(_st33["dns_enabled"] is False and _st33["dns_provider"] == "custom" and _st33["dns_servers"] == "10.0.0.2",
       f"off keeps Custom and 10.0.0.2 ({_st33['dns_provider']}, {_st33['dns_servers']!r})")
    _p33 = _L33(); asyncio.run(_p33.set_dns(True, _st33["dns_provider"], _st33["dns_servers"]))
    ok(("ipv4.dns", "10.0.0.2") in _p33.mods, "and turning it back on applies 10.0.0.2 again")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L34 L35 messages end with a period and say what to do next")
try:
    _src34 = open("main.py").read()
    ok(all(f'"{t}."' in _src34 for t in ("Connect to WiFi first", "Connect to WiFi first to set band preference",
                                          "Connect to WiFi first to set DNS",
                                          "Connect to WiFi first to disable IPv6"))
       and not any(f'"{t}"' in _src34 for t in ("Connect to WiFi first",
                                                 "Connect to WiFi first to set band preference",
                                                 "Connect to WiFi first to set DNS",
                                                 "Connect to WiFi first to disable IPv6")),
       "the four connect-first messages end with a period")
    ok(_src34.count("Couldn't read the WiFi link. Try again in a moment. ") == 2
       and "Couldn't remove CAKE. Try again in a moment. " in _src34
       and '"Couldn\'t read the WiFi link."' not in _src34 and '"Couldn\'t remove CAKE."' not in _src34,
       "a link that could not be read and CAKE that would not come off say what to do next")
    _ur = _src34[_src34.index("    def _unexpected_response"):]
    _ur = _ur[:_ur.index("        if isinstance(e, SettingsSaveError)")]
    ok("appends `detail`" not in _ur, "the docstring no longer says the panel appends detail")
    _mn = _src34[_src34.index("    async def _main(self):"):_src34.index("    _SETTINGS_VERSION = ")]
    ok(_mn.index("# Sanity check") > _mn.index("band_preference_ssid")
       and _mn.index("# Sanity check") < _mn.index("_get_backend_method"),
       "the backend sanity check comment sits above the backend check")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L36 an update that slept is not called failed while its script still runs")
try:
    _real_state36 = m.UPDATE_STATE_FILE
    m.UPDATE_STATE_FILE = os.path.join(tempfile.mkdtemp(), "update-state")
    try:
        open(m.UPDATE_STATE_FILE, "w").write("downloading 0.24.6\n")
        _old36 = time.time() - 1000
        os.utime(m.UPDATE_STATE_FILE, (_old36, _old36))
        _p36 = m.Plugin(); _p36._update_dir = tempfile.mkdtemp(); _p36._update_started_at = time.monotonic() - 30
        ok(asyncio.run(_p36.get_update_progress()).get("phase") == "downloading",
           "1000 s by the clock, 30 s awake, its directory there: still downloading")
        os.rmdir(_p36._update_dir)
        ok(asyncio.run(_p36.get_update_progress()).get("phase") == "failed",
           "with its directory gone it is failed")
        ok(asyncio.run(m.Plugin().get_update_progress()).get("phase") == "failed",
           "and a backend that did not start it goes by the clock, as before")
    finally:
        m.UPDATE_STATE_FILE = _real_state36
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("J1 Allow WPA2 here is undone by Reset and uninstall, and not offered where it would lose WPA3")
try:
    _W1 = "dddddddd-0000-0000-0000-0000000000a1"
    class _J1Nm(m.Plugin):
        """key-mgmt per profile, as NetworkManager holds it."""
        def __init__(self, km=None, fail=False):
            super().__init__(); self.km = dict(km or {}); self.fail = fail; self.mods = []
        def _run_cmd(self, cmd, timeout=5):
            if "802-11-wireless-security.key-mgmt" in cmd:
                return _ok(f"802-11-wireless-security.key-mgmt:{self.km.get(cmd[-1], '')}")
            return _ok()
        def _nmcli_modify(self, uuid, key, value, timeout=5):
            self.mods.append((uuid, key, value))
            if self.fail:
                return {"success": False, "stdout": "", "stderr": "busy", "returncode": 1}
            if key == "802-11-wireless-security.key-mgmt":
                self.km[uuid] = value
            return _ok()
        def _get_wifi_interface(self): return None
        def _get_active_connection_uuid(self): return None
    _d, _restore = _l_paths()
    try:
        # Allowed through the panel: recorded.
        class _J1Allow(_J1Nm):
            def __init__(self):
                super().__init__({_W1: "sae"})
                self._wpa2_blocked = {"signal": 80, "current_signal": 40}; self._reach_checked_for = _W1
            def _resolve_uuid(self, msg=None): return _W1, None
            def _get_profile_ssid(self, uuid, timeout=5): return "Home"
            def _profile_key_mgmt(self, uuid): return self.km.get(uuid, "")
            async def _reconnected_to(self, uuid): return True
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _a1 = _J1Allow(); asyncio.run(_a1.allow_wpa2())
        ok(m._load_settings().get("wpa2_uuids") == [_W1], "a profile Allow WPA2 changed is recorded")
        _left1 = _J1Nm({_W1: "wpa-psk"})
        _l1 = _left1._clear_profile_pins(None, m._load_settings())
        ok((_W1, "802-11-wireless-security.key-mgmt", "sae") in _left1.mods and not _l1.get("wpa2_restore_uuids"),
           "Reset and uninstall write sae back on it")
        _left1 = _J1Nm({_W1: "wpa-eap"})
        _left1._clear_profile_pins(None, m._load_settings())
        ok(not _left1.mods, "unless its security has since been changed some other way")
        # Reset when NetworkManager refuses: kept, and retried by the status poll.
        _rs1 = type("_J1Reset", (_J1Nm, _LReset), {})
        _rs1 = _J1Nm.__new__(_rs1); _LReset.__init__(_rs1); _rs1.km = {_W1: "wpa-psk"}; _rs1.fail = True; _rs1.mods = []
        asyncio.run(_rs1.reset_settings())
        _st1 = m._load_settings()
        ok(_st1.get("wpa2_restore_uuids") == [_W1] and _st1.get("wpa2_uuids") == [],
           f"one Reset could not put back stays listed ({_st1.get('wpa2_restore_uuids')})")
        # Two polls since L9 (pass 4): one nmcli call each, the read, then the write.
        _poll1 = _J1Nm({_W1: "wpa-psk"}); asyncio.run(_poll1.get_status()); asyncio.run(_poll1.get_status())
        ok(_poll1.km[_W1] == "sae" and m._load_settings().get("wpa2_restore_uuids") == [],
           "and status polls put it back and let it go")
    finally:
        _restore()

    class _J1Reach(m.Plugin):
        def _visible_access_points(self, iface, ssid, cached_only=False):
            return [("AA:00:00:00:00:02", 85, 2437, "WPA2"),
                    ("AA:00:00:00:00:01", 40, 5180, "WPA3")]
    _old1 = _J1Reach(); _old1._nm_version_seen = (1, 56, 0)
    ok(_old1._wpa2_finding("wlan0", "Home", "aa:00:00:00:00:01", {}) is None,
       "with a WPA3-only access point in sight on NetworkManager 1.56 it is not offered")
    _new1 = _J1Reach(); _new1._nm_version_seen = (1, 58, 1)
    ok(_new1._wpa2_finding("wlan0", "Home", "aa:00:00:00:00:01", {}) == {"signal": 85, "current_signal": 40},
       "on 1.58.1, which joins WPA3-only access points with it, it is")
    class _J1Mixed(_J1Reach):
        def _visible_access_points(self, iface, ssid, cached_only=False):
            return [("AA:00:00:00:00:02", 85, 2437, "WPA2"),
                    ("AA:00:00:00:00:01", 40, 5180, "WPA2 WPA3")]
    _mix1 = _J1Mixed(); _mix1._nm_version_seen = (1, 56, 0)
    ok(_mix1._wpa2_finding("wlan0", "Home", "aa:00:00:00:00:01", {}) is not None,
       "and on 1.56 where every access point still takes WPA2, it is too")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("J2 custom DNS covers IPv6 too")
try:
    _U2 = "dddddddd-0000-0000-0000-0000000000a2"
    class _J2(m.Plugin):
        def __init__(self, read=""):
            super().__init__(); self.mods = []; self.read = read
        def _resolve_uuid(self, msg=None): return _U2, None
        def _read_profile_field(self, uuid, field, timeout=5): return ""
        def _nmcli_modify(self, uuid, key, value, timeout=5): self.mods.append((uuid, key, value)); return _ok()
        def _hard_reconnect(self, uuid=None): return True
        def _run_cmd(self, cmd, timeout=5): return _ok(self.read)
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _p2 = _J2(); asyncio.run(_p2.set_dns(True, "quad9", ""))
    ok((_U2, "ipv6.ignore-auto-dns", "yes") in _p2.mods, "turning it on ignores the router's IPv6 DNS too")
    _p2 = _J2(); asyncio.run(_p2.set_dns(False))
    ok((_U2, "ipv6.ignore-auto-dns", "no") in _p2.mods, "turning it off lets it back in")
    _p2 = _J2(); _p2._clear_dns(_U2)
    ok((_U2, "ipv6.ignore-auto-dns", "no") in _p2.mods, "and so do Reset and uninstall")
    def _j2_drift(v6):
        p = _J2("ipv4.dns:9.9.9.9,149.112.112.112\nipv4.ignore-auto-dns:yes\n"
                f"ipv6.ignore-auto-dns:{v6}")
        st = {"live": {}, "drift": {}}
        p._check_dns_drift({**m.DEFAULT_SETTINGS, "dns_enabled": True,
                            "dns_servers": "9.9.9.9 149.112.112.112", "dns_uuids": [_U2]}, _U2, st)
        return bool(st["drift"].get("dns"))
    ok(_j2_drift("no") is True and _j2_drift("yes") is False,
       "the router's IPv6 DNS let back in is drift, and kept out is not")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

# ---- BACKEND-DEVICES, QA pass 3 (items D1-D21, J3, U1) ----
import subprocess as _dsp
_d_real_sleep = asyncio.sleep
_d_real_err, _d_real_info = m.decky.logger.error, m.decky.logger.info

def _d_template():
    return open("defaults/dispatcher.sh.tmpl").read()

def _d_filled():
    t = _d_template().replace("__SETTINGS_PATH__",
                              m._sh_quote("/home/deck/homebrew/settings/WiFi Optimizer/settings.json"))
    return t.replace("__PLUGIN_DIR__", m._sh_quote("/home/deck/homebrew/plugins/WiFi Optimizer"))

# A fake sysfs and a stub PATH to run parts of the dispatcher for real.
def _d_block(start, end):
    t = _d_template()
    return t[t.index(start):t.index(end)]

def _d_run(snippet, env_lines, stubs, root):
    """Run a piece of the dispatcher in bash with tools from stubs (name ->
    exit code, every call logged) ahead of the system's, and /sys/module and
    /run/wifi-optimizer moved under root. Returns (stdout, log lines)."""
    bindir = os.path.join(root, "bin"); os.makedirs(bindir, exist_ok=True)
    log = os.path.join(root, "calls.log")
    for name, code in stubs.items():
        path = os.path.join(bindir, name)
        open(path, "w").write(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit {code}\n')
        os.chmod(path, 0o755)
    body = (snippet.replace("/sys/module/", root + "/module/")
                   .replace("/run/wifi-optimizer/", root + "/run/"))
    script = (f'export PATH="{bindir}:$PATH"\n' + "\n".join(env_lines) + "\n" + body
              + '\necho "APPLIED=$APPLIED OUR_CARD=$OUR_CARD FIRST_RUN=$FIRST_RUN"\n')
    out = _dsp.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=20).stdout
    return out, (open(log).read().splitlines() if os.path.exists(log) else [])

def _d_netdev(root, name, module, operstate="up", link=("l1_aspm",), control="file", bus="pci"):
    """A netdev under root/net bound to a device whose driver module is module,
    on the bus named (its subsystem link, as sysfs gives every device)."""
    dev = os.path.join(root, "devices", name)
    os.makedirs(os.path.join(dev, "link"), exist_ok=True)
    os.makedirs(os.path.join(dev, "power"), exist_ok=True)
    for f in link:
        open(os.path.join(dev, "link", f), "w").write("1\n")
    if control == "file":
        open(os.path.join(dev, "power", "control"), "w").write("auto\n")
    else:
        os.makedirs(os.path.join(dev, "power", "control"), exist_ok=True)
    mod = os.path.join(root, "modules", module); os.makedirs(mod, exist_ok=True)
    os.makedirs(os.path.join(dev, "driver"), exist_ok=True)
    if not os.path.islink(os.path.join(dev, "driver", "module")):
        os.symlink(mod, os.path.join(dev, "driver", "module"))
    if bus:
        os.makedirs(os.path.join(root, "bus", bus), exist_ok=True)
        if not os.path.islink(os.path.join(dev, "subsystem")):
            os.symlink(os.path.join(root, "bus", bus), os.path.join(dev, "subsystem"))
    net = os.path.join(root, "net", name)
    os.makedirs(os.path.join(net, "phy80211"), exist_ok=True)
    if not os.path.islink(os.path.join(net, "device")):
        os.symlink(dev, os.path.join(net, "device"))
    open(os.path.join(net, "operstate"), "w").write(operstate + "\n")
    return dev

section("D1 the generic backend switch brings the WiFi interface back")
try:
    class IwdTakesIface(m.Plugin):
        """A generic system whose radio iwd owns: stopping iwd destroys the
        netdev, and wpa_supplicant never makes one. Only an `iw phy ...
        interface add` (here _recover_wlan0) brings it back."""
        _SETTLE_SECONDS = 0
        _IWD_INTERFACE_WAIT = 0
        def __init__(self, recovers=True, fail=()):
            super().__init__(); self._ensure_backend_switch_state()
            self.iface = "wlan0"; self.recovers = recovers; self.recovered = 0
            self.cmds = []; self.fail = fail
        def _run_cmd(self, cmd, timeout=5):
            self.cmds.append(cmd)
            if cmd[1:3] == ["stop", "iwd"] or cmd[1:4] == ["disable", "--now", "iwd"]:
                self.iface = None
            if tuple(cmd[1:3]) in self.fail:
                return {"success": False, "stdout": "", "stderr": "failed", "returncode": 1}
            return _ok()
        def _get_wifi_interface(self): return self.iface
        def _get_active_connection_uuid(self): return "u" if self.iface else None
        def _recover_wlan0(self):
            self.recovered += 1
            if self.recovers: self.iface = "wlan0"
            return self.recovers
        def _merged_nm_backend(self): return None
        def _effective_generic_backend(self): return "wpa_supplicant"
    _rc_d1 = m.GENERIC_BACKEND_CONF
    asyncio.sleep = _no_sleep
    try:
        m.GENERIC_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "99-backend.conf")
        g1 = IwdTakesIface(); asyncio.run(g1._generic_backend_switch_worker("wpa_supplicant"))
        r1 = g1._backend_switch["result"] or {}
        ok(r1.get("success") is True and r1.get("recovery_performed") is True
           and g1.recovered == 1 and r1.get("reconnect_timed_out") is False,
           f"iwd to wpa_supplicant recreates the interface iwd took with it, and says so ({r1})")
        os.path.exists(m.GENERIC_BACKEND_CONF) and os.remove(m.GENERIC_BACKEND_CONF)
        g2 = IwdTakesIface(recovers=False); asyncio.run(g2._generic_backend_switch_worker("wpa_supplicant"))
        r2 = g2._backend_switch["result"] or {}
        ok(r2.get("success") is False and r2.get("needs_reboot") is True,
           f"and when it cannot, the switch is not reported as a success with no interface ({r2})")
        os.path.exists(m.GENERIC_BACKEND_CONF) and os.remove(m.GENERIC_BACKEND_CONF)
        # A failure branch that puts wpa_supplicant back after iwd took the interface.
        class IwdToWpaFails(IwdTakesIface):
            def _effective_generic_backend(self): return None
        g3 = IwdToWpaFails(); g3.iface = "wlan0"
        asyncio.run(g3._generic_backend_switch_worker("iwd"))
        r3 = g3._backend_switch["result"] or {}
        ok(r3.get("success") is False and "needs_reboot" in r3 and "recovery_performed" in r3,
           f"a switch that is undone reports whether the interface came back ({r3})")
        g4 = IwdTakesIface(); g4.iface = None
        _back = g4._restore_generic_backend("wpa_supplicant")
        ok(_back == (True, True) and g4.recovered == 1,
           f"putting wpa_supplicant back recreates a missing interface ({_back})")
        g5 = IwdTakesIface(recovers=False); g5.iface = None
        ok(g5._restore_generic_backend("wpa_supplicant") == (False, False),
           "and says when there is none")
        open(m.BACKEND_SWITCH_MARKER, "w").write("wpa_supplicant iwd\n")
        g6 = IwdTakesIface(); g6.iface = None
        g6._reconcile_backend_switch()
        ok(g6.recovered == 1, "a switch cut off part way is undone with the interface brought back too")
    finally:
        asyncio.sleep = _d_real_sleep
        m.GENERIC_BACKEND_CONF = _rc_d1
        if os.path.exists(m.BACKEND_SWITCH_MARKER): os.remove(m.BACKEND_SWITCH_MARKER)

    # SteamOS's helper: the interface is checked on every driver, not only ath11k.
    _rv_d1 = m.WIFI_BACKEND_CONF
    try:
        m.WIFI_BACKEND_CONF = os.path.join(tempfile.mkdtemp(), "99-valve.conf")
        class HelperNoIface(m.Plugin):
            def __init__(self):
                super().__init__(); self._ensure_backend_switch_state()
                self.iface = None; self.recovered = 0
            def _run_cmd(self, cmd, timeout=5): return _ok()
            def _get_wifi_interface(self): return self.iface
            def _get_active_connection_uuid(self): return "u" if self.iface else None
            def _recover_wlan0(self):
                self.recovered += 1; self.iface = "wlan0"; return True
            def _get_current_backend(self): return "wpa_supplicant"
            def _valve_powersave(self): return None
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "mt7921e", "distro_id": "steamos"})
        asyncio.sleep = _no_sleep
        try:
            hn = HelperNoIface(); asyncio.run(hn._backend_switch_worker("wpa_supplicant"))
        finally:
            asyncio.sleep = _d_real_sleep
        rh = hn._backend_switch["result"] or {}
        ok(hn.recovered == 1 and rh.get("recovery_performed") is True and rh.get("success") is True,
           f"the SteamOS helper path recovers a missing interface on mt7921e too ({rh})")
    finally:
        m.WIFI_BACKEND_CONF = _rv_d1
        m._save_settings(dict(m.DEFAULT_SETTINGS))
    import inspect as _d_insp
    _hsrc = _d_insp.getsource(m.Plugin._backend_switch_worker)
    ok("has_wlan0_quirk" not in _hsrc and all(ord(c) < 128 for c in _hsrc),
       "with no ath11k-only interface check, and a plain ASCII docstring")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    asyncio.sleep = _d_real_sleep

section("D2 diagnostics say what the driver loaded with in the boot that failed")
try:
    _t2 = _d_template()
    _ps2 = _t2[_t2.index("# Apply power save"):_t2.index("# Apply buffer tuning")]
    # Pass 4 D4: "at load" is decided by the per-boot marker, not FIRST_RUN.
    ok('logger -t "$LOG_TAG" "driver parameters at load:$BEFORE"' in _ps2
       and _ps2.index('if claim_at_load; then\n            logger -t "$LOG_TAG" "driver parameters at load:')
       < _ps2.index("driver parameters before applying"),
       "the dispatcher's first run of a boot marks its note of the parameters 'at load:'")
    _marker = "[  3.100000] wifi-optimizer[812]: driver parameters at load: disable_lps_deep=N disable_aspm=N"
    _plog = [_marker] + [f"[ {10 + i}.000000] wifi-optimizer[9{i:02d}]: driver parameters before applying: "
                         f"disable_lps_deep=Y disable_aspm=Y" for i in range(49)]
    class LoadDiag(m.Plugin):
        grep_works = True
        def _get_wifi_interface(self): return "wlan0"
        async def get_device_info(self): return {"success": True, "driver": "rtw88"}
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/journalctl" and "-t" in cmd:
                if "--grep" in cmd:
                    if not self.grep_works:
                        return {"success": False, "stdout": "", "returncode": 1,
                                "stderr": "Compiled without pattern matching support"}
                    pat = cmd[cmd.index("--grep") + 1]
                    return _ok("\n".join(l for l in _plog if pat in l) + "\n")
                return _ok("\n".join(_plog) + "\n")
            return _ok()
    for _gw in (True, False):
        LoadDiag.grep_works = _gw
        _ld = m.Plugin._diagnostic_extras(LoadDiag(), "rtw88", "wlan0")
        _lv = _ld.get("plugin_load_values_previous_boot", [])
        ok(len(_lv) == 1 and "at load: disable_lps_deep=N disable_aspm=N" in _lv[0]
           and not any("at load" in l for l in _ld.get("plugin_log_previous_boot", [])),
           f"a first line that scrolled out of the tail is still in the report "
           f"({'journalctl --grep' if _gw else 'without --grep'}: {_lv})")
    ok("modprobe_conf_before_this_boot" in _ld and "modprobe_conf_modified" in _ld
       and "modprobe_conf_before_boot" not in _ld,
       "the modprobe file's check is named for this boot, with when the file was written")
    ok("probe" in _ld.get("driver_params_note", "") and "disable_lps_deep" in _ld.get("driver_params_note", ""),
       "and the driver parameters say when the driver reads them")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D3 a call trace with no end marker does not swallow the rest of the boot")
try:
    def _d3_klog(msgs):
        return "".join(f"[{100 + i:>5}.000000] kernel: {t}\n" for i, t in enumerate(msgs))
    _rounds = []
    for _i in range(10):
        _rounds += [f"PM: suspend entry (s2idle) round{_i}",
                    f"rtw88_8822ce 0000:01:00.0: failed to send h2c command {_i}",
                    f"PM: suspend exit round{_i}"]
    _hung = ["rtw88_8822ce 0000:01:00.0: failed to read ASPM, ret=-5",
             "INFO: task kworker/u16:3:123 blocked for more than 122 seconds.",
             "task:kworker/u16:3 state:D stack:0 pid:123 tgid:123 ppid:2 flags:0x00004000",
             "Workqueue: events_freezable rtw_ips_work [rtw88_core]",
             "Call Trace:", " <TASK>"] + [f" rtw_frame_{k}+0x10/0x20 [rtw88_core]" for k in "abcdefgh"]
    for _end, _label in (([" </TASK>"], "</TASK>"), ([], "no marker at all")):
        _kl = m.Plugin._diag_kernel_lines(_d3_klog(_hung + _end + _rounds), m.Plugin._DIAG_KERNEL_TERMS)
        _exits = [l for l in _kl if "PM: suspend exit" in l]
        ok(len(_exits) == 10 and sum("failed to send h2c" in l for l in _kl) == 10,
           f"every suspend round after a hung-task trace ending in {_label} is kept ({len(_exits)} of 10)")
    _kl2 = m.Plugin._diag_kernel_lines(_d3_klog(["Call Trace:"] + [f" rtw_frame_{w}+0x1/0x2 [rtw88_core]" for w in _words[:30]]
                                                + ["rtw88_8822ce: firmware failed to leave lps state"]),
                                       m.Plugin._DIAG_KERNEL_TERMS)
    ok(sum("rtw_frame_" in l for l in _kl2) == m.Plugin._DIAG_TRACE_FRAMES and "leave lps" in _kl2[-1],
       "while a long trace is still cut to its first frames and the line after it is kept")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D4 a GitHub rate limit does not put the public IP in the plugin log")
try:
    _d4 = []
    m.decky.logger.error = staticmethod(lambda msg: _d4.append(str(msg)))
    m.decky.logger.info = staticmethod(lambda msg: _d4.append(str(msg)))
    try:
        class RateLimited(m.Plugin):
            async def _github_api(self, url):
                return _ok(json.dumps({"message": "API rate limit exceeded for 203.0.113.7. (But here's "
                                       "the good news: Authenticated requests get a higher rate limit.)",
                                       "documentation_url": "https://docs.github.com/rest"}))
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _r4 = asyncio.run(RateLimited().check_for_update())
        class OtherRefusal(m.Plugin):
            async def _github_api(self, url):
                return _ok(json.dumps({"message": "Blocked for 2001:db8::7 and 198.51.100.4"}))
        asyncio.run(OtherRefusal().check_for_update())
    finally:
        m.decky.logger.error, m.decky.logger.info = _d_real_err, _d_real_info
    ok(_r4.get("success") is False and _d4 and not any("203.0.113" in l for l in _d4)
       and any("rate limit" in l for l in _d4),
       f"the log says it was a rate limit and nothing more ({[l for l in _d4 if 'Update check' in l]})")
    ok(not any("2001:db8" in l or "198.51.100" in l for l in _d4),
       "and any other refusal is logged with its addresses taken out")
    ok(m._mask_addresses("for 203.0.113.7. and fe80::1c2b:3cff:fe4d:5e6f and ::1") ==
       "for <ip>. and <ip> and <ip>", "every log line has IPv4 and IPv6 addresses masked")
    ok(m._mask_addresses("v0.24.5 at 12:34:56, kernel 6.11.11, 1.2.3.4.5") ==
       "v0.24.5 at 12:34:56, kernel 6.11.11, 1.2.3.4.5",
       "while versions, times and longer dotted numbers are left alone")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    m.decky.logger.error, m.decky.logger.info = _d_real_err, _d_real_info

section("D5 network names with a quote in them do not leak from the network log")
try:
    _apos = ("[ 9.000000] NetworkManager[700]: <info>  policy: set 'Jason's Home' (wlan0) as default for IPv4 routing and DNS\n"
             "[ 9.100000] wpa_supplicant[650]: wlan0: SME: Trying to authenticate with 62:e9:31:0d:1f:e7 "
             "(SSID='Jason's Home' freq=5180 MHz)\n"
             "[ 9.200000] NetworkManager[700]: <info>  device (wlan0): Activation: starting connection "
             "'O'Brien's WiFi' (1b2c)\n"
             "[ 9.300000] wpa_supplicant[650]: wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"O'Brien's WiFi\" auth_failures=1\n"
             "[ 9.400000] NetworkManager[700]: <info>  device (wlan0): state change: config -> failed "
             "(reason 'no-secrets', sys-iface-state: 'managed')\n")
    for _names, _label in (((), "from the quotes alone"), (("Jason's Home", "O'Brien's WiFi"), "and with the saved names")):
        _sl = " ".join(m.Plugin._diag_service_lines(_apos, 40, _names))
        ok("Home" not in _sl and "Brien" not in _sl and "Jason" not in _sl,
           f"a name with an apostrophe goes whole, {_label} ({_sl[:160]!r})")
        ok("(reason 'no-secrets', sys-iface-state: 'managed')" in _sl,
           f"while NetworkManager's own reason stays readable, {_label}")
    _sl2 = " ".join(m.Plugin._diag_service_lines(
        "[1.0] NetworkManager[1]: <info> (reason 'Jason Home', managed-type: 'full')\n", 40))
    ok("Jason" not in _sl2 and "'full'" in _sl2, "a name sitting where a reason goes is still taken out")
    _boot = ([f"[ {i}.0] NetworkManager[700]: <info>  dhcp4 (wlan0): routine renewal note {i}" for i in range(60)]
             + ["[ 70.0] NetworkManager[700]: <info>  device (wlan0): state change: activated -> failed "
                "(reason 'supplicant-timeout', managed-type: 'full')",
                "[ 71.0] wpa_supplicant[650]: wlan0: CTRL-EVENT-DISCONNECTED bssid=62:e9:31:0d:1f:e7 reason=4"]
             + [f"[ {80 + i}.0] NetworkManager[700]: <info>  dhcp4 (wlan0): routine renewal note {80 + i}" for i in range(20)]
             + ["[ 900.0] systemd[1]: Stopping Network Manager...",
                "[ 900.1] NetworkManager[700]: <info>  caught SIGTERM, shutting down normally."]
             + [f"[ 900.{2 + i}] NetworkManager[700]: <info>  device (p2p-dev-wlan0{i}): state change: disconnected -> unmanaged"
                for i in range(50)])
    _picked = m.Plugin._diag_service_lines("\n".join(_boot) + "\n", 40, (), events_first=True)
    _pj = " ".join(_picked)
    ok("supplicant-timeout" in _pj and "CTRL-EVENT-DISCONNECTED" in _pj and "caught SIGTERM" not in _pj
       and "p2p-dev" not in _pj and len(_picked) <= 42,
       f"a boot that ended in a reboot shows the failure, not the shutdown ({len(_picked)} lines)")
    _mid = (["[1.0] systemd[1]: Stopping Network Manager...", "[1.1] NetworkManager[7]: <info> caught SIGTERM, shutting down normally.",
             "[2.0] NetworkManager[8]: <info>  NetworkManager (version 1.46.0) is starting...",
             "[3.0] NetworkManager[8]: <info>  device (wlan0): state change: config -> failed (reason 'no-secrets', managed-type: 'full')"])
    _mp = " ".join(m.Plugin._diag_service_lines("\n".join(_mid) + "\n", 40, (), events_first=True))
    ok("no-secrets" in _mp and "shutdown left out" not in _mp,
       "and a restart in the middle of the boot (a backend switch) is not taken for its end")
    class NamesDiag(m.Plugin):
        def _get_wifi_interface(self): return "wlan0"
        async def get_device_info(self): return {"success": True, "driver": "rtw88"}
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[-1:] == ["dev"]: return _ok("phy#0\n\tInterface wlan0\n\t\tssid Al's Cafe\n")
            if cmd[0] == "/usr/bin/nmcli" and "NAME" in cmd: return _ok("Jason's Home\nwork'n'play\n")
            if cmd[0] == "/usr/bin/journalctl" and "-u" in cmd:
                return _ok("[1.0] NetworkManager[7]: <info> policy: set Jason's Home (wlan0) as default\n"
                           "[1.1] iwd[6]: connected to Al's Cafe now\n"
                           "[1.2] NetworkManager[7]: <info> joining work'n'play.\n")
            return _ok()
    _nj = json.dumps(asyncio.run(NamesDiag().get_diagnostic_info()).get("network_log_previous_boot", []))
    ok("Jason" not in _nj and "Cafe" not in _nj and "play" not in _nj,
       f"names the device knows are taken out even where nothing quotes them ({_nj[:200]})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D6 the panel is told why WiFi is not connected")
try:
    class NotConnected(m.Plugin):
        def __init__(self, iface="wlan0", radio="enabled", nm="disconnected", stuck=None):
            super().__init__(); self.iface = iface; self.radio = radio; self.nm = nm
            if stuck is not None:
                self._stuck_tools = lambda: set(stuck)
        def _get_wifi_interface(self): return self.iface
        def _get_active_connection_uuid(self): return None
        def _nm_general_state(self): return ("disconnected", self.radio)
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/nmcli" and "DEVICE,TYPE,STATE" in cmd:
                return _ok(f"{self.iface or 'eth0'}:{'wifi' if self.iface else 'ethernet'}:{self.nm}\n"
                           "p2p-dev-wlan0:wifi-p2p:disconnected\n")
            return _ok()
    m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "distro_id": "steamos"})
    def _ws(p): return asyncio.run(p.get_status()).get("live", {}).get("wifi_state")
    # Pass 4 C1: no_device and unavailable only once they have held 20 s.
    def _ws_held(p):
        _ws(p)
        p._wifi_state_seen = (p._wifi_state_seen[0], p._wifi_state_seen[1] - 25)
        return _ws(p)
    ok(_ws_held(NotConnected(iface=None)) == "no_device", "no wireless interface reads no_device")
    ok(_ws(NotConnected(radio="disabled")) == "radio_off", "WiFi turned off reads radio_off")
    ok(_ws_held(NotConnected(nm="unavailable")) == "unavailable", "a card NetworkManager calls unavailable reads unavailable")
    ok(_ws(NotConnected(stuck={"iw"})) == "not_responding",
       "an iw stuck in the driver reads not_responding (contract C2, _stuck_tools)")
    ok(_ws(NotConnected(stuck={"tc"})) == "disconnected" and _ws(NotConnected()) == "disconnected",
       "and anything else, including a plugin without _stuck_tools yet, reads disconnected")
    _rfk = m.Plugin.RFKILL_DIR
    try:
        m.Plugin.RFKILL_DIR = tempfile.mkdtemp()
        os.makedirs(os.path.join(m.Plugin.RFKILL_DIR, "rfkill1"))
        for _n, _v in (("type", "wlan"), ("soft", "0"), ("hard", "1")):
            open(os.path.join(m.Plugin.RFKILL_DIR, "rfkill1", _n), "w").write(_v + "\n")
        ok(_ws(NotConnected()) == "radio_off", "and a WiFi radio switch blocked in hardware reads radio_off")
    finally:
        m.Plugin.RFKILL_DIR = _rfk
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D7 on other systems the backend in use is the one running")
try:
    class Running(m.Plugin):
        def __init__(self, active=("iwd",)): super().__init__(); self.active = active
        def _get_current_backend(self): return "wpa_supplicant"
        def _get_backend_method_cached(self): return True
        def _run_cmd(self, cmd, timeout=5):
            if cmd[:2] == ["/usr/bin/systemctl", "is-active"]:
                return _ok("active") if cmd[2] in self.active else \
                    {"success": False, "stdout": "inactive", "stderr": "", "returncode": 3}
            return _ok()
    m._save_settings({**m.DEFAULT_SETTINGS, "distro_id": "bazzite"})
    _st7 = {"live": {}}; Running()._collect_backend_state(_st7)
    ok(Running()._backend_is_iwd() is True and _st7["live"].get("wifi_backend") == "iwd"
       and _st7["live"].get("wifi_backend_next") == "wpa_supplicant",
       f"config says wpa_supplicant while iwd still runs: iwd is in use, wpa_supplicant next ({_st7['live']})")
    _st7b = {"live": {}}; Running(active=("iwd", "wpa_supplicant"))._collect_backend_state(_st7b)
    ok(_st7b["live"].get("wifi_backend") == "wpa_supplicant" and "wifi_backend_next" not in _st7b["live"],
       "with both running there is no telling, so the configured one stands")
    m._save_settings({**m.DEFAULT_SETTINGS, "distro_id": "steamos"})
    _st7c = {"live": {}}; Running()._collect_backend_state(_st7c)
    ok(_st7c["live"].get("wifi_backend") == "wpa_supplicant" and "wifi_backend_next" not in _st7c["live"]
       and Running()._backend_is_iwd() is False,
       "and SteamOS answers from its configuration as before")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D8 driver parameters only for the card they belong to, and only while associated")
try:
    _r8 = tempfile.mkdtemp()
    _d_netdev(_r8, "wlan0", "mt7921u")
    _d_netdev(_r8, "wlan1", "rtw88_8822ce", operstate="down")
    _guard = _d_block("iface_module() {", "# The first run of a boot")
    def _card(iface, driver):
        out, _ = _d_run(_guard, [f'SYS_NET="{_r8}/net"', f"IFACE={iface}", f"DRIVER={driver}"], {}, _r8)
        return re.search(r"OUR_CARD=(\d)", out).group(1)
    ok(_card("wlan1", "rtw88") == "1" and _card("wlan0", "rtw88") == "0"
       and _card("wlan0", "unknown") == "1",
       "the dispatcher tells the radio bound to the settings' driver from a second one")
    os.makedirs(os.path.join(_r8, "module", "rtw88_core", "parameters"))
    os.makedirs(os.path.join(_r8, "module", "rtw88_pci", "parameters"))
    for _pp in ("rtw88_core/parameters/disable_lps_deep", "rtw88_pci/parameters/disable_aspm"):
        open(os.path.join(_r8, "module", _pp), "w").write("N\n")
    _ps8 = _d_block("# Apply power save", "# Apply buffer tuning")
    # (pass 4 D4: the parameter list is worked out before the streaming
    # exit now, so the run includes that part of the script.)
    _ps8 = _d_block("# Driver-specific sysfs fixes", "# The first run of a boot") + _ps8
    _env8 = [f'SYS_NET="{_r8}/net"', "IFACE=wlan1", "DRIVER=rtw88", "POWER_SAVE=True",
             "OUR_CARD=1", "FIRST_RUN=0", "NETLINK_STUCK=0", "LOG_TAG=t", "APPLIED=0"]
    _out8, _log8 = _d_run(_ps8, _env8, {"iw": 0, "logger": 0}, _r8)
    _aspm = open(os.path.join(_r8, "module", "rtw88_pci/parameters/disable_aspm")).read().strip()
    ok(_aspm == "N" and any("not associated" in l for l in _log8),
       f"with the radio not associated the driver parameters are left for the next connect ({_aspm})")
    open(os.path.join(_r8, "net", "wlan1", "operstate"), "w").write("up\n")
    _d_run(_ps8, _env8, {"iw": 0, "logger": 0}, _r8)
    ok(open(os.path.join(_r8, "module", "rtw88_pci/parameters/disable_aspm")).read().strip() == "Y",
       "and written once it is")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D8 the plugin's association check reads the radio the driver fixes are for")
try:
    _r8 = tempfile.mkdtemp()
    _d_netdev(_r8, "wlan0", "mt7921u")
    _d_netdev(_r8, "wlan1", "rtw88_8822ce", operstate="down")
    class TwoRadios(m.Plugin):
        def _run_cmd(self, cmd, timeout=5):
            return _ok("wlan0:wifi\nwlan1:wifi\n") if cmd[0] == "/usr/bin/nmcli" else _ok()
    _sn8 = m.SYSFS_NET
    try:
        m.SYSFS_NET = os.path.join(_r8, "net")
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88"})
        open(os.path.join(_r8, "net", "wlan1", "operstate"), "w").write("dormant\n")
        ok(TwoRadios()._wifi_associated() is False,
           "the plugin's association check reads the netdev bound to the driver, not the first one listed")
        open(os.path.join(_r8, "net", "wlan1", "operstate"), "w").write("up\n")
        ok(TwoRadios()._wifi_associated() is True, "and says associated when that one is")
        TwoRadios()._apply_pcie_aspm_fix(True)
        _l8 = lambda n, f: open(os.path.join(_r8, "devices", n, *f.split("/"))).read().strip()
        ok(_l8("wlan1", "link/l1_aspm") == "0" and _l8("wlan1", "power/control") == "on"
           and _l8("wlan0", "link/l1_aspm") == "1" and _l8("wlan0", "power/control") == "auto",
           "and the PCIe fix goes to that card too, not to the other radio")
        if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)
    finally:
        m.SYSFS_NET = _sn8
        m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D9 an upgrade mid-boot does not record our own values as the system's")
try:
    _first = _d_block("FIRST_RUN=0", "# Apply power save")
    def _fr(files):
        _r9 = tempfile.mkdtemp(); os.makedirs(os.path.join(_r9, "run"))
        for f in files: open(os.path.join(_r9, "run", f), "w").write("1\n")
        out, _ = _d_run(_first, [], {}, _r9)
        return re.search(r"FIRST_RUN=(\d)", out).group(1)
    ok(_fr([]) == "1" and _fr(["last_enforced"]) == "0" and _fr(["pcie-baseline"]) == "0",
       "the first run of a boot is one with neither a baseline nor a last_enforced")
    _ps9 = _d_block("# Apply power save", "# Apply buffer tuning")
    ok('if [ "$FIRST_RUN" = 1 ] && [ "$OUR_CARD" = 1 ]; then\n        python3 -I - "$WIFI_PCI"' in _ps9,
       "and only then is the baseline recorded")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D10 the dispatcher stamps last_enforced only for what really took")
try:
    _r10 = tempfile.mkdtemp()
    _d_netdev(_r10, "wlan0", "ath11k_pci", link=(), control="dir")
    os.makedirs(os.path.join(_r10, "devices", "wlan0", "link", "l1_aspm"))
    _ps10 = _d_block("# Apply power save", "# Apply buffer tuning")
    _env10 = [f'SYS_NET="{_r10}/net"', "IFACE=wlan0", "DRIVER=ath11k_pci", "POWER_SAVE=True",
              "OUR_CARD=1", "FIRST_RUN=0", "NETLINK_STUCK=0", "LOG_TAG=t", "APPLIED=0"]
    _o10, _l10 = _d_run(_ps10, _env10, {"iw": 1, "logger": 0}, _r10)
    ok("APPLIED=0" in _o10 and any("nothing took" in l for l in _l10),
       f"nothing taking (iw refused, every write refused) is not reported as applied ({_o10.strip()})")
    os.rmdir(os.path.join(_r10, "devices", "wlan0", "power", "control"))
    open(os.path.join(_r10, "devices", "wlan0", "power", "control"), "w").write("auto\n")
    _o10, _l10 = _d_run(_ps10, _env10, {"iw": 1, "logger": 0}, _r10)
    ok("APPLIED=1" in _o10 and any("2 writes refused" in l for l in _l10),
       f"one that took is, with the refused ones counted ({[l for l in _l10 if 'power_save' in l]})")
    _buf10 = _d_block("# Apply buffer tuning", "# Apply CAKE")
    _o10b, _l10b = _d_run(_buf10, ["IFACE=wlan0", "BUFFER_TUNING=True", "NETLINK_STUCK=0",
                                   "LOG_TAG=t", "APPLIED=0"], {"sysctl": 1, "ip": 0, "logger": 0}, _r10)
    ok("APPLIED=0" in _o10b and any("nothing took" in l for l in _l10b),
       "and buffer tuning that every sysctl refused is not either")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D11 the PCIe baseline is never missing while the plugin rewrites it")
try:
    _gaps = []
    _real_unlink, _real_remove = os.unlink, os.remove
    def _watch_unlink(path, *a, **k):
        _real_unlink(path, *a, **k); _gaps.append(os.path.exists(m.PCIE_BASELINE_FILE))
    def _watch_remove(path, *a, **k):
        _real_remove(path, *a, **k); _gaps.append(os.path.exists(m.PCIE_BASELINE_FILE))
    open(m.PCIE_BASELINE_FILE, "w").write(json.dumps({"/sys/x/link/l1_aspm": "1"}))
    os.unlink, os.remove = _watch_unlink, _watch_remove
    try:
        for _k in range(3):
            m._save_pcie_baseline({"/sys/x/link/l1_aspm": "1", f"/sys/x/link/l1_{_k}_pcipm": "1"})
    finally:
        os.unlink, os.remove = _real_unlink, _real_remove
    _b11 = m._read_pcie_baseline()
    ok(all(_gaps) and "/sys/x/link/l1_2_pcipm" in _b11,
       f"saving never removes the baseline before the new one is in place ({_gaps})")
    open(m.PCIE_BASELINE_FILE, "w").write(json.dumps({"/sys/x/power/control": "auto"}))
    m._save_pcie_baseline({"/sys/x/link/l1_aspm": "1"})
    ok(m._read_pcie_baseline() == {"/sys/x/power/control": "auto", "/sys/x/link/l1_aspm": "1"},
       "and what the dispatcher recorded meanwhile is kept")
    os.remove(m.PCIE_BASELINE_FILE)
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    os.unlink, os.remove = _real_unlink, _real_remove

section("D12 the region notice names the Steam Deck only on one")
try:
    for _fam, _want in (("rog_ally", False), ("legion_go", False), ("deck_oled", True), ("deck_lcd", True)):
        m._save_settings({**base_ap, "device_family": _fam})
        _msg = asyncio.run(Reg(SELF_MANAGED).set_regdomain("DE")).get("message", "")
        ok(bool(_msg) and ("Steam Deck" in _msg) is _want,
           f"on {_fam} the notice {'names' if _want else 'does not name'} the Steam Deck")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D13 an address clash from before the device took its address again is not shown")
try:
    class ClashLog(m.Plugin):
        """journalctl that honours --since, over clashes logged at given times."""
        def __init__(self, events):
            super().__init__(); self.events = events; self.since = []
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/journalctl":
                since = cmd[cmd.index("--since") + 1]; self.since.append(since)
                start = time.time() - 600 if since == "-10min" else float(since.lstrip("@"))
                return _ok("\n".join(line for t, line in self.events() if t >= start) + "\n")
            return _ok()
    def _clash(ip): return f"<warn>  [1.0] ipv4acd: conflict detected for IP address {ip} with host 62:E9:31:0D:1F:E7"
    _t0 = time.time()
    _boot_clash = [(_t0 - 120, _clash("192.168.1.9"))]
    cl = ClashLog(lambda: _boot_clash)
    cl._recheck_address_conflict("192.168.1.9")
    ok(cl._address_conflict and cl._address_conflict.get("ip") == "192.168.1.9",
       "a clash logged at boot, before the plugin loaded, is still reported")
    cl._recheck_address_conflict("192.168.1.20")
    cl._recheck_address_conflict("192.168.1.9")
    ok(not cl._address_conflict and cl.since[-1].startswith("@"),
       f"after the address changed and came back, the clash from before is not ({cl.since[-1]})")
    _late = _boot_clash + [(time.time() + 1, _clash("192.168.1.9"))]
    cl.events = lambda: _late
    cl._conflict_checked_at = 0.0
    cl._recheck_address_conflict("192.168.1.9")
    ok(bool(cl._address_conflict), "while a clash logged after it is")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Send report: off until there is somewhere to send it")
# Every check in these sections stubs curl, nmcli and journalctl through
# _run_cmd and nothing else: the payload, the size cap, the queue file and
# the wake rule all run as they would on a device.
try:
    _rep_dir = os.path.join(tempfile.mkdtemp(), "wifi-optimizer")
    m.REPORT_DIR = _rep_dir
    _rep_url_real = getattr(m, "REPORT_URL", None)
    _REP_URL = "https://reports.example.org/report"
    _src_main = open("main.py").read()
    ok(re.search(r'(?m)^REPORT_URL = ""$', _src_main) is not None,
       "REPORT_URL ships empty, as one line to change when the receiving end is live")

    _REP_DIAG = {
        "success": True, "plugin_version": "0.24.5-beta",
        "device_info": {"success": True, "driver": "rtw88"},
        "iw_dev": "phy#0\n\tInterface wlan0\n\t\tssid <ssid>\n",
        "kernel_log_previous_boot": ["[  900.000000] PM: suspend exit"],
        "kernel_log_this_boot": ["[    1.000000] rtw88_8822ce: note"],
    }

    class RepStub(m.Plugin):
        """curl, nmcli and journalctl answers; everything else is the plugin."""
        def __init__(self, curl=None, nm="connected:enabled", journal=None, diag=None):
            super().__init__()
            self.curl = list(curl or [])
            self.nm = nm
            self.journal = journal
            self.diag = diag if diag is not None else _REP_DIAG
            self.calls, self.bodies, self.diag_calls = [], [], 0
        async def get_diagnostic_info(self):
            self.diag_calls += 1
            return json.loads(json.dumps(self.diag))
        def _run_cmd(self, cmd, timeout=5):
            self.calls.append((list(cmd), timeout))
            if cmd[0] == "/usr/bin/curl":
                at = [a for a in cmd if a.startswith("@")]
                self.bodies.append(open(at[0][1:]).read() if at else None)
                answer = self.curl.pop(0) if self.curl else ("", 7)
                if isinstance(answer[1], int) and answer[1] != 0 and answer[0] == "":
                    return {"success": False, "stdout": "", "stderr": "curl: (7) Failed to connect",
                            "returncode": answer[1]}
                return _ok(f"{answer[0]}\n{answer[1]}")
            if cmd[0] == "/usr/bin/nmcli":
                return _ok(self.nm)
            if cmd[0] == "/usr/bin/journalctl":
                if self.journal is None:
                    return {"success": False, "stdout": "", "returncode": 1,
                            "stderr": "Data from the specified boot (-1) is not available"}
                return _ok(self.journal)
            return _ok()
        def curl_calls(self): return [c for c, _t in self.calls if c[0] == "/usr/bin/curl"]

    def _rep_reset():
        import shutil
        shutil.rmtree(_rep_dir, ignore_errors=True)

    def _pending_path(): return os.path.join(_rep_dir, getattr(m, "REPORT_PENDING_NAME", "pending-report.json"))

    m.REPORT_URL = ""
    _rep_reset()
    _p = RepStub(journal="")
    _st = _maybe_run(getattr(_p, "get_report_state", lambda: {})())
    ok(_st.get("enabled") is False and _st.get("wake_failure") is False and not _p.calls,
       f"with no URL the panel is told the feature is off, and no log is read ({_st})")
    _pv = _maybe_run(getattr(_p, "preview_report", lambda: {})())
    ok(_pv.get("success") is False and _pv.get("error") == "disabled" and _p.diag_calls == 0,
       "the preview is refused without collecting anything")
    _p._report_preview = ("x", "{}")
    _sr = _maybe_run(getattr(_p, "send_report", lambda i: {})("x"))
    ok(_sr.get("success") is False and _sr.get("error") == "disabled" and not _p.curl_calls(),
       "and a send is refused without running curl, even with a report in hand")
    os.makedirs(_rep_dir, mode=0o700, exist_ok=True)
    open(_pending_path(), "w").write('{"plugin_version":"0.24.5-beta","report":{}}')
    _maybe_run(getattr(_p, "_send_pending_report_if_due", lambda: None)())
    ok(hasattr(_p, "_send_pending_report_if_due") and not _p.calls and os.path.exists(_pending_path()),
       "a waiting report is not sent while the URL is empty, and costs no command")
    _rep_reset()
    for _bad in ("http://reports.example.org/report", "https://reports.example.org/report?x=1",
                 "https://reports.example.org/re port", "https://reports.example.org/report\n",
                 "https://user@reports.example.org/report", "ftp://reports.example.org/", " "):
        m.REPORT_URL = _bad
        ok(getattr(m, "_report_url", lambda: "x")() == "",
           f"a URL that is not plain https is treated as none ({_bad!r})")
    m.REPORT_URL = _REP_URL
    ok(getattr(m, "_report_url", lambda: "")() == _REP_URL, "a plain https URL turns it on")

    _tsx_rep = open("src/components/SendReport.tsx").read() if os.path.exists("src/components/SendReport.tsx") else ""
    ok("if (!state?.enabled) return null;" in _tsx_rep
       and "if (!state?.enabled || (!state.wake_failure && !fromHere)) return null;" in _tsx_rep,
       "the panel renders neither the button nor the wake notice while the backend says off")
    ok("<SendReportRow />" in open("src/components/PanelFooter.tsx").read()
       and "onClick={handleCopyDiagnostics}" in open("src/components/PanelFooter.tsx").read()
       and "<WakeFailureNotice />" in open("src/index.tsx").read(),
       "the button sits beside Copy diagnostics, which stays, and the notice is in the panel")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D14 error text in the log and diagnostics does not carry the home folder")
try:
    _home_was = getattr(m.decky, "DECKY_USER_HOME", None)
    try:
        m.decky.DECKY_USER_HOME = "/home/jason"
        ok(m._mask_addresses("_uninstall could not remove /home/jason/homebrew/settings/WiFi Optimizer/x.json: [Errno 13]")
           == "_uninstall could not remove ~/homebrew/settings/WiFi Optimizer/x.json: [Errno 13]",
           "a logged path under the home folder reads ~/")
        ok("jason" not in m._mask_addresses("Not following the link 'settings' on the way to /var/home/jason/homebrew"),
           "Bazzite's /var/home too")
        m.decky.DECKY_USER_HOME = "/data/people/jason"
        ok(m._mask_addresses("Failed to install dispatcher: [Errno 2] /data/people/jason/x") ==
           "Failed to install dispatcher: [Errno 2] ~/x", "and a home folder somewhere else")
        _rd = m.Plugin._redact_diagnostics({"extras_error": "[Errno 13] Permission denied: '/home/jason/homebrew/x'"})
        ok("jason" not in json.dumps(_rd) and "~/homebrew/x" in _rd["extras_error"],
           "the diagnostics' error text is masked the same way")
    finally:
        m.decky.DECKY_USER_HOME = _home_was
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D15 an address at the end of a sentence is still redacted")
try:
    ok(m.Plugin._redact_diagnostics("x 10.0.0.1. y") == "x <ip>. y",
       "an IPv4 address followed by a full stop is redacted")
    ok(m.Plugin._redact_diagnostics("build 1.2.3.4.5 and 10.0.0.1") == "build 1.2.3.4.5 and <ip>",
       "while five numbers in a row are not taken for one")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D16 chip labels and the 6 GHz fallback fit every card a driver serves")
try:
    _pr = m.DRIVER_PROFILES
    ok(_pr["rtw89"]["supports_6ghz"] is False and _pr["mt7921e"]["supports_6ghz"] is False
       and _pr["iwlwifi"]["supports_6ghz"] is False,
       "drivers that also run cards with no 6 GHz radio do not claim it when the card cannot be asked")
    ok("MT7922" not in _pr["mt7921e"]["chip_label"] and "MT792" in _pr["mt7921e"]["chip_label"]
       and _pr["rtw89"]["chip_label"] != "WiFi 6 (RTL8852)",
       f"and their labels name the family ({_pr['mt7921e']['chip_label']}, {_pr['rtw89']['chip_label']})")
    _dmi_was = getattr(m, "DMI_DIR", None)
    try:
        m.DMI_DIR = tempfile.mkdtemp()
        for _prod, _vend, _fam in (("AIR Plus", "AYANEO", "ayaneo"), ("G1617-01", "GPD", "gpd"),
                                   ("G1618-04", "GPD", "gpd_win"), ("Jupiter", "Valve", "deck_lcd"),
                                   ("Something", "Acme", "unknown")):
            open(os.path.join(m.DMI_DIR, "product_name"), "w").write(_prod + "\n")
            open(os.path.join(m.DMI_DIR, "sys_vendor"), "w").write(_vend + "\n")
            ok(m.Plugin()._detect_device_family()[1] == _fam,
               f"product {_prod!r} from vendor {_vend!r} is recognised as {_fam}")
    finally:
        if _dmi_was is not None: m.DMI_DIR = _dmi_was
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D17 a switch request that stops early does not bring back the last result")
try:
    class EarlyOut(m.Plugin):
        def _has_backend_tool(self): return True
        def _get_current_backend(self): return "iwd"
        def _get_backend_method(self): return "steamos"
    eo = EarlyOut(); eo._ensure_backend_switch_state()
    eo._backend_switch.update({"phase": "done", "target": "iwd",
                               "result": {"success": True, "backend": "iwd", "target": "iwd"}})
    _r17 = asyncio.run(eo.start_backend_switch("iwd"))
    ok(_r17.get("reason") == "already_set" and eo._backend_switch["in_progress"] is False
       and eo._backend_switch["result"] is None and eo._backend_switch["phase"] == "done",
       f"the reservation is given back without the previous switch's result ({eo._backend_switch})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D18 the dispatcher does not start iw, ip or tc behind a stuck one")
try:
    _r18 = tempfile.mkdtemp(); _d_netdev(_r18, "wlan0", "ath11k_pci")
    _ps18 = _d_block("# Apply power save", "# Apply buffer tuning")
    _o18, _l18 = _d_run(_ps18, [f'SYS_NET="{_r18}/net"', "IFACE=wlan0", "DRIVER=ath11k_pci",
                                "POWER_SAVE=True", "OUR_CARD=1", "FIRST_RUN=0", "NETLINK_STUCK=1",
                                "LOG_TAG=t", "APPLIED=0"], {"iw": 0, "logger": 0}, _r18)
    ok(not any(l.startswith("iw ") for l in _l18) and "APPLIED=1" in _o18
       and open(os.path.join(_r18, "devices", "wlan0", "power", "control")).read().strip() == "on",
       "then iw is not run, while the sysfs writes still are")
    _buf18 = _d_block("# Apply buffer tuning", "# Apply CAKE")
    _o18b, _l18b = _d_run(_buf18, ["IFACE=wlan0", "BUFFER_TUNING=True", "NETLINK_STUCK=1",
                                   "LOG_TAG=t", "APPLIED=0"], {"sysctl": 0, "ip": 0, "logger": 0}, _r18)
    _cake18 = _d_block("# Apply CAKE", "# Record enforcement")
    _o18c, _l18c = _d_run(_cake18, ["IFACE=wlan0", "CAKE=True", "NETLINK_STUCK=1", "LOG_TAG=t",
                                    "APPLIED=0"], {"tc": 0, "ip": 0, "modprobe": 0, "logger": 0}, _r18)
    ok(not any(l.startswith(("ip ", "tc ")) for l in _l18b + _l18c)
       and any(l.startswith("sysctl ") for l in _l18b),
       "nor ip or tc, while the sysctls still are")
    _t18 = _d_template()
    _stuck = _t18[_t18.index("NETLINK_STUCK=0"):_t18.index("# Apply power save")]
    ok('pgrep -O 8 -f "^([^ ]*/)?iw dev $IFACE( |\\$)"' in _stuck and "NETLINK_STUCK=1" in _stuck,
       "an iw on this interface still running after seconds is what marks the netlink path stuck")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D19 copying diagnostics does not run iw on the event loop")
try:
    import threading as _thr
    _on_loop = []
    class ThreadDiag(m.Plugin):
        def _get_wifi_interface(self):
            _on_loop.append(("iface", _thr.current_thread() is _thr.main_thread())); return "wlan0"
        async def get_device_info(self): return {"success": True, "driver": "rtw88"}
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] in ("/usr/bin/iw", "/usr/bin/uname"):
                _on_loop.append((" ".join(cmd[1:3]), _thr.current_thread() is _thr.main_thread()))
            return _ok()
    asyncio.run(ThreadDiag().get_diagnostic_info())
    ok(_on_loop and not any(on for _w, on in _on_loop) and len(_on_loop) >= 4,
       f"iw dev, iw reg get, uname and the interface lookup run off the event loop ({_on_loop})")
    _det = []
    class ThreadDetect(m.Plugin):
        def _detect_device_family(self): return ("Galileo", "deck_oled", "Steam Deck OLED")
        def _detect_wifi_driver(self):
            _det.append(_thr.current_thread() is _thr.main_thread()); return "ath11k_pci"
        def _radio_has_6ghz(self):
            _det.append(_thr.current_thread() is _thr.main_thread()); return True
    _di = asyncio.run(ThreadDetect().get_device_info())
    ok(_det == [False, False] and _di.get("driver") == "ath11k_pci" and _di.get("supports_6ghz") is True,
       f"and so do the driver lookup and the 6 GHz question (iw phy) behind device info ({_det})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D20 diagnostics read Clock PM and the device's power state")
try:
    _r20 = tempfile.mkdtemp(); _dev20 = _d_netdev(_r20, "wlan0", "rtw88_8822ce")
    open(os.path.join(_dev20, "link", "clkpm"), "w").write("1\n")
    open(os.path.join(_dev20, "power_state"), "w").write("D0\n")
    _sn20 = m.SYSFS_NET
    try:
        m.SYSFS_NET = os.path.join(_r20, "net")
        class PcieDiag(m.Plugin):
            def _get_current_backend(self): return "wpa_supplicant"
            def _run_cmd(self, cmd, timeout=5): return _ok()
        _p20 = m.Plugin._diagnostic_extras(PcieDiag(), "rtw88", "wlan0").get("pcie", {})
    finally:
        m.SYSFS_NET = _sn20
    ok(_p20.get("clkpm") == "1" and _p20.get("power_state") == "D0" and _p20.get("l1_aspm") == "1",
       f"the PCIe section has clkpm and power_state beside the ASPM states ({_p20})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D21 backend and region failures say what to do next")
try:
    _rb21, _rc21 = m.BAZZITE_IWD_CONF, m.GENERIC_BACKEND_CONF
    try:
        _d21 = tempfile.mkdtemp()
        m.BAZZITE_IWD_CONF = os.path.join(_d21, "iwd.conf")
        m.GENERIC_BACKEND_CONF = os.path.join(_d21, "99-wifi-optimizer-backend.conf")
        open(m.BAZZITE_IWD_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        class Outranked21(m.Plugin):
            def __init__(self): super().__init__(); self._ensure_backend_switch_state()
            def _merged_nm_backend(self): return "iwd"
            def _run_cmd(self, cmd, timeout=5): return _ok()
        o21 = Outranked21(); asyncio.run(o21._generic_backend_switch_worker("wpa_supplicant"))
        _m21 = o21._backend_switch["result"].get("message", "")
        ok(m.BAZZITE_IWD_CONF in _m21 and "change that setting" in _m21,
           f"a switch another file outranks names that file and what to do ({_m21!r})")
        os.remove(m.BAZZITE_IWD_CONF)
        o21 = Outranked21(); asyncio.run(o21._generic_backend_switch_worker("wpa_supplicant"))
        _m21b = o21._backend_switch["result"].get("message", "")
        ok("/etc/NetworkManager/conf.d" in _m21b, f"and where to look when it is not one we know ({_m21b!r})")
    finally:
        m.BAZZITE_IWD_CONF, m.GENERIC_BACKEND_CONF = _rb21, _rc21
    m._save_settings(dict(base_ap))
    _m21c = asyncio.run(Reg(PLAIN, ok_set=False).set_regdomain("DE")).get("message", "")
    class NoBus(m.Plugin):
        def __init__(self): super().__init__(); self._ensure_backend_switch_state()
        def _session_bus_cmd(self, args): return None
    nb = NoBus(); asyncio.run(nb._steamos_manager_backend_switch_worker("iwd"))
    _m21d = nb._backend_switch["result"].get("message", "")
    ok("Try again" in _m21c and "Try again" in _m21d,
       f"a region that could not be set and an unreachable SteamOS service each give a next step ({_m21c!r}, {_m21d!r})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("J3 the dispatcher stands down while the plugin is disabled in Decky")
try:
    _tj = _d_template()
    _prog = _tj[_tj.index("<<'DISABLEDEOF'\n") + len("<<'DISABLEDEOF'\n"):]
    _prog = _prog[:_prog.index("\nDISABLEDEOF")]
    def _dis(path, name="WiFi Optimizer"):
        return _dsp.run([sys.executable, "-I", "-c", _prog, path, name], capture_output=True,
                        text=True, timeout=10).stdout.strip()
    _dj = tempfile.mkdtemp()
    _lj = os.path.join(_dj, "loader.json")
    open(_lj, "w").write(json.dumps({"disabled_plugins": ["CSS Loader", "WiFi Optimizer"]}))
    ok(_dis(_lj) == "yes", "listed in Decky's disabled_plugins reads as disabled")
    open(_lj, "w").write(json.dumps({"disabled_plugins": ["CSS Loader"], "branch": 0}))
    ok(_dis(_lj) == "no", "and not listed as not")
    _lk = os.path.join(_dj, "link.json"); os.symlink(_lj, _lk)
    open(_lj, "w").write(json.dumps({"disabled_plugins": ["WiFi Optimizer"]}))
    _ff = os.path.join(_dj, "fifo.json"); os.mkfifo(_ff)
    _bad = os.path.join(_dj, "bad.json"); open(_bad, "w").write("{not json")
    ok(_dis(_lk) == "no" and _dis(_ff) == "no" and _dis(_bad) == "no"
       and _dis(os.path.join(_dj, "missing.json")) == "no",
       "a link at the name, a FIFO, a broken file or none at all count as not disabled")
    _assign = "\n".join(l for l in _d_filled().splitlines()
                        if l.startswith(("SETTINGS_FILE=", "PLUGIN_NAME=", "LOADER_SETTINGS=")))
    _vals = _dsp.run(["/bin/bash", "-c", _assign + '\nprintf "%s|%s" "$LOADER_SETTINGS" "$PLUGIN_NAME"'],
                     capture_output=True, text=True).stdout
    ok(_vals == "/home/deck/homebrew/settings/loader.json|" + json.load(open("plugin.json"))["name"],
       f"Decky's settings are read beside the plugin's, for the name plugin.json gives ({_vals})")
    _gate = _tj.index('if [ "$(plugin_disabled)" = "yes" ]; then')
    ok(_gate < _tj.index("# Apply power save") and "exit 0" in _tj[_gate:_gate + 150]
       and _tj.index("# Self-removal") < _gate,
       "and it exits before anything is applied, after the clean-up for a removed plugin")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("U1 a region the card does not take is said, not shown as set")
try:
    m._save_settings(dict(base_ap))
    asyncio.sleep = _no_sleep
    try:
        _ru = asyncio.run(Reg(PLAIN).set_regdomain("DE"))
        class Takes(Reg):
            def _run_cmd(self, cmd, timeout=5):
                if cmd[:3] == ["/usr/bin/iw", "reg", "set"]:
                    self.reg_text = self.reg_text.replace("country 00", "country " + cmd[3])
                return super()._run_cmd(cmd, timeout)
        m._save_settings(dict(base_ap))
        _rt = asyncio.run(Takes(PLAIN).set_regdomain("DE"))
    finally:
        asyncio.sleep = _d_real_sleep
    # Pass 4 D13: cfg80211's 00 is said in words.
    ok(_ru.get("success") is True and "world default" in (_ru.get("message") or "")
       and "(00)" not in (_ru.get("message") or "") and "DE" in (_ru.get("message") or ""),
       f"a card that stays on its own region says so ({_ru.get('message')!r})")
    ok(_rt.get("success") is True and not _rt.get("message") and _rt.get("governing") == "DE",
       "and one that takes it says nothing more")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    asyncio.sleep = _d_real_sleep
m._save_settings(dict(m.DEFAULT_SETTINGS))


# ---- QA pass 3, FRONT: panel and public docs (source checks; none reads dist) ----
def _src(path):
    try:
        return open(path).read()
    except OSError:
        return ""
def _between(text, start, end):
    """text from start to the first end after it, or "" when start is missing."""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    return text[i:j if j >= 0 else len(text)]
_F_IX = _src("src/index.tsx"); _F_UW = _src("src/updateWatch.ts"); _F_TY = _src("src/types.ts")
_F_BT = _src("src/components/BackendToggleRow.tsx"); _F_SS = _src("src/components/StreamingSection.tsx")
_F_US = _src("src/components/UpdatesSection.tsx"); _F_AS = _src("src/components/ActionsSection.tsx")
_F_SG = _src("src/components/StatsGrid.tsx"); _F_TH = _src("src/theme.ts"); _F_PF = _src("src/components/PanelFooter.tsx")
_F_RD = _src("README.md"); _F_SEC = _src("SECURITY.md"); _F_MAIN = _src("main.py")
_F_ALL = "".join(_src(os.path.join(dp, f)) for dp, _dn, fs in os.walk("src") for f in fs)

section("F1 Reset re-checks for updates, and says update settings are kept")
try:
    _rr = _between(_F_IX, "const runReset = async", "\n  };")
    _rr_ok = _between(_rr, "} else {", "} catch")
    ok("updateCheckSeqRef.current += 1;" in _rr_ok and "setUpdateInfo(null);" in _rr_ok
       and "runUpdateCheck(true);" in _rr_ok, "a successful Reset drops the old offer and checks again")
    ok("Update settings are kept." in _between(_F_IX, "const RESET_CONFIRM_TEXT", ";\n"),
       "the confirmation says the update settings are kept")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F2 a refused update re-checks, so the offer matches GitHub")
try:
    _ref = _between(_between(_F_IX, "const handleApplyUpdate", "\n  };"), "if (!result.success) {", "return;")
    ok("updateCheckSeqRef.current += 1;" in _ref and "runUpdateCheck(true, true);" in _ref,
       "the refusal branch asks again, keeping the refusal's reason on screen")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F3 the Optimize Safe / Fix now / Force Reapply result expires")
try:
    ok("setOptimizeResult((prev) => (prev === shown ? null : prev))" in _F_IX
       and "MESSAGE_TTL_MS" in _between(_F_IX, "if (!optimizeResult) return;", "}, [optimizeResult]);"),
       "the result banner goes MESSAGE_TTL_MS after it was set, like the marks it points at")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F4 a lock on for another network reads as done, as Optimize Safe treats it")
try:
    _ls = _between(_F_IX, "const lockSafe =", ";\n")
    ok("!!status?.live?.bssid_lock_other_network ||" in _ls
       and "!status?.live?.bssid_lock_other_network)" not in _ls,
       "the panel's rule counts bssid_lock_other_network as satisfied")
    ok("_lock_held_elsewhere" in _between(_F_MAIN, "async def optimize_safe", "def _record_reapplied"),
       "and the backend rule it mirrors still leaves such a lock out (_lock_held_elsewhere)")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F5 while the script waits for a game, the panel says so and waits as long")
try:
    ok('"Finishing the update. Steam restarts once no game or stream is running."' in _F_IX
       and "Steam will restart" not in _F_IX, "the restarting phase says the restart waits for the game")
    _ui = re.search(r"UPDATE_TIMEOUT_MS = (\d+) \* (\d+) \* (\d+);", _F_IX)
    _mw = re.search(r"MAX_WATCH_MS = (\d+) \* (\d+) \* (\d+);", _F_UW)
    _prod = lambda mt: int(mt.group(1)) * int(mt.group(2)) * int(mt.group(3)) if mt else -1
    ok(_prod(_ui) == _prod(_mw) == 15 * 60 * 1000, "the panel's timeout is the watch's 15 minutes")
    ok("so the panel waits a little longer than that" not in _F_IX, "and the old download-only comment is gone")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F6 no panel text tells people to restart Steam")
try:
    ok(not re.search(r"[Rr]estart Steam", _F_ALL),
       "every failure says restart the device, which is known to load what is installed")
    ok("If it still doesn't finish, restart the device." in _between(_F_IX, "const UPDATE_FAILED_TEXT", ";\n"),
       "a failed update says to try again, then restart the device")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F7 backend switch failures: no raw output, one wording, failure checked first")
try:
    _bp = _between(_F_IX, "const beginBackendPoll", "}, BACKEND_POLL_INTERVAL);")
    ok("s.result.detail ? ` (" not in _bp and "wifi_backend: backendResultText(s.result!)" in _bp,
       "the row shows the banner's words, never the command output")
    _brt = _between(_F_BT, "export function backendResultText", "\n}")
    ok(0 <= _brt.find("if (!r.success)") < _brt.find("r.needs_reboot")
       and "return r.message ?? \"WiFi didn't come back after the backend switch. Restart the device.\";" in _brt,
       "a failure with needs_reboot is not announced as Switched to, and says what the backend found")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F8 the WPA2 and weaker-lock notices can be dismissed")
try:
    ok("let findingsDismissed" in _F_IX and "onClick={handleDismissWpa2}" in _F_IX
       and "onClick={handleDismissLockWeaker}" in _F_IX,
       "each has a Dismiss, remembered across the panel closing")
    ok("{(wpa2Shown || errors.allow_wpa2) && (" in _F_IX and "{(lockWeakerShown || errors.move_bssid_lock) && (" in _F_IX,
       "and a dismissed finding stays hidden until it clears")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F9 All good is not a button that runs Optimize Safe")
try:
    _ob = _between(_F_IX, "<ButtonItem", "onClick={handleOptimize}")
    _ob = _ob[_ob.rfind("<ButtonItem"):]
    ok("allSafeActive" in _ob and "disabled=" in _ob, "the button is disabled while everything is applied")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F10 a dead or missing card is not told to connect first")
try:
    ok("live?.wifi_state" in _F_IX and "The WiFi card isn't responding. Restart the device." in _F_IX
       and "WiFi is off. Turn it on in Steam's settings." in _F_IX,
       "the disconnected banner follows wifi_state")
    ok('?? "disconnected"' in _F_IX and "wifi_state?:" in _F_TY, "a backend without it reads as disconnected")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F11 the backend row says which backend comes at the next boot")
try:
    ok("wifi_backend_next" in _F_BT and "from the next boot." in _F_BT and "wifi_backend_next?:" in _F_TY,
       "wifi_backend_next is shown in the subtitle")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F12 Reset says what it does: no reconnect, settings from the next connect")
try:
    ok('working === "reset" ? RESET_WORKING_TEXT' in _F_IX and 'const RESET_WORKING_TEXT = "Working...";' in _F_IX,
       "Reset does not claim WiFi will reconnect")
    ok("Every setting is back to its default" not in _F_IX and 'RESET_DONE_TEXT = "Done. Settings are reset."' in _F_IX,
       "nor that every setting is at its default")
    ok("from the next time WiFi connects" in _between(_F_IX, "const RESET_CONFIRM_TEXT", ";\n"),
       "and the confirmation says when the network settings come off")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F13 the region's Not managed entry says what choosing it does")
try:
    ok('"Not managed (put back the region from before)"' in _F_IX and "regdomain.setting\n" in _F_IX,
       "once the plugin set a region, it is not labelled with that region")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F14 Keep fixes on lasts until the device restarts")
try:
    ok("restart." not in _F_SS.replace("restart the device.", "") and _F_SS.count("restart the device") == 2
       and "until you restart the device" in _F_RD, "the panel and README say the device")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F15 the streaming idle text names only what is on, by the panel's names")
try:
    ok("Power save, buffer tuning and CAKE are paused" not in _F_SS
       and all(k in _F_SS for k in ("settings?.power_save_disabled", "settings?.buffer_tuning_enabled",
                                     "settings?.cake_enabled", '"Prevent lag spikes"')),
       "built from the enabled fixes")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F16 update errors: channel and notice errors apart, and every one expires")
try:
    _nc = _between(_F_IX, "const handleNotifyChange", "\n  };") + _between(_F_IX, "const handleChannelChange", "\n  };")
    ok("setUpdateError(result.message" not in _nc and "setUpdateSettingError(" in _nc
       and "settingError={updateSettingError}" in _F_IX and "{settingError}" in _F_US,
       "a failed channel or notice change is shown in the Updates section, not under Update")
    ok("setUpdateError((prev) => (prev === shown ? null : prev))" in _F_IX,
       "and an update error expires")
    ok("if (info?.success && !keepError) setUpdateError(null);" in _F_IX,
       "and goes on the next successful check")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F17 the process names field can be cancelled")
try:
    _cp = _between(_F_SS, "const cancelPatterns", "};")
    ok("setPatternsDirty(false)" in _cp and "onClick={cancelPatterns}" in _F_SS,
       "Cancel drops the draft and follows the saved value again")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F18 a paused result is not All applied")
try:
    _ob = _between(_F_IX, "{optimizeResult && (() => {", "})()}")
    ok("v.paused" in _ob and "`Saved. ${pausedText}`" in _ob and "paused?: boolean;" in _F_TY,
       "fixes saved for the next stream say so")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F19 the stranded-release banner is true for the band")
try:
    ok("The band\n          preference comes back after about 10 minutes" in _F_IX
       and "applies again to whichever access point works" not in _F_IX,
       "the band waits about 10 minutes and needs the band in range")
    ok(getattr(m.Plugin, "_RELEASE_NOTICE_SECONDS", None) == 600, "which is the backend's own wait")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F20 README and the first-run banner: two fixes are on from install")
try:
    ok("already on from the moment the plugin is installed" in _F_RD,
       "the README says Prevent lag spikes and Auto-fix are on from install")
    ok("alreadyOn" in _F_IX and "to get started." not in _F_IX, "the first-run banner says which are on already")
    ok(m.DEFAULT_SETTINGS.get("power_save_disabled") is True and m.DEFAULT_SETTINGS.get("auto_fix_on_wake") is True,
       "which is what the defaults do")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F21 the README's Reset paragraph is true")
try:
    _rp = _between(_F_RD, "**Before uninstalling:**", "\n\n")
    ok("deletes the plugin's own config files" not in _rp and "until WiFi next connects" in _rp
       and "Allow WPA2" in _rp and "auto-fix script" in _rp,
       "no deleted config claim, the next-connect timing, Allow WPA2 and the auto-fix script")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F22 the README's manual uninstall works and says how to undo network changes")
try:
    _un = _between(_F_RD, "## Uninstall", "## Building from source")
    ok(0 <= _un.find("sudo systemctl restart plugin_loader") < _un.find("homebrew/settings/WiFi")
       and "homebrew/logs/WiFi" in _un, "Decky restarts before the settings and logs go, and the logs go too")
    ok("go to **Plugins**" in _un and "ipv6.method auto" in _un and "Steam's WiFi power management" in _un,
       "the Decky path, the network undo line and the power management note are there")
    ok("Disabling the plugin in Decky" in _un, "and what disabling does (J3)")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F23 the README's install and update claims hold for the versions they name")
try:
    ok("curl -fsSL" in _F_RD and "curl -sL" not in _F_RD, "the one-liner fails on an HTTP error")
    _upd = _between(_F_RD, "**Updating:**", "\n\n")
    ok("From v0.19.0-beta on" in _upd and "From v0.24.2 on" in _upd and "Keep fixes on" in _upd,
       "reload and the game refusal are version-qualified, and Keep fixes on is named")
    ok("from late 2024 or newer" in _F_RD, "the zip-over-install claim names the Decky it holds for")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F24 F25 the README's hardware and region rows")
try:
    ok("Varies (often MediaTek MT7922 or Intel)" in _F_RD and "WiFi 6 (RTL8852) or WiFi 7" not in _F_RD,
       "other handhelds are not claimed to use one chip")
    ok("the Steam Deck OLED's among them" in _F_RD and "Steam Deck cards do" not in _F_RD,
       "the region row is true for the LCD too")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F26 SECURITY.md: who owns the plugin folder")
try:
    ok("3.1.10" in _F_SEC and "The plugin's own folder is owned by root, so only" not in _F_SEC,
       "the ownership claim names the installs it holds for")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F27 calls that hold a control are bounded")
try:
    ok("withTimeout(backend.startBackendSwitch(target))" in _F_IX, "starting a switch")
    _bp = _between(_F_IX, "const beginBackendPoll", "}, BACKEND_POLL_INTERVAL);")
    ok(0 <= _bp.find("pollStartedAt > BACKEND_SWITCH_MAX_S") < _bp.find("if (inFlight) return;")
       and "withTimeout(backend.getBackendSwitchStatus()" in _bp,
       "the switch poll gives up on its own time even when a call hangs")
    _pl = _between(_F_UW, "async function poll()", "\n}")
    ok(0 <= _pl.find("MAX_WATCH_MS") < _pl.find("if (inFlight) return;"),
       "the update watch checks its age before the in-flight guard")
    ok("withTimeout(backend.checkForUpdate())" in _F_IX and "withTimeout(backend.getDiagnosticInfo())" in _F_PF,
       "and the update check and diagnostics, which hold their buttons too")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F28 the update watch is not ended by a sleep")
try:
    _pl = _between(_F_UW, "async function poll()", "\n}")
    ok("startedAt" not in _F_UW and "MAX_TICK_MS" in _pl and "idleMs = 0;" in _pl
       and "progress.phase !== lastPhase" in _pl,
       "it stops after MAX_WATCH_MS awake with no new phase, not 15 minutes of wall clock")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("F29 F30 F31 F32 F33 small panel text")
try:
    ok('band_preference: "Band preference"' in _F_IX, "F29 the drift list reads naturally")
    ok("README mockup" not in _F_TH, "F30 no comment about a mockup that does not exist")
    ok("parseInt(live.frequency" in _F_SG and "const freq = connected ? live.frequency" not in _F_SG,
       "F31 the frequency is shown as a number, once with MHz")
    ok(all(t in _F_AS + _F_US + _F_IX for t in ("Force reapply all", "Reset settings", '"Check for updates"',
                                                '"Update now"', 'label="DNS provider"'))
       and not any(t in _F_ALL for t in ("Force Reapply All", "Reset Settings", "Check for Updates\"",
                                         "\"Update Now\"", "DNS Provider")),
       "F32 buttons in sentence case")
    ok("The other WiFi backend" not in _F_BT, "F32 the backend subtitle reads right with wpa_supplicant in use")
    ok("Turn your settings back on, or tap Optimize Safe." in _F_TY, "F33 the recovered notice says what to do")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("J1 J2 FRONT: Allow WPA2 and custom DNS say what they do")
try:
    ok("Access points that offer WPA3 keep using it." not in _F_IX
       and "Reset settings puts it back to WPA3 only." in _F_IX
       and "| Allow WPA2 here |" in _F_RD, "the WPA2 banner and README are true before and after NetworkManager 1.58.1")
    ok("no IPv4 at all" in _between(_F_IX, 'label="Custom DNS"', "checked="), "the DNS explanation warns about an IPv6-only network")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

# ---- QA pass 3 merge: where two owners' work meets ----
section("merge: last_enforced and the streaming flag are never missing while the plugin rewrites them")
# The dispatcher records the system's PCIe values only while last_enforced is
# absent (D9), and in auto mode leaves the fixes off while the streaming flag
# is absent. Removing either before writing it again gave a dispatcher run in
# between our values as the system's, or no stream mid-stream.
try:
    _mg_dir = tempfile.mkdtemp()
    _mg_saved = (m.ENFORCED_FILE, m.STREAM_FLAG_FILE)
    m.ENFORCED_FILE = os.path.join(_mg_dir, "last_enforced")
    m.STREAM_FLAG_FILE = os.path.join(_mg_dir, "streaming")
    _mg_gaps = []
    _mg_unlink, _mg_remove = os.unlink, os.remove
    def _mg_watch(real):
        def watch(path, *a, **k):
            real(path, *a, **k)
            _mg_gaps.append((os.path.exists(m.ENFORCED_FILE), os.path.exists(m.STREAM_FLAG_FILE)))
        return watch
    for _f in (m.ENFORCED_FILE, m.STREAM_FLAG_FILE):
        open(_f, "w").write("1\n")
    m._save_settings({**m.DEFAULT_SETTINGS, "last_applied": 1700000000})
    _mg_p = m.Plugin()
    os.unlink, os.remove = _mg_watch(_mg_unlink), _mg_watch(_mg_remove)
    try:
        for _k in range(3):
            _mg_p._record_reapplied(1700000000)
            _mg_p._set_stream_flag(True)
    finally:
        os.unlink, os.remove = _mg_unlink, _mg_remove
    ok(all(a and b for a, b in _mg_gaps),
       f"rewriting either never leaves it missing ({_mg_gaps})")
    ok(abs(int(open(m.ENFORCED_FILE).read()) - time.time()) < 60
       and abs(int(open(m.STREAM_FLAG_FILE).read()) - time.time()) < 60,
       "and both hold the new time afterwards")
    ok(sorted(os.listdir(_mg_dir)) == ["last_enforced", "streaming"],
       f"with no temporary file left beside them ({sorted(os.listdir(_mg_dir))})")
    _mg_p._set_stream_flag(False)
    ok(not os.path.exists(m.STREAM_FLAG_FILE), "and the flag still comes off")
    m.ENFORCED_FILE, m.STREAM_FLAG_FILE = _mg_saved
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    os.unlink, os.remove = _mg_unlink, _mg_remove
    m.ENFORCED_FILE, m.STREAM_FLAG_FILE = _mg_saved
m._save_settings(dict(m.DEFAULT_SETTINGS))

section("merge: a Reset that could not undo everything is not shown as a successful one")
# LIFECYCLE's Reset answers {success: false, error: "partial", message}. The
# panel shows that message as the Reset error, and only a success says it
# worked and asks GitHub again (contract C4).
try:
    _mg_tsx = open("src/index.tsx").read()
    _mg_rr = _mg_tsx[_mg_tsx.find("const runReset = async"):]
    _mg_rr = _mg_rr[:_mg_rr.find("\n  };\n")]
    _mg_fail = _mg_rr.find("if (!isResult(result) || !result.success)")
    _mg_else = _mg_rr.find("} else {", _mg_fail)
    ok(_mg_fail != -1 and _mg_else != -1
       and "failureText(result)" in _mg_rr[_mg_fail:_mg_else]
       and "RESET_DONE_TEXT" not in _mg_rr[_mg_fail:_mg_else]
       and "runUpdateCheck" not in _mg_rr[_mg_fail:_mg_else],
       "a refused or partial Reset shows the backend's message, without the done text or the update re-check")
    ok("RESET_DONE_TEXT" in _mg_rr[_mg_else:] and "runUpdateCheck(true)" in _mg_rr[_mg_else:],
       "and only a successful one says done and checks for updates again")
    ok('"error": "partial"' in open("main.py").read(),
       "the backend half: Reset still answers partial when a step could not be undone")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("merge: advice to restart says the device, in the update script too")
try:
    _mg_main = open("main.py").read()
    ok("Restart the Deck" not in _mg_main and "Restart the device to finish." in _mg_main
       and "Restart the device to load it." in _mg_main,
       "the update script's log lines say restart the device, as the panel does, on every handheld")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Send report: exactly what was shown, and no more than the receiver takes")
try:
    _rep_reset()
    _REF = "WO-7K2M9Q"
    _p = RepStub(curl=[(json.dumps({"ok": True, "ref": _REF}), "200")])
    _pv = asyncio.run(_p.preview_report())
    _sr = asyncio.run(_p.send_report(_pv.get("id", "")))
    _sent = json.loads(_p.bodies[0]) if _p.bodies and _p.bodies[0] else None
    ok(_pv.get("success") is True and _sent == _pv.get("payload"),
       "the body sent is exactly the payload the preview showed")
    # Pass 4 C6: the report also says whether the wake notice found a failed wake.
    ok(_pv["payload"] == {"plugin_version": m.decky.DECKY_PLUGIN_VERSION,
                          "report": dict(_REP_DIAG, wake_failure=False)},
       "which is the diagnostics as Copy diagnostics gives them, plus the plugin version")
    _cc = _p.curl_calls()[0]
    ok(_cc[1] == "-q" and "--proto" in _cc and _cc[_cc.index("--proto") + 1] == "=https"
       and _cc[-1] == _REP_URL and "-L" not in _cc and "-sL" not in _cc
       and not any("plugin_version" in a or "wlan0" in a for a in _cc),
       f"curl gets https only, no redirects, no curlrc, and the body only as a file ({_cc})")
    ok("--max-time" in _cc and int(_cc[_cc.index("--max-time") + 1]) <= 30
       and _p.calls[-1][1] <= 30,
       "and it is bounded in time")
    ok(_sr.get("status") == "sent" and _sr.get("ref") == _REF
       and _sr.get("message") == f"Report sent. Reference {_REF}. Quote it if you post about this.",
       "a sent report answers with its reference, worded for the panel")
    ok(not os.path.exists(_pending_path()), "and nothing is left waiting")
    _again = asyncio.run(_p.send_report(_pv.get("id", "")))
    ok(_again.get("error") == "stale" and len(_p.curl_calls()) == 1,
       "the same report is not sent twice from one preview")
    _p2 = RepStub(curl=[("", 7)])
    asyncio.run(_p2.preview_report())
    _r2 = asyncio.run(_p2.send_report("0" * 64))
    ok(_r2.get("error") == "stale" and not _p2.curl_calls(),
       "a send that names a report the user was not shown is refused")
    # Pass 4 C6: ok:true is sent, with or without a reference this version knows.
    _bad = RepStub(curl=[("<html>portal</html>", "200"), ('{"ok":true,"ref":"WO-ILOU00"}', "200")])
    _pv = asyncio.run(_bad.preview_report())
    _rb = asyncio.run(_bad.send_report(_pv["id"]))
    ok(_rb.get("status") != "sent" and os.path.exists(_pending_path()),
       "an answer that is not the receiver's is not taken as sent; the report is kept")
    _rep_reset()

    # Larger than the receiver takes: the biggest log section goes first.
    _big = dict(_REP_DIAG)
    _big["kernel_log_previous_boot"] = ["[ 1.0] rtw88_8822ce: " + "x" * 280] * 300
    _big["kernel_log_this_boot"] = ["[ 1.0] rtw88_8822ce: " + "y" * 280] * 200
    _big["network_log_previous_boot"] = ["NetworkManager: line"] * 10
    _p = RepStub(curl=[(json.dumps({"ok": True, "ref": _REF}), "200")], diag=_big)
    _pv = asyncio.run(_p.preview_report())
    _rep = (_pv.get("payload") or {}).get("report", {})
    _ascii = len(json.dumps(_rep, separators=(",", ":")))
    _js = len(json.dumps(_rep, separators=(",", ":"), ensure_ascii=False).encode())
    ok(_pv.get("success") is True and _ascii <= getattr(m, "REPORT_MAX_BYTES", 0) <= 96 * 1024 - 8 * 1024,
       f"a report too large for the receiver is cut below its 96 KiB limit ({_ascii} bytes)")
    # Pass 4 D18: in a fixed order, the previous boot's kernel log last.
    ok("kernel_log_this_boot" not in _rep and "device_info" in _rep,
       "by leaving out log sections, keeping what fits")
    ok("kernel_log_previous_boot" in str(_rep.get("left_out", "")),
       "and the report says what was left out")
    asyncio.run(_p.send_report(_pv["id"]))
    ok(_p.bodies and len(_p.bodies[0].encode()) < 128 * 1024 and json.loads(_p.bodies[0]) == _pv["payload"],
       "what is sent is the cut report the preview showed, under the request limit")
    _wide = dict(_REP_DIAG, os_release="é" * 70000)
    _made = getattr(m, "_report_payload", lambda i, v: None)(_wide, "1.0.0")
    _wr = _made[0]["report"] if _made else {}
    ok(_made is not None and len(json.dumps(_wr, separators=(",", ":"), ensure_ascii=False).encode())
       <= 96 * 1024 and "os_release" not in _wr,
       "non-ASCII text is measured as the receiver measures it, never under")
    _rep_reset()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Send report: kept while offline, one at a time, sent when WiFi is back")
try:
    _rep_reset()
    _p = RepStub(curl=[("", 7)], nm="disconnected:enabled")
    _pv = asyncio.run(_p.preview_report())
    _sr = asyncio.run(_p.send_report(_pv["id"]))
    ok(_sr.get("status") == "queued"
       and _sr.get("message") == "No connection. It will be sent when WiFi is back.",
       "with no connection the report is kept and the panel says when it will go")
    ok(os.path.exists(_pending_path()) and open(_pending_path()).read() == _p.bodies[0],
       "the waiting report is exactly what was shown")
    ok(oct(os.stat(_pending_path()).st_mode & 0o777) == "0o600"
       and oct(os.stat(_rep_dir).st_mode & 0o777) == "0o700",
       "readable by the plugin's user alone, in a directory nobody else can open")
    ok(asyncio.run(_p.get_report_state()).get("pending") is True, "the panel is told one is waiting")

    # A second report while one waits replaces it.
    _p2 = RepStub(curl=[("", 6)], diag=dict(_REP_DIAG, iw_dev="second"))
    _pv2 = asyncio.run(_p2.preview_report())
    asyncio.run(_p2.send_report(_pv2["id"]))
    ok(sorted(os.listdir(_rep_dir)) == [m.REPORT_PENDING_NAME]
       and json.loads(open(_pending_path()).read())["report"]["iw_dev"] == "second",
       "at most one waits: a new one replaces it, and no temporary file is left")

    # Background: still offline, nothing is sent.
    _bg = RepStub(curl=[(json.dumps({"ok": True, "ref": _REF}), "200")], nm="disconnected:enabled")
    asyncio.run(_bg._send_pending_report_if_due())
    ok(not _bg.curl_calls() and os.path.exists(_pending_path()),
       "while NetworkManager says offline, the background watch does not try")
    _bg.nm = "connected:enabled"
    asyncio.run(_bg._send_pending_report_if_due())
    ok(not _bg.curl_calls(), "and it waits two minutes before looking again")
    _bg._report_retry_at = 0
    asyncio.run(_bg._send_pending_report_if_due())
    ok(len(_bg.curl_calls()) == 1 and json.loads(_bg.bodies[0])["report"]["iw_dev"] == "second",
       "once WiFi is back the waiting report is sent as it was kept")
    ok(not os.path.exists(_pending_path()), "and then it is no longer kept")
    _st = asyncio.run(RepStub().get_report_state())
    ok(_st.get("sent_ref") == _REF and _st.get("pending") is False,
       "its reference is kept for the panel, across a reload of the plugin")
    _d = asyncio.run(RepStub().dismiss_report_notice("sent"))
    ok(_d.get("success") is True and asyncio.run(RepStub().get_report_state()).get("sent_ref") == "",
       "until the panel dismisses it")
    _bg._report_retry_at = 0
    _n = len(_bg.calls)
    asyncio.run(_bg._send_pending_report_if_due())
    ok(len(_bg.calls) == _n, "with nothing waiting the watch runs no command at all")
    ok("await self._send_pending_report_if_due()" in _src_main[_src_main.index("async def _stranded_watch(self)"):
                                                             _src_main.index("async def _stranded_watch_once")],
       "the background watch that always runs is what sends it")

    # 400 and 413 are final.
    for _code, _err in (("400", "refused"), ("413", "too_large")):
        _rep_reset()
        _p = RepStub(curl=[('{"ok":false,"error":"x"}', _code)])
        _pv = asyncio.run(_p.preview_report())
        _sr = asyncio.run(_p.send_report(_pv["id"]))
        ok(_sr.get("success") is False and _sr.get("error") == _err and not os.path.exists(_pending_path()),
           f"a {_code} is not kept for another try")
        ok("Copy diagnostics" in (_sr.get("message") or ""), f"and the panel says what to do instead ({_code})")
        _bg = RepStub(curl=[(json.dumps({"ok": True, "ref": _REF}), "200")])
        asyncio.run(_bg._send_pending_report_if_due())
        ok(not _bg.curl_calls(), f"so the background never sends it again ({_code})")
        # And a waiting report refused later is dropped too.
        os.makedirs(_rep_dir, mode=0o700, exist_ok=True)
        open(_pending_path(), "w").write(_p.bodies[0])
        _bg = RepStub(curl=[('{"ok":false}', _code), (json.dumps({"ok": True, "ref": _REF}), "200")])
        asyncio.run(_bg._send_pending_report_if_due())
        _bg._report_retry_at = 0
        asyncio.run(_bg._send_pending_report_if_due())
        ok(len(_bg.curl_calls()) == 1 and not os.path.exists(_pending_path()),
           f"a waiting report the receiver refuses with {_code} is dropped, not retried")

    # 429 and 503 keep it, and wait longer before the next try.
    for _code, _kind, _text in (("503", "unavailable", "The report service is busy."),
                                ("429", "rate_limited", "Too many reports from this network")):
        _rep_reset()
        _p = RepStub(curl=[('{"ok":false}', _code)])
        _pv = asyncio.run(_p.preview_report())
        _sr = asyncio.run(_p.send_report(_pv["id"]))
        ok(_sr.get("status") == "queued" and _text in (_sr.get("message") or "")
           and os.path.exists(_pending_path()),
           f"a {_code} keeps the report and says it will go later")
        _wait = getattr(_p, "_report_retry_at", 0) - time.monotonic()
        ok(_wait > m.REPORT_RETRY_AFTER["offline"] and abs(_wait - m.REPORT_RETRY_AFTER[_kind]) < 5,
           f"and the next try waits longer than for no connection ({_code}, {int(_wait)}s)")
        _p.curl = [(json.dumps({"ok": True, "ref": _REF}), "200")]
        asyncio.run(_p._send_pending_report_if_due())
        ok(len(_p.curl_calls()) == 1, f"so the watch does not try again straight away ({_code})")
    _rep_reset()
    # Old reports go unsent.
    os.makedirs(_rep_dir, mode=0o700, exist_ok=True)
    open(_pending_path(), "w").write('{"plugin_version":"0.24.5-beta","report":{"a":1}}')
    _old = time.time() - m.REPORT_PENDING_MAX_AGE - 60
    os.utime(_pending_path(), (_old, _old))
    _bg = RepStub(curl=[(json.dumps({"ok": True, "ref": _REF}), "200")])
    asyncio.run(_bg._send_pending_report_if_due())
    ok(not _bg.curl_calls() and not os.path.exists(_pending_path()),
       "a report left waiting for two weeks is dropped unsent")
    open(_pending_path(), "w").write('{"plugin_version":"$(id)","report":{}}')
    _bg = RepStub(curl=[(json.dumps({"ok": True, "ref": _REF}), "200")])
    asyncio.run(_bg._send_pending_report_if_due())
    ok(not _bg.curl_calls() and not os.path.exists(_pending_path()),
       "and so is a waiting file this plugin did not write")
    _rep_reset()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Send report: the waiting report is written safely")
try:
    _rep_reset()
    _victim_dir = tempfile.mkdtemp()
    _victim = os.path.join(_victim_dir, "victim")
    open(_victim, "w").write("PRECIOUS")
    os.makedirs(_rep_dir, mode=0o755)
    os.symlink(_victim, _pending_path())
    os.symlink(_victim, _pending_path() + ".tmp")
    ok(getattr(m, "_write_report_file", lambda n, d: False)(m.REPORT_PENDING_NAME, "{}") is True
       and open(_victim).read() == "PRECIOUS" and not os.path.islink(_pending_path())
       and open(_pending_path()).read() == "{}",
       "links planted at the report's names are replaced, never written through")
    ok(oct(os.stat(_rep_dir).st_mode & 0o777) == "0o700",
       "a report directory others could open is closed to them first")
    os.remove(_pending_path())
    os.symlink(_victim, _pending_path())
    ok(m._read_report_file(m.REPORT_PENDING_NAME, 1000) == (None, 0),
       "and a link is never read as a waiting report")
    _rep_reset()
    _away = tempfile.mkdtemp()
    os.symlink(_away, _rep_dir)
    _p = RepStub(curl=[("", 7)])
    _pv = asyncio.run(_p.preview_report())
    _sr = asyncio.run(_p.send_report(_pv["id"]))
    ok(os.listdir(_away) == [] and _sr.get("error") == "not_kept"
       and "couldn't be saved" in (_sr.get("message") or ""),
       "a report directory swapped for a link is refused, and the panel is told it was not kept")
    ok(_p.bodies and json.loads(_p.bodies[0]) == _pv["payload"],
       "while the send itself still went from a private file of its own")
    os.remove(_rep_dir)
    _left = [p for p in os.listdir(tempfile.gettempdir()) if p.startswith("wifi-optimizer-report-")]
    ok(not _left, f"which is removed afterwards ({_left})")
    ok(_src_main.count('REPORT_DIR = "/var/lib/wifi-optimizer"') == 1,
       "kept under /var/lib, which survives a reboot and only root writes")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Send report: the wake notice, from the previous boot's log")
try:
    _rep_reset()
    def _wj(rows, host=False):
        return "".join(f"{1700000000 + t}.123456 {'steamdeck ' if host else ''}{who}: {msg}\n"
                       for t, who, msg in rows)
    _NM = "NetworkManager[812]"
    _UP = [(10, "kernel", "wlan0: authenticate with xx:xx:xx:xx:1f:e7 (local address=3c:e9:f7:12:34:56)"),
           (11, "kernel", "wlan0: associated"),
           (12, _NM, "<info>  [1700000012.1] device (wlan0): state change: secondaries -> activated "
                     "(reason 'none', sys-iface-state: 'managed')"),
           (13, _NM, "<info>  [1700000013.1] device (wlan0): Activation: successful, device activated."),
           (14, _NM, "<info>  [1700000014.1] policy: set 'Smith Family 5G' (wlan0) as default")]
    _SLEEP = [(100, "kernel", "PM: suspend entry (s2idle)"), (500, "kernel", "PM: suspend exit")]
    _FAIL = _UP + _SLEEP + [
        (501, "kernel", "rtw_8822ce 0000:01:00.0: failed to leave lps state"),
        (502, "kernel", "rtw_8822ce 0000:01:00.0: failed to send h2c command"),
        (540, _NM, "<info>  [1700000540.1] device (wlan0): state change: disconnected -> prepare"),
        (600, _NM, "<info>  [1700000600.1] manager: NetworkManager state is now DISCONNECTED")]
    _w = getattr(m, "_wake_failure_at", lambda t: -1)
    ok(_w(_wj(_FAIL)) == 1700000500,
       "a wake with driver errors and no association after it is found, at the wake's time")
    ok(_w(_wj(_FAIL, host=True)) == 1700000500, "with or without a host column")
    for _rows, _why in (
        (_UP + [(600, "kernel", "rtw_8822ce 0000:01:00.0: failed to send h2c command")], "a boot that never slept"),
        (_FAIL + [(610, "kernel", "wlan0: associated")], "WiFi back after the wake (kernel)"),
        (_FAIL + [(610, _NM, "<info>  device (wlp1s0): state change: secondaries -> activated (reason 'none')")],
         "WiFi back after the wake (NetworkManager, a driver that does its own association)"),
        ([r for r in _FAIL if "rtw_8822ce" not in r[2]], "no driver error after the wake (WiFi switched off)"),
        (_UP + _SLEEP + [(503, "kernel", "rtw_8822ce 0000:01:00.0: failed to leave lps state")],
         "switched off seconds after waking"),
        ([r for r in _FAIL if r not in _UP], "WiFi never up before the sleep"),
        (_FAIL + [(700, "kernel", "PM: suspend entry (s2idle)"), (800, "kernel", "PM: suspend exit"),
                  (801, "kernel", "wlan0: associated"), (900, _NM, "<info> manager: sleep")],
         "an earlier failed wake, but the last one came back"),
        (_UP + _SLEEP + [(501, "kernel", "rtw_8822ce 0000:01:00.0: chip_id 0x2 firmware ok"),
                         (502, "kernel", "usb 1-1: device descriptor read failed"),
                         (600, _NM, "<info> manager: NetworkManager state is now DISCONNECTED")],
         "errors from something other than the WiFi driver"),
        ("", "an empty log"),
        ("garbage\n\x00\n-- No entries --\n", "lines that are not a log"),
    ):
        ok(_w(_rows if isinstance(_rows, str) else _wj(_rows)) == 0, f"no notice for {_why}")

    # The read: one, bounded, of the previous boot, cached for the load.
    _p = RepStub(journal=_wj(_FAIL))
    _s1 = asyncio.run(_p.get_report_state())
    _s2 = asyncio.run(_p.get_report_state())
    _jc = [c for c, _t in _p.calls if c[0] == "/usr/bin/journalctl"]
    # Pass 4 D5: a few bounded reads, all made on the first ask.
    _jc_first = len(_jc)
    asyncio.run(_p.get_report_state())
    ok(_jc_first <= 4 and len([c for c, _t in _p.calls if c[0] == "/usr/bin/journalctl"]) == _jc_first
       and _s1.get("wake_failure") is True and _s2.get("wake_failure") is True,
       f"the log is read once per load, however often the panel asks ({len(_jc)} reads)")
    _j = _jc[0] if _jc else []
    ok("-b" in _j and _j[_j.index("-b") + 1] == "-1" and "-n" in _j
       and int(_j[_j.index("-n") + 1]) <= 20000 and "--no-hostname" in _j
       and [t for c, t in _p.calls if c[0] == "/usr/bin/journalctl"][0] <= 10,
       f"the previous boot only, bounded in lines and time ({_j})")
    # Pass 4 C5: plus dropped.
    ok(set(_s1) == {"success", "enabled", "pending", "sent_ref", "wake_failure", "wake_failure_at",
                    "dropped"}
       and type(_s1["wake_failure"]) is bool and type(_s1["wake_failure_at"]) is int
       and _s1["wake_failure_at"] == 1700000500,
       f"the panel gets a yes or no and a time ({_s1})")
    _flat = json.dumps(_s1)
    ok(not any(w in _flat for w in ("Smith", "wlan0", "rtw", "3c:e9", "1f:e7", "lps", "kernel")),
       "and nothing from the log itself")
    ok(asyncio.run(RepStub(journal=None).get_report_state()).get("wake_failure") is False,
       "no previous boot in the journal is no notice")
    _d = asyncio.run(_p.dismiss_report_notice("wake"))
    ok(_d.get("success") is True and asyncio.run(_p.get_report_state()).get("wake_failure") is False,
       "dismissed, it goes")
    ok(asyncio.run(RepStub(journal=_wj(_FAIL)).get_report_state()).get("wake_failure") is False,
       "and stays gone after the plugin reloads, for that wake")
    _later = [(t + 2000, w, msg) for t, w, msg in _FAIL]
    ok(asyncio.run(RepStub(journal=_wj(_later)).get_report_state()).get("wake_failure") is True,
       "while a later failed wake brings it back")
    ok("Last time your device woke from sleep, WiFi didn't come back." in _tsx_rep
       and 'startReport("wake")' in _tsx_rep and 'dismissReportNotice("wake")' in _tsx_rep,
       "the notice says so plainly, with Send report and Dismiss")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("Send report: the panel shows it, then asks once")
try:
    ok("previewReport()" in _tsx_rep and "JSON.stringify(preview.payload, null, 2)" in _tsx_rep
       and "send(id, from)" in _tsx_rep and 'callable<[id: string], ReportResult>("send_report")' in _tsx_rep,
       "tapping the button shows the preview's payload, and only its id is sent")
    ok('strOKButtonText="Send"' in _tsx_rep and 'strCancelButtonText="Cancel"' in _tsx_rep
       and "overflowY: \"auto\"" in _tsx_rep and "onActivate={() => {}}" in _tsx_rep,
       "in a box a controller can step through, with one Send to confirm")
    ok("Dismiss" in _tsx_rep and "update({ outcome: null })" in _tsx_rep
       and "setTimeout(() => update" not in _tsx_rep,
       "the result stays on screen until dismissed")
    _texts = re.findall(r'"([^"\n]{12,})"', _tsx_rep) + re.findall(r">\s*([A-Z][^<>{}]{12,})\s*<", _tsx_rep)
    _texts += [v for v in getattr(m.Plugin, "_REPORT_MESSAGES", {}).values()]
    ok(_texts and all(all(ord(ch) < 128 for ch in t) for t in _texts),
       "the wording is plain ASCII: no curly quotes or long dashes")
    _readme = open("README.md").read()
    _sec = _readme[_readme.find("## Send report"):]
    _sec = _sec[:_sec.find("\n## ", 5)] if "\n## " in _sec[5:] else _sec
    ok(_readme.count("## Send report") == 1 and "Copy diagnostics" in _sec and "90 days" in _sec
       and "confirm" in _sec.lower() and "workers" not in _sec.lower()
       and "cloudflare" not in _sec.lower() and all(ord(ch) < 128 for ch in _sec),
       "the README says what is sent, where it goes and that nothing goes unconfirmed")
    # Uninstalling takes the report directory with it.
    _rep_reset()
    os.makedirs(_rep_dir, mode=0o700)
    for _n in (m.REPORT_PENDING_NAME, m.REPORT_SENT_NAME, m.REPORT_WAKE_SEEN_NAME):
        open(os.path.join(_rep_dir, _n), "w").write("x")
    ok("_forget_reports" in _src_main[_src_main.index("def _teardown"):_src_main.index("def _stop_update")],
       "uninstalling removes the report directory")
    getattr(m, "_forget_reports", lambda: None)()
    ok(not os.path.exists(_rep_dir), "and the removal takes everything in it")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
finally:
    try:
        m.REPORT_URL = _rep_url_real
    except NameError:
        pass

section("L1 the update script never takes itself for a stream, with the real pgrep")
try:
    import shutil as _l1_sh, subprocess as _l1_sp
    # Pass 4's X-S1: the patterns rode on the script's own command line
    # (argv 8), and playing() ran pgrep inside $(...), whose subshell is a
    # copy of the script under another PID. Every update found a "stream",
    # so the fallback Decky restart never ran. Checked here with the system's
    # own pgrep and real processes: a pgrep written as a shell function
    # cannot see the subshell, which is how this got through pass 3.
    _l1_dl = "https://github.com/ArcadaLabs-Jason/WifiOptimizer/releases/download"

    class _L1(m.Plugin):
        def __init__(self):
            super().__init__(); self.launched = []
        async def check_for_update(self):
            return {"success": True, "update_available": True, "channel": "beta",
                    "latest_version": "0.24.6-beta"}
        async def _signed_release(self, version):
            return {"ok": True, "url": f"{_l1_dl}/v{version}/wifi-optimizer-v{version}.tar.gz",
                    "sha256": "0" * 64}
        def _launch_updater(self, argv): self.launched.append(list(argv))

    # Defaults (every app watched) plus a name of the user's that matches
    # the script's own directory, which the field accepts. An app whose name
    # something on this machine already matches (macOS runs Apple's own
    # parsecd) is unticked, or the real-pgrep runs below could not tell a
    # self-match from it; Moonlight stays, as it is what they start.
    _l1_pgrep = _l1_sh.which("pgrep")
    _l1_host = {}
    if _l1_pgrep:
        for _app, _lab, _pts in m.STREAMING_APPS:
            _hit = _l1_sp.run([_l1_pgrep, "-if", m._stream_process_pattern(list(_pts))],
                              capture_output=True, text=True).stdout.split()
            if _hit and _app != "moonlight":
                _l1_host[_app] = False
    _l1_settings = {**m.DEFAULT_SETTINGS, "streaming_apps": _l1_host,
                    "streaming_custom_patterns": "wifi-optimizer-update"}
    m._save_settings(_l1_settings)
    _l1p = _L1()
    _l1r = asyncio.run(_l1p.apply_update("0.24.6-beta"))
    _l1_argv = _l1p.launched[0] if _l1p.launched else []
    _l1_dir = getattr(_l1p, "_update_dir", None) or ""
    ok(_l1r.get("success") and _l1_argv and os.path.isdir(_l1_dir),
       f"the update is launched ({_l1r})")
    _l1_pattern = m._stream_process_pattern(
        [pt for _l, pt in _l1p._stream_patterns(m._load_settings())])
    ok(not any(any(pt in a for pt in ("moonlight", "chiaki", "steamlink")) for a in _l1_argv[2:]),
       f"no stream pattern is on the command line the script is started with ({_l1_argv[2:]})")
    _l1_sf = os.path.join(_l1_dir, "streams")
    ok(os.path.isfile(_l1_sf) and open(_l1_sf).read() == _l1_pattern + "\n"
       and oct(os.stat(_l1_sf).st_mode & 0o777) == "0o600",
       "they are in a 0600 file beside the script instead")

    if not _l1_pgrep:
        print("SKIP  no pgrep here: the real-pgrep half runs on Linux and macOS")
    else:
        # The script the plugin wrote, cut down to its start (arguments,
        # traps, the patterns, its temporary directory) and playing(), run
        # from the directory apply_update made with the argv it was given.
        _l1_text = open(_l1_argv[1]).read()
        _l1_head = _l1_text[:_l1_text.index("STATE=/run/wifi-optimizer/update-state")]
        _l1_fn = _l1_text[_l1_text.index("playing() {"):]
        _l1_fn = _l1_fn[:_l1_fn.index("\n}\n") + 3]
        _l1_probe = (_l1_head
                     + "TMP=$(mktemp -d) || exit 1\n"
                     + "trap 'rm -rf \"$TMP\"' EXIT\ntrap 'exit 1' TERM INT HUP\n"
                     + _l1_fn.replace("/run/wifi-optimizer/streaming", "/nonexistent/wo-streaming")
                     + "if playing; then echo yes; else echo no; fi\n")
        _l1_path = os.path.join(_l1_dir, "update.sh")
        open(_l1_path, "w").write(_l1_probe)

        def _l1_run():
            return _l1_sp.run(["/bin/bash", _l1_path, *_l1_argv[2:]], capture_output=True,
                              text=True, timeout=30, env={**os.environ}).stdout.strip()

        # Something on this machine already matching (a real Moonlight on a
        # developer's desktop) leaves nothing to tell apart.
        for _ in range(10):
            _l1_busy = _l1_sp.run([_l1_pgrep, "-if", "steamlaunch appid=|" + _l1_pattern],
                                  capture_output=True, text=True).stdout.split()
            if not _l1_busy:
                break
            time.sleep(0.5)
        if _l1_busy:
            print(f"SKIP  a process here already matches the patterns ({_l1_busy[:3]})")
        else:
            _l1_seen = [_l1_run() for _ in range(3)]
            ok(_l1_seen == ["no", "no", "no"],
               f"with nothing matching running, the script finds no stream, 3 of 3 ({_l1_seen})")
            # sleep, started as "moonlight" (its argv[0]): macOS kills a
            # copied system binary, and pgrep -f reads the argv either way.
            _l1_moon = _l1_sp.Popen(["/usr/bin/moonlight", "30"],
                                    executable=_l1_sh.which("sleep") or "/bin/sleep")
            try:
                time.sleep(0.3)
                _l1_live = _l1_run()
            finally:
                _l1_moon.kill(); _l1_moon.wait()
            ok(_l1_live == "yes", f"and a real moonlight process running is one ({_l1_live})")
    _l1_sh.rmtree(_l1_dir, ignore_errors=True)
    _l1p._update_dir = None

    # The plugin's half: its own search must not take the update script, or
    # what the script and the plugin started, for Moonlight.
    _l1_saved_proc = m.PROC_DIR
    try:
        m.PROC_DIR = tempfile.mkdtemp()

        def _l1_proc(pid, argv, ppid, comm):
            os.mkdir(os.path.join(m.PROC_DIR, str(pid)))
            open(os.path.join(m.PROC_DIR, str(pid), "cmdline"), "wb").write(
                b"\0".join(a.encode() for a in argv) + b"\0")
            open(os.path.join(m.PROC_DIR, str(pid), "status"), "w").write(
                f"Name:\t{comm}\nPPid:\t{ppid}\n")
            open(os.path.join(m.PROC_DIR, str(pid), "comm"), "w").write(comm + "\n")

        # The script exactly as launched (systemd starts it, so its parent is 1).
        _l1_proc(70001, _l1_argv, 1, "bash")
        # The pgrep it runs carries every pattern on its own command line.
        _l1_proc(70002, ["pgrep", "-if", "steamlaunch appid=|" + _l1_pattern], 70001, "pgrep")
        # Something this backend started (nmcli, curl, systemd-run) whose
        # arguments a name of the user's matches.
        _l1_proc(70003, ["/usr/bin/systemd-run", "--unit", "wifi-optimizer-update-1-2", "--",
                         *_l1_argv], os.getpid(), "systemd-run")
        _l1_found = m.Plugin()._find_stream(m.Plugin()._stream_patterns(m._load_settings()))
        ok(_l1_found is None,
           f"the plugin's stream search skips the update script, what it runs, and what the "
           f"plugin itself started ({_l1_found})")
        _l1_proc(70004, ["/usr/bin/moonlight", "stream", "host"], 1, "moonlight")
        _l1_found = m.Plugin()._find_stream(m.Plugin()._stream_patterns(m._load_settings()))
        ok(bool(_l1_found) and _l1_found.get("app") == "Moonlight",
           f"while a real Moonlight beside them is still found ({_l1_found})")
    finally:
        m.PROC_DIR = _l1_saved_proc
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L2 uninstall keeps the record of pins it could not reach, and always removes its files")
try:
    _d, _restore = _l_paths()
    _l2_rep = tempfile.mkdtemp()
    _l2_rep_real = getattr(m, "REPORT_DIR", None)
    try:
        m.REPORT_DIR = _l2_rep
        _l2_pending = os.path.join(_l2_rep, getattr(m, "REPORT_PENDING_NAME", "pending-report.json"))
        open(_l2_pending, "w").write("{}")
        _U2 = "dddddddd-0000-0000-0000-0000000000a2"
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        open(m.MODPROBE_CONF_PATH, "w").write("options rtw88_pci disable_aspm=Y\n")
        open(m.DISPATCHER_PATH, "w").write("#!/bin/sh\n")
        # Our backend file naming iwd: putting the fallback back first is a
        # systemctl call, which here never answers.
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True,
                          "bssid_lock_uuids": [_U2], "pins_owned": ["802-11-wireless.bssid"]})

        class _L2(m.Plugin):
            def _run_cmd(self, cmd, timeout=5):
                # Every command a sleeper, through the real _run_cmd, which
                # holds each one to what is left of uninstall's budget.
                return m.Plugin._run_cmd(self, ["/bin/sleep", "30"], timeout)
            def _backend_without_ours(self): return "wpa_supplicant"
            def _get_wifi_interface(self): return None
            def _apply_driver_fixes(self, enable, persist=True, record_all=False): pass
            def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
            def _rotate_logs(self, keep=10): pass
        _l2_t = time.monotonic()
        _L2()._teardown(time.monotonic() + 4)
        _l2_took = time.monotonic() - _l2_t
        ok(_l2_took < 4.5, f"uninstall keeps to its budget with every command stuck ({_l2_took:.1f}s)")
        _l2_kept = json.load(open(m.SETTINGS_FILE)) if os.path.exists(m.SETTINGS_FILE) else {}
        ok(_U2 in _l2_kept.get("bssid_lock_uuids", []),
           f"pins it never got to stay recorded in a kept settings file, so a reinstall can take them off "
           f"({_l2_kept.get('bssid_lock_uuids')})")
        ok(not os.path.exists(_l2_pending),
           "the waiting report goes whatever the commands before it took")
        ok(not os.path.exists(m.NM_CONF_PATH) and not os.path.exists(m.MODPROBE_CONF_PATH)
           and not os.path.exists(m.DISPATCHER_PATH),
           "and so do our NetworkManager and modprobe files and the dispatcher")
    finally:
        m.REPORT_DIR = _l2_rep_real
        _restore()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L3 a set_power_save already under way cannot undo Reset or auto mode")
try:
    _d, _restore = _l_paths()
    try:
        class _L3(_LReset):
            def __init__(self):
                super().__init__(real_drivers=True)
            def _reload_nm_conf(self): time.sleep(0.6)
            def _paused_by_streaming(self): return False
            def _backend_switch_running(self): return False

        async def _l3_race(cls, second):
            p = cls()
            first = asyncio.create_task(p.set_power_save(True))
            await asyncio.sleep(0.2)
            await second(p)
            await first
            for t in [getattr(p, "_stream_task", None)]:
                if t: t.cancel()
            return p

        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "power_save_disabled": False})
        asyncio.run(_l3_race(_L3, lambda p: p.reset_settings()))
        _l3s = m._load_settings()
        ok(_l3s.get("power_save_disabled") is False and not os.path.exists(m.NM_CONF_PATH)
           and not os.path.exists(m.MODPROBE_CONF_PATH),
           f"Reset during a set_power_save(True) ends with power save released and none of our files "
           f"({_l3s.get('power_save_disabled')}, NM {os.path.exists(m.NM_CONF_PATH)}, "
           f"modprobe {os.path.exists(m.MODPROBE_CONF_PATH)})")
        for _p in (m.NM_CONF_PATH, m.MODPROBE_CONF_PATH):
            if os.path.exists(_p): os.remove(_p)
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "power_save_disabled": False})

        class _L3S(_L3):
            async def reapply_volatile(self): return {"success": True}
            def _revert_volatile_fixes(self, settings): pass
            def _find_stream(self, patterns, wait=2.0): return None
        asyncio.run(_l3_race(_L3S, lambda p: p.set_streaming_mode(True)))
        ok(not os.path.exists(m.MODPROBE_CONF_PATH),
           "and turning auto mode on during one leaves no modprobe options behind")
    finally:
        for _p in (m.NM_CONF_PATH, m.MODPROBE_CONF_PATH):
            if os.path.exists(_p): os.remove(_p)
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L4 an iw stuck in the driver: power save comes off anyway, waits end early, other cards still work")
try:
    _d, _restore = _l_paths()
    try:
        class _L4Stuck:
            """A child left behind stuck, as _abandon records it."""
            def __init__(self, args): self.args = args
            def poll(self): return None

        class _L4(m.Plugin):
            def _get_wifi_interface(self): return "wlan0"
            def _managed_interface(self): return "wlan0"
            def _apply_driver_fixes(self, enable, persist=True, record_all=False): pass
            def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
            def _reload_nm_conf(self): pass
        _l4 = _L4()
        _l4._abandoned_record().append(("iw", _L4Stuck(["/usr/sbin/iw", "dev", "wlan0", "link"])))
        open(m.NM_CONF_PATH, "w").write("[connection]\nwifi.powersave = 2\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": True})
        _r4 = asyncio.run(_l4.set_power_save(False))
        ok(_r4.get("success") is True and "isn't responding" in (_r4.get("message") or "")
           and "restart the device" in (_r4.get("message") or ""),
           f"turning power save back on with iw stuck still undoes the rest, and says so ({_r4})")
        ok(m._load_settings().get("power_save_disabled") is False and not os.path.exists(m.NM_CONF_PATH),
           "the setting reads off and our NetworkManager file is gone")
        m._save_settings({**m.DEFAULT_SETTINGS, "power_save_disabled": False})
        _r4 = asyncio.run(_l4.set_power_save(True))
        ok(_r4.get("success") is False and "isn't responding" in (_r4.get("message") or "")
           and m._load_settings().get("power_save_disabled") is False,
           f"turning it on is refused, saying the card isn't responding ({_r4.get('message')!r})")
        _l4._ASSOCIATION_WAIT_SECONDS = 4
        _t4 = time.monotonic()
        _a4 = asyncio.run(_l4._await_association("wlan0"))
        _took4 = time.monotonic() - _t4
        ok(_took4 < 1.0 and "still stuck" in (_a4.get("error") or ""),
           f"waiting for the link ends at once with the stuck error ({_took4:.1f}s, {_a4.get('error')!r})")
        _o4 = _l4._run_cmd(["/usr/bin/iw", "dev", "wlan1", "link"], timeout=2)
        ok("still stuck" not in (_o4.get("stderr") or ""),
           f"an iw stuck for wlan0 does not refuse iw for another card ({_o4.get('stderr')!r})")
        _o4 = _l4._run_cmd(["/usr/bin/iw", "dev", "wlan0", "link"], timeout=2)
        _g4 = _l4._run_cmd(["/usr/bin/iw", "reg", "get"], timeout=2)
        ok("still stuck" in (_o4.get("stderr") or "") and "still stuck" in (_g4.get("stderr") or ""),
           "while iw for the stuck card, and iw that names no card, are still not started")
        ok(_l4._stuck_tools() == {"iw"}, "and the stuck tools are still named by tool")
    finally:
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L5 Reset says partial when a part was not undone, and pins left get their own words")
try:
    _d, _restore = _l_paths()
    try:
        class _L5Backend(_LReset):
            def _backend_without_ours(self): return "wpa_supplicant"
            def _release_generic_backend_conf(self):
                return m.Plugin._release_generic_backend_conf(self)
            def _run_cmd(self, cmd, timeout=5):
                self.cmds.append(cmd)
                if cmd[:2] == ["/usr/bin/systemctl", "enable"]:
                    return {"success": False, "stdout": "", "stderr": "Failed to enable", "returncode": 1}
                return _ok()
        open(m.GENERIC_BACKEND_CONF, "w").write("[device]\nwifi.backend=iwd\n")
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88"})
        _r5 = asyncio.run(_L5Backend(left={}).reset_settings())
        ok(_r5.get("error") == "partial" and os.path.exists(m.GENERIC_BACKEND_CONF),
           f"our backend file kept, because the fallback could not be enabled, is a partial Reset ({_r5})")
        os.remove(m.GENERIC_BACKEND_CONF)

        class _L5Cake(_LReset):
            def _get_wifi_interface(self): return "wlan0"
            def _revert_queue_tuning(self, settings):
                return m.Plugin._revert_queue_tuning(self, settings)
            def _run_cmd(self, cmd, timeout=5):
                self.cmds.append(cmd)
                if cmd[:3] == ["/usr/bin/tc", "qdisc", "del"]:
                    return {"success": False, "stdout": "", "stderr": "RTNETLINK answers: Operation not permitted",
                            "returncode": 2}
                if cmd[:3] == ["/usr/bin/tc", "qdisc", "show"]:
                    return _ok("qdisc cake 8001: root refcnt 2 bandwidth unlimited")
                return _ok()
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "cake_enabled": True})
        _r5 = asyncio.run(_L5Cake(left={}).reset_settings())
        ok(_r5.get("error") == "partial", f"CAKE that would not come off is a partial Reset ({_r5})")

        _U5 = "dddddddd-0000-0000-0000-0000000000a5"
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "bssid_lock_uuids": [_U5]})
        _r5 = asyncio.run(_LReset(left={"bssid_lock_uuids": [_U5]}).reset_settings())
        ok(_r5.get("success") is True and "retries while it runs" in (_r5.get("message") or "")
           and "Try Reset again" not in (_r5.get("message") or ""),
           f"a pin left over is not a failure: it is said to be retried ({_r5})")
        ok(m._load_settings().get("bssid_lock_uuids") == [_U5], "and it stays tracked")
    finally:
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L6 a watch pass whose search went stale under a settled pass searches again")
try:
    _l6_moon = {"app": "Moonlight", "process": "moonlight"}

    class _L6(m.Plugin):
        def __init__(self):
            super().__init__(); self.reapplied = 0; self.reverted = 0
        def _find_stream(self, patterns, wait=2.0):
            if any(pt == "moonlight" for _l, pt in patterns):
                time.sleep(0.3)  # the watch pass's slow search, Moonlight still ticked
                return dict(_l6_moon)
            return None
        async def reapply_volatile(self):
            self.reapplied += 1; return {"success": True}
        def _revert_volatile_fixes(self, settings): self.reverted += 1

    async def _l6_go():
        p = _L6()
        p._set_stream_flag(True)  # Moonlight was streaming, the fixes are on
        watch = asyncio.create_task(p._stream_pass())
        await asyncio.sleep(0.05)
        # The user unticks Moonlight; set_streaming_app's settled pass runs.
        m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True,
                          "streaming_apps": {"moonlight": False}})
        await p._stream_pass(settle=True)
        await watch
        return p
    m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})
    _l6 = asyncio.run(_l6_go())
    ok(_l6.reverted == 1 and _l6.reapplied == 0 and not os.path.exists(m.STREAM_FLAG_FILE),
       f"the fixes come off and stay off: the stale answer does not put them back "
       f"(reverted {_l6.reverted}, reapplied {_l6.reapplied})")
    for _p in (m.STREAM_FLAG_FILE, m.STREAM_HOLD_FILE):
        if os.path.exists(_p): os.remove(_p)
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L7 one process search at a time, and the status read never waits on one")
try:
    import threading as _l7_th
    # A command line whose read never returns: a FIFO nobody writes stands
    # in for /proc/PID/cmdline of a process stuck in the kernel, read with
    # the plugin's real open() and read().
    _l7_saved = (m.PROC_DIR, os.listdir)
    _l7_dirs = []
    try:
        m.PROC_DIR = tempfile.mkdtemp()
        os.mkdir(os.path.join(m.PROC_DIR, "5001"))
        _l7_fifo = os.path.join(m.PROC_DIR, "5001", "cmdline")
        os.mkfifo(_l7_fifo)
        _l7_real_listdir = os.listdir

        def _l7_listdir(path="."):
            if path == m.PROC_DIR:
                _l7_dirs.append(time.monotonic())
            return _l7_real_listdir(path)
        os.listdir = _l7_listdir
        m._save_settings({**m.DEFAULT_SETTINGS, "streaming_mode_enabled": True})

        class _L7(m.Plugin):
            async def reapply_volatile(self): return {"success": True}
            def _revert_volatile_fixes(self, settings): pass
            def _run_cmd(self, cmd, timeout=5): return _ok()
        _l7p = _L7()
        _l7_done = []

        def _l7_pokes():
            async def go():
                await asyncio.gather(*[_l7p.poke_detection() for _ in range(10)])
            asyncio.run(go())
            _l7_done.append("pokes")

        def _l7_status():
            _l7p._lock_scan_is_fresh(m._load_settings(), "wlan0", time.monotonic())
            _l7_done.append("status")

        _l7_t1 = _l7_th.Thread(target=_l7_pokes, daemon=True); _l7_t1.start()
        time.sleep(1.0)
        _l7_started = len(_l7_dirs)
        _l7_t2 = _l7_th.Thread(target=_l7_status, daemon=True); _l7_t2.start()
        _l7_t2.join(1.5)
        _l7_status_done = "status" in _l7_done
        ok(_l7_started == 1, f"ten pokes at once make one process search, not ten ({_l7_started})")
        ok(_l7_status_done,
           "a status read with a lock rescan due completes while that search is stuck")
    finally:
        os.listdir = _l7_saved[1]
        # Let every reader still blocked on the FIFO go.
        _l7_end = time.monotonic() + 8
        while time.monotonic() < _l7_end:
            try:
                _fd = os.open(_l7_fifo, os.O_WRONLY | os.O_NONBLOCK); os.close(_fd)
            except OSError:
                pass
            if all(not t.is_alive() for t in _l7_th.enumerate()
                   if t.name in ("wifi-optimizer-process-search",) or t in (_l7_t1, _l7_t2)):
                break
            time.sleep(0.05)
        m.PROC_DIR = _l7_saved[0]
    for _p in (m.STREAM_FLAG_FILE, m.STREAM_HOLD_FILE):
        if os.path.exists(_p): os.remove(_p)
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L8 Reset reads the settings after its await, and no IPv6 adoption lands under a profile change")
try:
    _d, _restore = _l_paths()
    try:
        _U8 = "dddddddd-0000-0000-0000-0000000000a8"

        class _L8(_LReset):
            def __init__(self):
                super().__init__(); self.cleared_from = None
            async def get_device_info(self):
                # A status pass on the loop records a profile meanwhile.
                s = m._load_settings(); s["ipv6_uuids"] = [_U8]; m._save_settings(s)
                return await super().get_device_info()
            def _clear_profile_pins(self, deadline=None, settings=None):
                self.cleared_from = list((settings or {}).get("ipv6_uuids", []))
                return {"bssid_lock_uuids": [], "band_preference_uuids": [], "ipv6_uuids": [],
                        "dns_uuids": [], "wpa2_uuids": [], "wpa2_restore_uuids": []}
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "ipv6_disabled": True})
        _l8 = _L8(); asyncio.run(_l8.reset_settings())
        ok(_l8.cleared_from == [_U8],
           f"a profile recorded during Reset's device read reaches its pin removal ({_l8.cleared_from})")
        m._save_settings({**m.DEFAULT_SETTINGS, "ipv6_disabled": True, "ipv6_uuids": []})
        _l8b = m.Plugin(); _l8b._profile_change_depth = 1
        _l8b._apply_status_actions({"live": {}, "drift": {}}, {},
                                   [{"kind": "ipv6_adopt", "uuid": _U8}])
        ok(m._load_settings().get("ipv6_uuids") == [],
           "and the poll adopts no profile while a profile change (Reset) is under way")
        _l8b._profile_change_depth = 0
        _l8b._apply_status_actions({"live": {}, "drift": {}}, {},
                                   [{"kind": "ipv6_adopt", "uuid": _U8}])
        ok(m._load_settings().get("ipv6_uuids") == [_U8], "but does once nothing is")
    finally:
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L9 bounded waits leave the event loop: iw at load, power save's iw, DNS off; one leftover call per poll")
try:
    import threading as _l9_th
    _d, _restore = _l_paths()
    try:
        class _L9Boot(_LBoot):
            def __init__(self):
                super().__init__(); self.where = []
            def _run_cmd(self, cmd, timeout=5):
                self.where.append((cmd[:3], _l9_th.current_thread() is _l9_th.main_thread()))
                return _ok()
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "regdomain": "US",
                          "settings_version": m.Plugin._SETTINGS_VERSION})
        _b9 = _L9Boot(); _l_main(_b9)
        _reg9 = [main for c, main in _b9.where if c == ["/usr/bin/iw", "reg", "set"]]
        ok(_reg9 and not any(_reg9), f"_main's iw reg set runs off the event loop ({_reg9})")

        class _L9Ps(m.Plugin):
            def __init__(self):
                super().__init__(); self.where = []
            def _get_wifi_interface(self):
                self.where.append(("lookup", _l9_th.current_thread() is _l9_th.main_thread()))
                return "wlan0"
            def _run_cmd(self, cmd, timeout=5):
                self.where.append((cmd[0], _l9_th.current_thread() is _l9_th.main_thread()))
                return _ok()
            def _apply_driver_fixes(self, enable, persist=True, record_all=False): pass
            def _apply_pcie_aspm_fix(self, enable, record_all=False): return []
            def _reload_nm_conf(self): pass
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _p9 = _L9Ps(); asyncio.run(_p9.set_power_save(True))
        _iw9 = [main for what, main in _p9.where if what in ("/usr/bin/iw", "lookup")]
        ok(_iw9 and not any(_iw9), f"set_power_save's interface lookup and iw run off the event loop ({_p9.where})")

        _A9, _B9 = "dddddddd-0000-0000-0000-0000000000a9", "dddddddd-0000-0000-0000-0000000000b9"

        class _L9Dns(m.Plugin):
            def __init__(self):
                super().__init__(); self.where = []
            def _resolve_uuid(self, msg=None): return _A9, None
            def _nmcli_modify(self, uuid, key, value, timeout=5):
                self.where.append((uuid, _l9_th.current_thread() is _l9_th.main_thread()))
                return _ok()
            async def _reconnect(self, uuid): return True
            def _get_wifi_interface(self): return "wlan0"
        m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": True, "dns_uuids": [_A9, _B9]})
        _n9 = _L9Dns(); _rn9 = asyncio.run(_n9.set_dns(False))
        _oth9 = [main for u, main in _n9.where if u == _B9]
        ok(_rn9.get("success") and _oth9 and not any(_oth9)
           and m._load_settings().get("dns_uuids") == [],
           f"turning DNS off gives the other profiles their DNS back off the event loop ({_rn9}, {_n9.where})")

        class _L9Left(m.Plugin):
            def __init__(self):
                super().__init__(); self.calls = []
            def _nmcli_modify(self, uuid, key, value, timeout=5):
                self.calls.append((key, timeout))
                if key == "ipv4.dns":
                    return {"success": False, "stdout": "", "stderr": "busy", "returncode": 1}
                return _ok()
            def _run_cmd(self, cmd, timeout=5):
                if cmd[:1] == ["/usr/bin/nmcli"] and "show" in cmd:
                    self.calls.append(("show", timeout))
                    return _ok("802-11-wireless-security.key-mgmt:wpa-psk")
                return _ok()
            def _get_wifi_interface(self): return None
            def _get_active_connection_uuid(self): return None
        m._save_settings({**m.DEFAULT_SETTINGS, "dns_enabled": False, "dns_uuids": [_A9],
                          "wpa2_restore_uuids": [_B9]})
        _lf9 = _L9Left(); _per9 = []
        for _ in range(4):
            _before9 = len(_lf9.calls); asyncio.run(_lf9.get_status()); _per9.append(len(_lf9.calls) - _before9)
        ok(max(_per9) <= 1 and all(t <= 1 for _k, t in _lf9.calls),
           f"a poll with leftovers failing makes one nmcli call, of a second at most ({_per9}, {_lf9.calls})")
    finally:
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L10 L11 Allow WPA2: the relock's own note is passed on, and the change is recorded before it is made")
try:
    _d, _restore = _l_paths()
    try:
        _W10 = "dddddddd-0000-0000-0000-0000000000aa"

        class _L10(m.Plugin):
            def __init__(self, reconnects=True):
                super().__init__(); self.km = {_W10: "sae"}; self.listed_during = None
                self.reconnects = reconnects
                self._wpa2_blocked = {"signal": 80, "current_signal": 40}; self._reach_checked_for = _W10
            def _resolve_uuid(self, msg=None): return _W10, None
            def _get_profile_ssid(self, uuid, timeout=5): return "Home"
            def _profile_key_mgmt(self, uuid): return self.km.get(uuid, "")
            def _nmcli_modify(self, uuid, key, value, timeout=5):
                self.km[uuid] = value; return _ok()
            async def _reconnected_to(self, uuid):
                if self.listed_during is None:
                    self.listed_during = uuid in m._load_settings().get("wpa2_uuids", [])
                return self.reconnects
            async def set_bssid_lock(self, enabled):
                return {"success": True, "bssid_locked": True,
                        "message": "Locked to the access point in use; the stronger one would not take."}
        m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_enabled": True})
        _p10 = _L10(); _r10 = asyncio.run(_p10.allow_wpa2())
        ok(_r10.get("success") and "stronger one would not take" in (_r10.get("message") or ""),
           f"a relock that went through with a note of its own is passed on ({_r10})")
        ok(_p10.listed_during is True,
           "the profile is already recorded while the reconnect is awaited")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _p11 = _L10(reconnects=False); _r11 = asyncio.run(_p11.allow_wpa2())
        ok(_r11.get("success") is False and _p11.km[_W10] == "sae"
           and _W10 not in m._load_settings().get("wpa2_uuids", []),
           f"put back to WPA3 only after a failed reconnect, it is unlisted again ({_r11})")
    finally:
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L12 a Reset cut off after its first save leaves Allow WPA2 on the list the poll retries")
try:
    _d, _restore = _l_paths()
    try:
        _W12 = "dddddddd-0000-0000-0000-0000000000ab"

        class _L12(_LReset):
            def _release_power_save(self, settings=None, reload_conf=False):
                time.sleep(0.6)  # in a worker thread; the task is cancelled meanwhile
                return True
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "wpa2_uuids": [_W12]})

        async def _l12_go():
            t = asyncio.create_task(_L12(left={}).reset_settings())
            await asyncio.sleep(0.3)
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        asyncio.run(_l12_go())
        _s12 = m._load_settings()
        ok(_s12.get("wpa2_restore_uuids") == [_W12] and _s12.get("wpa2_uuids") == [],
           f"the profile is on wpa2_restore_uuids from Reset's first save "
           f"({_s12.get('wpa2_uuids')}, {_s12.get('wpa2_restore_uuids')})")
    finally:
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L13 L14 an unreadable settings file is never written over, and Reset can still go on")
try:
    _d, _restore = _l_paths()
    try:
        open(m.SETTINGS_FILE, "w").write("{not json")
        os.mkdir(m.SETTINGS_FILE + ".corrupt")
        m._load_settings()
        try:
            m._save_settings(dict(m.DEFAULT_SETTINGS)); _saved13 = True
        except getattr(m, "SettingsSaveError", OSError):
            _saved13 = False
        ok(not _saved13 and open(m.SETTINGS_FILE).read() == "{not json",
           "a file that does not parse and cannot be set aside is not replaced by the next save")
        os.rmdir(m.SETTINGS_FILE + ".corrupt"); os.remove(m.SETTINGS_FILE)
        m._load_settings()

        # A file whose read fails outright (an I/O error) is left in place
        # and every save refused; Reset sets it aside and goes on.
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "bssid_lock_enabled": True})
        m._SETTINGS_STATE["unread"] = m._settings_file_identity()
        m._SETTINGS_STATE["read_failed"] = True
        _r14 = asyncio.run(_LReset(left={}).reset_settings())
        ok(_r14.get("success") is True and os.path.isfile(m.SETTINGS_FILE + ".corrupt")
           and m._load_settings().get("bssid_lock_enabled") is False,
           f"Reset sets the unreadable file aside as .corrupt and resets ({_r14})")
        os.remove(m.SETTINGS_FILE + ".corrupt")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        os.mkdir(m.SETTINGS_FILE + ".corrupt")
        m._SETTINGS_STATE["unread"] = m._settings_file_identity()
        m._SETTINGS_STATE["read_failed"] = True
        _r14 = asyncio.run(_LReset(left={}).reset_settings())
        ok(_r14.get("success") is False and "settings.json" in (_r14.get("message") or "")
           and "Remove" in (_r14.get("message") or ""),
           f"and when even that fails, says which file to remove ({_r14.get('message')!r})")
    finally:
        m._SETTINGS_STATE["unread"] = None
        m._SETTINGS_STATE["read_failed"] = False
        if os.path.isdir(m.SETTINGS_FILE + ".corrupt"):
            os.rmdir(m.SETTINGS_FILE + ".corrupt")
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L15 L16 update refusals say what is wrong, all of it")
try:
    class _L15(m.Plugin):
        async def check_for_update(self):
            return {"success": False, "update_available": False,
                    "message": "Couldn't reach GitHub. Check your connection and try again."}
        def _launch_updater(self, argv): raise AssertionError("must not launch")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    _r15 = asyncio.run(_L15().apply_update("0.24.6"))
    ok(_r15.get("success") is False and "Couldn't reach GitHub" in (_r15.get("message") or ""),
       f"an update that could not check GitHub says so, not 'No update available' ({_r15})")
    _l16_saved = m.PROC_DIR
    try:
        m.PROC_DIR = tempfile.mkdtemp()
        os.mkdir(os.path.join(m.PROC_DIR, "4243"))
        open(os.path.join(m.PROC_DIR, "4243", "cmdline"), "wb").write(
            b"/home/deck/.local/share/Steam/ubuntu12_32/reaper\0SteamLaunch\0AppId=1091500\0--\0game.exe\0")
        open(os.path.join(m.PROC_DIR, "4243", "comm"), "wb").write(b"reaper\n")
        open(m.STREAM_HOLD_FILE, "w").write("1\n")
        _m16 = m.Plugin()._update_blocker(m._load_settings()) or ""
        ok("Close your game" in _m16 and "Keep fixes on" in _m16,
           f"a game running with Keep fixes on also on names both in one message ({_m16!r})")
    finally:
        m.PROC_DIR = _l16_saved
        if os.path.exists(m.STREAM_HOLD_FILE): os.remove(m.STREAM_HOLD_FILE)
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L17 custom DNS from an older version is brought up to date at load, not reported as drift")
try:
    _d, _restore = _l_paths()
    try:
        _U17 = "dddddddd-0000-0000-0000-0000000000b7"
        _prof17 = {"ipv4.dns": "1.1.1.1,1.0.0.1", "ipv4.ignore-auto-dns": "yes",
                   "ipv6.ignore-auto-dns": "no"}

        class _L17(_LBoot):
            def _nmcli_modify(self, uuid, key, value, timeout=5):
                if uuid == _U17: _prof17[key] = value
                return _ok()
            def _run_cmd(self, cmd, timeout=5):
                self.cmds.append(cmd)
                if cmd[:1] == ["/usr/bin/nmcli"] and "show" in cmd and _U17 in cmd:
                    return _ok("\n".join(f"{k}:{v}" for k, v in _prof17.items()))
                return _ok()
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "settings_version": 2,
                          "dns_enabled": True, "dns_provider": "cloudflare",
                          "dns_servers": "1.1.1.1,1.0.0.1", "dns_uuids": [_U17]})
        _b17 = _L17(); _l_main(_b17)
        ok(_prof17["ipv6.ignore-auto-dns"] == "yes",
           f"the first load of this version lets no router IPv6 DNS in on the tracked profile ({_prof17})")
        _st17 = {"live": {}, "drift": {}}
        _b17._check_dns_drift(m._load_settings(), _U17, _st17)
        ok(not _st17["drift"].get("dns"), f"and the drift check then reports nothing ({_st17})")
        _cons17 = [c for c in _b17.cmds if c[:1] == ["/usr/bin/nmcli"] and "up" in c]
        ok(not _cons17, "with no reconnect")
    finally:
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L18 no band drift is reported during the hold after the band was released")
try:
    _p18 = m.Plugin()
    _p18._released_at = {"802-11-wireless.band": time.time() - 60}
    _s18 = {**m.DEFAULT_SETTINGS, "band_preference_enabled": True, "band_preference": "a",
            "band_preference_ssid": "Home"}
    _st18 = {"live": {"frequency": "2437"}, "drift": {}}
    _act18 = []
    _p18._visible_networks = lambda *a, **k: {}
    _p18._propose_band(_s18, "u", "bg", lambda: "Home", _st18, _act18)
    ok(not _st18["drift"].get("band_preference") and not _act18,
       f"released a minute ago, a different band is not drift yet ({_st18['drift']}, {_act18})")
    _p18._released_at = {"802-11-wireless.band": time.time() - 3600}
    _st18 = {"live": {"frequency": "2437"}, "drift": {}}
    _p18._propose_band(_s18, "u", "bg", lambda: "Home", _st18, [])
    ok(_st18["drift"].get("band_preference") is True, "after the hold it is again")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L19 the plugin waits for a dispatcher run holding the apply lock before taking the fixes off")
try:
    import threading as _l19_th
    import fcntl as _l19_fcntl
    _d, _restore = _l_paths()
    _l19_lock = getattr(m, "APPLY_LOCK_FILE", os.path.join(tempfile.mkdtemp(), "apply.lock"))

    def _l19_hold(seconds, ready):
        fd = os.open(_l19_lock, os.O_RDWR | os.O_CREAT, 0o644)
        _l19_fcntl.flock(fd, _l19_fcntl.LOCK_EX)
        ready.set()
        time.sleep(seconds)
        os.close(fd)

    try:
        class _L19(_LReset):
            def __init__(self):
                super().__init__(); self.reverted_at = None
            def _paused_by_streaming(self): return False
            def _backend_switch_running(self): return False
            def _apply_driver_fixes(self, enable, persist=True, record_all=False):
                if not enable and self.reverted_at is None:
                    self.reverted_at = time.monotonic()
            def _reload_nm_conf(self): pass

        def _l19_run(cls, coro_fn, setup):
            setup()
            ready = _l19_th.Event()
            holder = _l19_th.Thread(target=_l19_hold, args=(1.0, ready), daemon=True)
            holder.start(); ready.wait(2)
            released = time.monotonic() + 1.0
            p = cls()
            mid = {}

            async def go():
                task = asyncio.create_task(coro_fn(p))
                await asyncio.sleep(0.5)
                mid["saved_off"] = m._load_settings().get("power_save_disabled") is False
                await task
            asyncio.run(go())
            holder.join(3)
            return p, released, mid

        _p19, _rel19, _mid19 = _l19_run(
            _L19, lambda p: p.set_power_save(False),
            lambda: m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "power_save_disabled": True}))
        ok(_p19.reverted_at is not None and _p19.reverted_at >= _rel19 - 0.05 and not _mid19["saved_off"],
           f"turning power save off saves and takes the fixes off only once the lock is free "
           f"({None if _p19.reverted_at is None else round(_p19.reverted_at - _rel19, 2)}, saved early: {_mid19})")
        _p19, _rel19, _mid19 = _l19_run(
            _L19, lambda p: p.reset_settings(),
            lambda: m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "power_save_disabled": True}))
        ok(_p19.reverted_at is not None and _p19.reverted_at >= _rel19 - 0.05,
           "and so does Reset's power save step")

        class _L19S(_L19):
            def _revert_volatile_fixes(self, settings):
                if self.reverted_at is None: self.reverted_at = time.monotonic()
            def _find_stream(self, patterns, wait=2.0): return None

        def _l19_stream_setup():
            m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88", "streaming_mode_enabled": True})
            m.Plugin()._set_stream_flag(True)
        _p19, _rel19, _mid19 = _l19_run(_L19S, lambda p: p._stream_pass(settle=True), _l19_stream_setup)
        ok(_p19.reverted_at is not None and _p19.reverted_at >= _rel19 - 0.05
           and not os.path.exists(m.STREAM_FLAG_FILE),
           "and the end of a stream in auto mode")

        # Reset during a stream pass that is taking the fixes off: both end.
        _l19_stream_setup()

        async def _l19_both():
            p = _L19S()
            await asyncio.wait_for(asyncio.gather(p._stream_pass(settle=True), p.reset_settings()), 15)
        _t19 = time.monotonic(); asyncio.run(_l19_both())
        ok(time.monotonic() - _t19 < 15, "and Reset during a stream pass deadlocks on nothing")
    finally:
        for _p in (m.STREAM_FLAG_FILE, m.STREAM_HOLD_FILE):
            if os.path.exists(_p): os.remove(_p)
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L20 uninstall removes the earlier logs and the empty settings folder")
try:
    _d, _restore = _l_paths()
    _l20_real = (getattr(m.decky, "DECKY_PLUGIN_LOG_DIR", None), getattr(m.decky, "DECKY_PLUGIN_LOG", None))
    try:
        _l20_logs = tempfile.mkdtemp()
        for _i, _n in enumerate(("2026-10-01.log", "2026-10-02.log", "2026-10-03.log")):
            open(os.path.join(_l20_logs, _n), "w").write("x\n")
            os.utime(os.path.join(_l20_logs, _n), (1e9 + _i, 1e9 + _i))
        m.decky.DECKY_PLUGIN_LOG_DIR = _l20_logs
        m.decky.DECKY_PLUGIN_LOG = os.path.join(_l20_logs, "2026-10-03.log")
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        _LTear()._teardown(time.monotonic() + 4)
        ok(sorted(os.listdir(_l20_logs)) == ["2026-10-03.log"],
           f"only the log being written stays ({sorted(os.listdir(_l20_logs))})")
        ok(not os.path.exists(os.path.dirname(m.SETTINGS_FILE)),
           "and the settings folder goes once nothing is kept in it")
        os.makedirs(os.path.dirname(m.SETTINGS_FILE), exist_ok=True)
        _U20 = "dddddddd-0000-0000-0000-0000000000c0"

        class _L20Kept(_LTear):
            def _clear_profile_pins(self, deadline=None, settings=None):
                return {"bssid_lock_uuids": [_U20], "band_preference_uuids": [], "ipv6_uuids": [],
                        "dns_uuids": [], "wpa2_uuids": [], "wpa2_restore_uuids": []}
        m._save_settings({**m.DEFAULT_SETTINGS, "bssid_lock_uuids": [_U20]})
        _L20Kept()._teardown(time.monotonic() + 4)
        ok(os.path.isfile(m.SETTINGS_FILE), "while a settings file kept for pins keeps its folder")
    finally:
        m.decky.DECKY_PLUGIN_LOG_DIR, m.decky.DECKY_PLUGIN_LOG = _l20_real
        os.makedirs(os.path.dirname(m.SETTINGS_FILE), exist_ok=True)
        _restore()
    m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("L21 L22 no dead relock helper, and messages that say what to do next")
try:
    ok(not hasattr(m.Plugin, "_relock_after_band_change"),
       "the band change's relock helper that nothing called is gone")
    _src22 = open("main.py").read()
    ok("6 GHz requires them. Change it in Desktop Mode's network settings to use 6 GHz." in
       re.sub(r'"\s*\n\s*"', "", _src22),
       "the 6 GHz refusal for management frames says where to change it")
    ok("Couldn't remove the auto-fix script. Restart the device, then try again." in _src22
       and "Couldn't change the auto-fix script. Restart the device, then try again." in _src22,
       "and the auto-fix script failures say to restart and try again")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

# ---- Pass 4, DEVICES ----
# The kernel's own line order around a real S3 sleep, measured on a Steam Deck
# OLED (SteamOS 3.8.28, session-artifacts deck-measurements-2026-10-22.md
# section 2): suspend entry, the SD card's cache flush error, freezing, ACPI
# going down; ACPI coming back, "Waking up", the device resume block,
# restarting tasks, "PM: suspend exit"; then on the OLED ath11k probed afresh,
# and wlan0 associating about ten seconds later.
_P4_U = 1700000000
_P4_NM = "NetworkManager[812]"

def _p4_cycle(base, errors=(), reprobe=False, associate=False, attempt=True, end=600):
    """(seconds from _P4_U, who, message) for one sleep and wake at base."""
    rows = [(base, "kernel", "PM: suspend entry (deep)"),
            (base + 0.1, "kernel", "mmc0: error -110 writing Cache Flush bit"),
            (base + 0.2, "kernel", "Freezing user space processes"),
            (base + 0.5, "kernel", "ACPI: PM: Preparing to enter system sleep state S3"),
            (base + 3000.0, "kernel", "ACPI: PM: Low-level resume complete"),
            (base + 3000.1, "kernel", "ACPI: PM: Waking up from system sleep state S3"),
            (base + 3000.3, "kernel", "amdgpu 0000:04:00.0: amdgpu: PSP is resuming..."),
            (base + 3000.5, "kernel", "nvme nvme0: 8/0/0 default/read/poll queues")]
    rows += [(base + 3000.6 + i / 10, "kernel", e) for i, e in enumerate(errors)]
    rows += [(base + 3001.0, "kernel", "OOM killer enabled."),
             (base + 3001.1, "kernel", "Restarting tasks ... done."),
             (base + 3001.2, "kernel", "PM: suspend exit")]
    if reprobe:
        rows += [(base + 3001.5, "kernel", "ath11k_pci 0000:03:00.0: BAR 0 [mem 0x80000000-0x801fffff 64bit]: assigned"),
                 (base + 3001.6, "kernel", "ath11k_pci 0000:03:00.0: MSI vectors: 32"),
                 (base + 3001.7, "kernel", "mhi mhi0: Requested to power ON"),
                 (base + 3002.0, "kernel", "ath11k_pci 0000:03:00.0: chip_id 0x12 chip_family 0xb board_id 0xff soc_id 0x400c1211"),
                 (base + 3002.1, "kernel", "ath11k_pci 0000:03:00.0: fw_version 0x110cffff fw_build_timestamp 2024-01-10 "
                                           "fw_build_id QC_IMAGE_VERSION_STRING=WLAN.HSP.1.1-03125")]
    if attempt:
        rows += [(base + 3005.0, _P4_NM, "<info>  [1.0] device (wlan0): Activation: starting connection "
                                         "'Home' (1b2c3d4e-0000-0000-0000-000000000001)")]
    if associate:
        rows += [(base + 3011.0, "kernel", "wlan0: authenticate with xx:xx:xx:xx:1f:e7 (local address=3c:e9:f7:12:34:56)"),
                 (base + 3011.3, "kernel", "wlan0: associated"),
                 (base + 3012.0, _P4_NM, "<info>  [1.0] device (wlan0): state change: secondaries -> activated "
                                         "(reason 'none', sys-iface-state: 'managed')")]
    elif attempt:
        rows += [(base + 3030.0, _P4_NM, "<info>  [1.0] device (wlan0): state change: config -> failed "
                                         "(reason 'supplicant-timeout', sys-iface-state: 'managed')")]
    rows += [(base + 3000 + end, _P4_NM, "<info>  [1.0] manager: NetworkManager state is now DISCONNECTED")]
    return rows

_P4_UP = [(100, "kernel", "wlan0: associated"),
          (101, _P4_NM, "<info>  [1.0] device (wlan0): state change: secondaries -> activated "
                        "(reason 'none', sys-iface-state: 'managed')")]
_P4_LCD_ERRORS = ("rtw_8822ce 0000:01:00.0: failed to power on mac",
                  "rtw_8822ce 0000:01:00.0: failed to leave ips state")

def _p4_unix(rows):
    """journalctl -o short-unix --no-hostname, in time order."""
    return "".join(f"{_P4_U + t:.6f} {who}: {msg}\n" for t, who, msg in sorted(rows, key=lambda r: r[0]))


class _P4Journal:
    """A previous boot's journal that answers journalctl the way it does:
    -k / _TRANSPORT=kernel, -u, _SYSTEMD_UNIT, -t, --grep (a regex on the
    message), --since/--until @unix, -n and -o short-unix/short-monotonic."""
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: r[0])
    def answer(self, cmd):
        def arg(flag):
            return cmd[cmd.index(flag) + 1] if flag in cmd else None
        want = set()
        if "-k" in cmd or "_TRANSPORT=kernel" in cmd:
            want.add("kernel")
        units = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-u"]
        if "NetworkManager" in units or "_SYSTEMD_UNIT=NetworkManager.service" in cmd:
            want.add("NetworkManager")
        tag = arg("-t")
        rows = []
        for t, who, msg in self.rows:
            src = "NetworkManager" if who.startswith("NetworkManager") else who.split("[")[0]
            if tag is not None:
                if src != tag:
                    continue
            elif want and src not in want:
                continue
            rows.append((t, who, msg))
        if arg("--grep") is not None:
            rows = [r for r in rows if re.search(arg("--grep"), r[2])]
        if arg("--since"):
            rows = [r for r in rows if _P4_U + r[0] >= float(arg("--since")[1:])]
        if arg("--until"):
            rows = [r for r in rows if _P4_U + r[0] <= float(arg("--until")[1:])]
        if arg("-n"):
            rows = rows[-int(arg("-n")):]
        if arg("-o") == "short-monotonic":
            return "".join(f"[{t + 5:>12.6f}] {who}: {msg}\n" for t, who, msg in rows)
        return "".join(f"{_P4_U + t:.6f} {who}: {msg}\n" for t, who, msg in rows)


section("D1 the dispatcher writes rtw88's disable_aspm only once iw turned power save off")
try:
    _ps1 = _d_block("# Apply power save", "# Apply buffer tuning")
    ok('PS_OFF=1' in _ps1 and _ps1.index('elif iw dev "$IFACE" set power_save off')
       < _ps1.index("PS_OFF=1") < _ps1.index("echo Y 2>/dev/null"),
       "the dispatcher remembers whether iw itself turned power save off")
    _gate1 = _ps1[_ps1.index("*/rtw88_pci/parameters/disable_aspm)"):_ps1.index("esac")]
    ok('"$PS_OFF" != 1' in _gate1 and "operstate" in _gate1 and _ps1.count("operstate") == 1,
       "and only disable_aspm waits on that and on the association")
    ok(m.ASSOCIATED_ONLY_PARAMS == ("/sys/module/rtw88_pci/parameters/disable_aspm",),
       "which is the plugin's one associated-only parameter")

    def _p1_run(iw_rc, operstate, stuck=0):
        _r = tempfile.mkdtemp()
        _d_netdev(_r, "wlan0", "rtw88_8822ce", operstate=operstate)
        for _pp in ("rtw88_core/parameters/disable_lps_deep", "rtw88_pci/parameters/disable_aspm"):
            os.makedirs(os.path.dirname(os.path.join(_r, "module", _pp)), exist_ok=True)
            open(os.path.join(_r, "module", _pp), "w").write("N\n")
        os.makedirs(os.path.join(_r, "run"))
        _snip = _d_block("# Driver-specific sysfs fixes", "# The first run of a boot") + _ps1
        _out, _log = _d_run(_snip, [f'SYS_NET="{_r}/net"', "IFACE=wlan0", "DRIVER=rtw88", "POWER_SAVE=True",
                                    "OUR_CARD=1", "FIRST_RUN=0", f"NETLINK_STUCK={stuck}", "LOG_TAG=t",
                                    "APPLIED=0"], {"iw": iw_rc, "logger": 0}, _r)
        _v = lambda p: open(os.path.join(_r, "module", p)).read().strip()
        return _v("rtw88_pci/parameters/disable_aspm"), _v("rtw88_core/parameters/disable_lps_deep"), _log
    _a, _l, _lg = _p1_run(1, "up")
    ok(_a == "N" and _l == "Y" and any("power save not confirmed off" in x for x in _lg),
       f"iw failing leaves disable_aspm alone and still writes disable_lps_deep (aspm={_a}, lps={_l})")
    _a, _l, _lg = _p1_run(0, "up", stuck=1)
    ok(_a == "N" and _l == "Y", f"so does iw left out behind a stuck one (aspm={_a}, lps={_l})")
    _a, _l, _lg = _p1_run(0, "down")
    ok(_a == "N" and _l == "Y",
       f"not associated: disable_aspm waits, the others are written as the plugin writes them (aspm={_a}, lps={_l})")
    _a, _l, _lg = _p1_run(0, "up")
    ok(_a == "Y" and _l == "Y", f"power save off and associated: both written (aspm={_a}, lps={_l})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D2 the dispatcher waits for the plugin's apply lock before reading the settings")
try:
    import fcntl as _p2_fcntl, shutil as _p2_sh, threading as _p2_thr
    _r2 = tempfile.mkdtemp(); os.makedirs(os.path.join(_r2, "run"))
    _sf2 = os.path.join(_r2, "settings.json")
    open(_sf2, "w").write(json.dumps({"power_save_disabled": False}))
    _blk2 = _d_block("# One at a time with the plugin", "# Disabled in Decky")
    _stubs2 = {"logger": 0}
    if _p2_sh.which("flock") is None:
        # No flock(1) here (macOS): a stand-in that takes the same kind of
        # lock on the same descriptor, flock(2) with a time limit.
        _bin2 = os.path.join(_r2, "bin"); os.makedirs(_bin2, exist_ok=True)
        open(os.path.join(_bin2, "flock"), "w").write(
            f"#!{sys.executable}\nimport fcntl, sys, time\n"
            "w = float(sys.argv[sys.argv.index('-w') + 1]); fd = int(sys.argv[-1])\n"
            "end = time.monotonic() + w\n"
            "while True:\n"
            "    try:\n        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); sys.exit(0)\n"
            "    except OSError:\n"
            "        if time.monotonic() > end: sys.exit(1)\n        time.sleep(0.05)\n")
        os.chmod(os.path.join(_bin2, "flock"), 0o755)
    _held = open(os.path.join(_r2, "run", "apply.lock"), "a")
    _p2_fcntl.flock(_held.fileno(), _p2_fcntl.LOCK_EX)
    _released = []
    def _p2_release():
        time.sleep(1.0); _released.append(time.monotonic())
        _p2_fcntl.flock(_held.fileno(), _p2_fcntl.LOCK_UN); _held.close()
    _t2 = _p2_thr.Thread(target=_p2_release); _t2.start()
    _o2, _l2 = _d_run(_blk2 + '\necho "READ_AT=$(python3 -c "import time; print(time.monotonic())") PS=$POWER_SAVE"\n',
                      [f"SETTINGS_FILE={_sf2}", "LOG_TAG=t"], _stubs2, _r2)
    _t2.join()
    _at = re.search(r"READ_AT=([\d.]+) PS=(\w+)", _o2)
    ok(_at is not None and _released and float(_at.group(1)) >= _released[0] and _at.group(2) == "False",
       f"held by the plugin, the settings are read only after it lets go ({_o2.strip()[:120]})")
    ok(not any("going ahead" in x for x in _l2), "and a short wait is not logged as a timeout")
    _t2t = _d_template()
    ok(_t2t.index("# Self-removal") < _t2t.index("# One at a time with the plugin")
       < _t2t.index("SETTINGS_RAW=$(python3") and _t2t.index("command -v python3")
       < _t2t.index("# One at a time with the plugin")
       and "exec 8>>/run/wifi-optimizer/apply.lock" in _t2t and "flock -w 10 8 ||" in _t2t,
       "the lock is fd 8 on /run/wifi-optimizer/apply.lock, after the clean-up and python3 checks, before the settings read")
    _nl2 = [l for l in _t2t.splitlines() if re.search(r'\b(iw dev "\$IFACE"|ip link set|tc qdisc)', l)]
    ok(len(_nl2) == 4 and all("8>&-" in l for l in _nl2),
       "and iw, ip and tc run without it, so one stuck in the driver cannot hold it for good")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D3 a USB radio of the same family is not the card the fixes are for")
try:
    _r3 = tempfile.mkdtemp()
    _d_netdev(_r3, "wlan0", "rtw88_8822ce", operstate="down")
    _d_netdev(_r3, "wlan1", "rtw88_8821cu", bus="usb")
    class Dongle(m.Plugin):
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/nmcli":
                return _ok("wlan1:wifi\nwlan0:wifi\n")
            return _ok()
    _sn3 = m.SYSFS_NET
    try:
        m.SYSFS_NET = os.path.join(_r3, "net")
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88"})
        ok(Dongle()._driver_interface() == "wlan0" and Dongle()._managed_interface() == "wlan0",
           "the managed card is the rtw88 one on PCI, not the rtw88 USB adapter (contract C2)")
        ok(Dongle()._wifi_associated() is False, "and its association is the one read")
        ok(Dongle()._detect_wifi_driver() == "rtw88", "the driver is read from the card on PCI")
        _r3b = tempfile.mkdtemp()
        _d_netdev(_r3b, "wlan0", "rtw88_8822ce")
        _d_netdev(_r3b, "wlan1", "mt7921u", bus="usb")
        m.SYSFS_NET = os.path.join(_r3b, "net")
        ok(Dongle()._detect_wifi_driver() == "rtw88",
           "and a USB adapter of another family NetworkManager lists first does not replace it")
        _r3c = tempfile.mkdtemp()
        _d_netdev(_r3c, "wlan1", "mt7921u", bus="usb")
        m.SYSFS_NET = os.path.join(_r3c, "net")
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "unknown"})
        class OnlyDongle(m.Plugin):
            def _get_wifi_interface(self): return "wlan1"
        OnlyDongle()._apply_pcie_aspm_fix(True)
        ok(open(os.path.join(_r3c, "devices", "wlan1", "power", "control")).read().strip() == "auto"
           and open(os.path.join(_r3c, "devices", "wlan1", "link", "l1_aspm")).read().strip() == "1",
           "no PCIe write goes to a USB device, whatever the driver")
    finally:
        m.SYSFS_NET = _sn3
        m._save_settings(dict(m.DEFAULT_SETTINGS))
        if os.path.exists(m.PCIE_BASELINE_FILE): os.remove(m.PCIE_BASELINE_FILE)

    # The dispatcher run for the adapter.
    for _pp in ("rtw88_core/parameters/disable_lps_deep", "rtw88_pci/parameters/disable_aspm"):
        os.makedirs(os.path.dirname(os.path.join(_r3, "module", _pp)), exist_ok=True)
        open(os.path.join(_r3, "module", _pp), "w").write("N\n")
    os.makedirs(os.path.join(_r3, "run"), exist_ok=True)
    _snip3 = _d_block("iface_module() {", "# Apply buffer tuning").replace('"/run/wifi-optimizer"', f'"{_r3}/run"')
    _env3 = [f'SYS_NET="{_r3}/net"', "IFACE=wlan1", "DRIVER=rtw88", "POWER_SAVE=True", "STREAM_MODE=False",
             "LOG_TAG=t", "APPLIED=0"]
    _o3, _l3 = _d_run(_snip3, _env3, {"iw": 0, "logger": 0, "pgrep": 1}, _r3)
    ok("OUR_CARD=0" in _o3
       and open(os.path.join(_r3, "module", "rtw88_pci/parameters/disable_aspm")).read().strip() == "N"
       and not os.path.exists(os.path.join(_r3, "run", "pcie-baseline"))
       and open(os.path.join(_r3, "devices", "wlan1", "power", "control")).read().strip() == "auto",
       f"the dispatcher writes no rtw88_pci parameter, no PCIe file and no baseline for it ({_o3.strip()})")

    # Interface recovery after a backend restart.
    _r3d = tempfile.mkdtemp()
    _d_netdev(_r3d, "wlan0", "rtw88_8821cu", bus="usb")
    _ieee = os.path.join(_r3d, "ieee80211")
    os.makedirs(os.path.join(_r3d, "bus", "pci"), exist_ok=True)
    for _phy, _dev in (("phy0", "pcicard"), ("phy1", "wlan0")):
        os.makedirs(os.path.join(_ieee, _phy))
        if _phy == "phy0":
            _pd = os.path.join(_r3d, "devices", "pcicard"); os.makedirs(os.path.join(_pd, "driver"))
            os.makedirs(os.path.join(_r3d, "modules", "rtw88_8822ce"), exist_ok=True)
            os.symlink(os.path.join(_r3d, "modules", "rtw88_8822ce"), os.path.join(_pd, "driver", "module"))
            os.symlink(os.path.join(_r3d, "bus", "pci"), os.path.join(_pd, "subsystem"))
        else:
            _pd = os.path.join(_r3d, "devices", "wlan0")
            os.makedirs(os.path.join(_pd, "net", "wlan0"), exist_ok=True)
        os.symlink(_pd, os.path.join(_ieee, _phy, "device"))
    _sn3, _si3 = m.SYSFS_NET, m.SYSFS_IEEE80211
    try:
        m.SYSFS_NET, m.SYSFS_IEEE80211 = os.path.join(_r3d, "net"), _ieee
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88"})
        class Recover(m.Plugin):
            def __init__(self): super().__init__(); self.cmds = []
            def _run_cmd(self, cmd, timeout=5):
                self.cmds.append(cmd)
                return _ok("wlan0:wifi\n") if cmd[0] == "/usr/bin/nmcli" else _ok()
        _rc = Recover()
        ok(_rc._interface_back() is None,
           "with the internal card's netdev gone, the adapter's is not taken as the card back")
        _rc._recover_wlan0()
        _add = [c for c in _rc.cmds if "interface" in c and "add" in c]
        ok(_add and _add[0][2] == "phy0" and _add[0][5] == "wlan1",
           f"recovery adds the card's interface on its own phy, under the first free name ({_add[:1]})")
    finally:
        m.SYSFS_NET, m.SYSFS_IEEE80211 = _sn3, _si3
        m._save_settings(dict(m.DEFAULT_SETTINGS))
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D4 the driver parameters at load reach the journal on every boot")
try:
    import builtins as _p4_bi
    _r4 = tempfile.mkdtemp()
    _p4_real_open = _p4_bi.open
    def _p4_sys(path): return os.path.join(_r4, "module", path[len("/sys/module/"):])
    def _p4_open(path, *a, **k):
        if isinstance(path, str) and path.startswith("/sys/module/"):
            path = _p4_sys(path)
        return _p4_real_open(path, *a, **k)
    for _pp in m.DRIVER_PROFILES["rtw88"]["sysfs_power_fixes"]:
        os.makedirs(os.path.dirname(_p4_sys(_pp)), exist_ok=True)
        _p4_real_open(_p4_sys(_pp), "w").write("N\n")
    _r4net = tempfile.mkdtemp(); _d_netdev(_r4net, "wlan0", "rtw88_8822ce")
    _p4_journal = []   # (tag, message) as logger sends them to the journal
    class Logs(m.Plugin):
        """logger and journalctl answer from one shared journal, so what the
        plugin logs is what the diagnostics read back."""
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/logger":
                tag = cmd[cmd.index("-t") + 1]
                msg = cmd[cmd.index("--") + 1] if "--" in cmd else cmd[-1]
                _p4_journal.append((tag, msg)); return _ok()
            if cmd[0] == "/usr/bin/journalctl" and "-t" in cmd:
                tag = cmd[cmd.index("-t") + 1]
                lines = [f"[{3 + i:>12.6f}] {t}[812]: {msg}" for i, (t, msg) in enumerate(_p4_journal) if t == tag]
                if "--grep" in cmd:
                    lines = [l for l in lines if re.search(cmd[cmd.index("--grep") + 1], l)]
                return _ok("\n".join(lines) + "\n")
            return _ok()
    _s4 = (m.AT_LOAD_MARKER if hasattr(m, "AT_LOAD_MARKER") else None, m.ENFORCED_FILE, m.SYSFS_NET,
           m.PCIE_BASELINE_FILE)
    _run4 = tempfile.mkdtemp()
    m.AT_LOAD_MARKER = os.path.join(_run4, "at-load-logged")
    m.ENFORCED_FILE = os.path.join(_run4, "last_enforced")
    m.PCIE_BASELINE_FILE = os.path.join(_run4, "pcie-baseline")
    m.SYSFS_NET = os.path.join(_r4net, "net")
    _p4_bi.open = _p4_open
    try:
        m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw88"})
        Logs()._apply_driver_fixes(True, persist=False)
        Logs()._apply_driver_fixes(True, persist=False)
    finally:
        _p4_bi.open = _p4_real_open
    _at4 = [msg for t, msg in _p4_journal if "at load" in msg]
    ok(_at4 == ["driver parameters at load: disable_lps_deep=N disable_aspm=N"]
       and all(t == "wifi-optimizer" for t, _msg in _p4_journal),
       f"the plugin writing a parameter first logs the values before its write, once a boot, "
       f"under the tag the diagnostics read ({_p4_journal})")
    _d4 = m.Plugin._diagnostic_extras(Logs(), "rtw88", "wlan0")
    ok(any("driver parameters at load: disable_lps_deep=N disable_aspm=N" in l
           for l in _d4.get("plugin_load_values_this_boot", [])),
       f"and Copy diagnostics shows it ({_d4.get('plugin_load_values_this_boot')})")
    _p4_journal.clear(); os.remove(m.AT_LOAD_MARKER)
    open(m.ENFORCED_FILE, "w").write("1\n")
    _p4_bi.open = _p4_open
    try:
        Logs()._apply_driver_fixes(True, persist=False)
    finally:
        _p4_bi.open = _p4_real_open
    ok(not _p4_journal, "an earlier version's last_enforced with no marker: the values are ours, not at load")
    (m.AT_LOAD_MARKER, m.ENFORCED_FILE, m.SYSFS_NET, m.PCIE_BASELINE_FILE) = _s4
    m._save_settings(dict(m.DEFAULT_SETTINGS))

    # The dispatcher: auto mode with no stream, and a second radio first.
    def _p4_disp(root, iface, stream=False):
        _snip = (_d_block("iface_module() {", "# Apply buffer tuning")
                 + _d_block("# Record enforcement", "\nexit 0\n")).replace('"/run/wifi-optimizer"', f'"{root}/run"')
        return _d_run(_snip, [f'SYS_NET="{root}/net"', f"IFACE={iface}", "DRIVER=rtw88", "POWER_SAVE=True",
                              f"STREAM_MODE={'True' if stream else 'False'}", "LOG_TAG=t", "APPLIED=0"],
                      {"iw": 0, "logger": 0, "pgrep": 1}, root)
    def _p4_root():
        _r = tempfile.mkdtemp(); os.makedirs(os.path.join(_r, "run"))
        _d_netdev(_r, "wlan0", "rtw88_8822ce")
        _d_netdev(_r, "wlan1", "rtw88_8821cu", bus="usb")
        for _pp in ("rtw88_core/parameters/disable_lps_deep", "rtw88_pci/parameters/disable_aspm"):
            os.makedirs(os.path.dirname(os.path.join(_r, "module", _pp)), exist_ok=True)
            open(os.path.join(_r, "module", _pp), "w").write("N\n")
        return _r
    _ra = _p4_root()
    _o4, _l4 = _p4_disp(_ra, "wlan0", stream=True)
    ok(any("driver parameters at load: disable_lps_deep=N disable_aspm=N" in l for l in _l4)
       and any("no stream running" in l for l in _l4)
       and open(os.path.join(_ra, "module", "rtw88_pci/parameters/disable_aspm")).read().strip() == "N",
       f"auto mode with no stream logs the values at load and still leaves the fixes off ({_l4})")
    _rb = _p4_root()
    _p4_disp(_rb, "wlan1")
    ok(not os.path.exists(os.path.join(_rb, "run", "last_enforced"))
       and not os.path.exists(os.path.join(_rb, "run", "at-load-logged")),
       "a second radio's connect stamps nothing and claims nothing")
    _o4b, _l4b = _p4_disp(_rb, "wlan0")
    _base4 = os.path.join(_rb, "run", "pcie-baseline")
    ok(any("driver parameters at load:" in l for l in _l4b) and os.path.exists(_base4),
       f"so the card's first run after it still logs the values at load and records the baseline ({_l4b})")
    _o4c, _l4c = _p4_disp(_rb, "wlan0")
    _l4c = _l4c[len(_l4b):]   # the call log is kept across runs in one root
    ok(any("before applying" in l for l in _l4c) and not any("at load" in l for l in _l4c),
       "and later runs log 'before applying'")
    _rc4 = _p4_root()
    open(os.path.join(_rc4, "run", "last_enforced"), "w").write("1\n")
    _o4d, _l4d = _p4_disp(_rc4, "wlan0")
    ok(not os.path.exists(os.path.join(_rc4, "run", "pcie-baseline"))
       and not any("at load" in l for l in _l4d),
       "a last_enforced from a version before the marker, with no marker: no baseline and no 'at load' (D9 kept)")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    try:
        _p4_bi.open = _p4_real_open
    except NameError:
        pass

section("D5 a previous boot flooded by driver errors still shows its sleep and wake")
try:
    _rows5 = (_P4_UP + _p4_cycle(1000, errors=_P4_LCD_ERRORS, end=40000)
              + [(4100 + i * 2.5, "kernel", "rtw_8822ce 0000:01:00.0: failed to send h2c command")
                 for i in range(12000)])
    _j5 = _P4Journal(_rows5)
    class Flooded(m.Plugin):
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/journalctl":
                return _ok(_j5.answer(cmd))
            return _ok()
    _d5 = m.Plugin._diagnostic_extras(Flooded(), "rtw88", "wlan0")
    _k5 = _d5.get("kernel_log_previous_boot", [])
    ok(any("PM: suspend entry" in l for l in _k5) and any("failed to power on mac" in l for l in _k5)
       and any("PM: suspend exit" in l for l in _k5),
       f"the report's previous-boot kernel log has the sleep, the first errors and the wake ({len(_k5)} lines)")
    ok(any("failed to send h2c" in l for l in _k5[-3:]) and len(_k5) <= m.Plugin._DIAG_KERNEL_LINES + 2,
       "and still the end of the boot, within the line budget")
    _n5 = " ".join(_d5.get("network_log_previous_boot", []))
    ok("Activation" in _n5, "the network log keeps the attempt after the wake too")
    ok(Flooded()._read_wake_failure() == _P4_U + 4001,
       "the wake notice finds the failed wake in the flooded boot")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D6 the wake notice counts the driver's errors from the sleep, and judges the first wake")
try:
    _w6 = m._wake_failure_at
    _a6 = _P4_UP + _p4_cycle(1000, reprobe=True, associate=True)
    ok(_w6(_p4_unix(_a6)) == 0,
       "the measured OLED cycle (mmc error at entry, ath11k probed afresh, associated) is a good wake")
    _b6 = _P4_UP + _p4_cycle(1000, errors=_P4_LCD_ERRORS)
    ok(_w6(_p4_unix(_b6)) == _P4_U + 4001,
       "rtw_8822ce errors inside the device resume block, before 'PM: suspend exit', are a failed wake")
    _c6 = _b6 + [(4700, "kernel", "PM: suspend entry (deep)"),
                 (4900, "kernel", "ACPI: PM: Waking up from system sleep state S3"),
                 (4901, "kernel", "PM: suspend exit"),
                 (4910, _P4_NM, "<info>  [1.0] device (wlan0): Activation: starting connection 'Home' (1b2c)"),
                 (5600, _P4_NM, "<info>  [1.0] manager: NetworkManager state is now DISCONNECTED")]
    ok(_w6(_p4_unix(_c6)) == _P4_U + 4001,
       "a retry sleep with no new errors does not hide it: the first wake after WiFi was last up is judged")
    _d6 = _P4_UP + [r for r in _p4_cycle(1000, attempt=False)] + [
        (4001.4, "kernel", "rtw_8822ce 0000:01:00.0: failed to get tx report from firmware"),
        (4002.0, _P4_NM, "<info>  [1.0] device (wlan0): state change: unavailable -> disconnected "
                         "(reason 'supplicant-available', sys-iface-state: 'managed')")]
    ok(_w6(_p4_unix(_d6)) == 0, "away from every saved network (nothing tried) is no notice")
    _e6 = _b6 + [(4040.0, _P4_NM, "<info>  [1.0] audit: op=\"radio-control\" arg=\"wireless-enabled:off\" result=\"success\"")]
    _e6b = _b6 + [(4040.0, _P4_NM, "<info>  [1.0] manager: rfkill: Wi-Fi now disabled by radio killswitch")]
    ok(_w6(_p4_unix(_e6)) == 0 and _w6(_p4_unix(_e6b)) == 0,
       "nor is WiFi switched off after the wake, on purpose or by a radio switch, and left off")
    _f6 = _b6 + [(4100, _P4_NM, "<info>  [1.0] audit: op=\"radio-control\" arg=\"wireless-enabled:off\""),
                 (4110, _P4_NM, "<info>  [1.0] audit: op=\"radio-control\" arg=\"wireless-enabled:on\""),
                 (4111, _P4_NM, "<info>  [1.0] device (wlan0): Activation: starting connection 'Home' (1b2c)")]
    ok(_w6(_p4_unix(_f6)) == _P4_U + 4001, "while WiFi turned off and on to try to fix it still counts")
    _g6 = _P4_UP + [r for r in _p4_cycle(1000, errors=_P4_LCD_ERRORS) if "suspend entry" not in r[2]]
    ok(_w6(_p4_unix(_g6)) == _P4_U + 4001,
       "with the entry line missing, errors are counted from 'ACPI: PM: Waking up'")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D7 normal transitions are not shown as a dead card")
try:
    _mono7 = [1000.0]
    _real_mono7 = m.time.monotonic
    class States(m.Plugin):
        def __init__(self, iface="wlan0", nm="unavailable", state="disconnected", stuck=None):
            super().__init__(); self.iface, self.nm, self.state = iface, nm, state
            if stuck is not None:
                self._stuck_tools = lambda: set(stuck)
        def _nm_general_state(self): return (self.state, "enabled")
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/nmcli" and "DEVICE,TYPE,STATE" in cmd:
                return _ok(f"wlan0:wifi:{self.nm}\n")
            return _ok()
    m.time.monotonic = lambda: _mono7[0]
    try:
        _s7 = States()
        _first = _s7._wifi_state("wlan0")
        _mono7[0] += 10; _mid = _s7._wifi_state("wlan0")
        _mono7[0] += 15; _late = _s7._wifi_state("wlan0")
        ok(_first == "disconnected" and _mid == "disconnected" and _late == "unavailable",
           f"unavailable reads disconnected until it has held 20 s, then unavailable ({_first}, {_mid}, {_late})")
        _s7b = States(iface=None)
        _x = _s7b._wifi_state(None); _mono7[0] += 25; _y = _s7b._wifi_state(None)
        ok(_x == "disconnected" and _y == "no_device", f"so does no_device ({_x}, {_y})")
        _s7c = States(); _s7c._wifi_state("wlan0"); _mono7[0] += 25
        _s7c._backend_switch = {"in_progress": True}
        _sw = _s7c._wifi_state("wlan0")
        _s7c._backend_switch = {"in_progress": False}
        _after = _s7c._wifi_state("wlan0")
        ok(_sw == "disconnected" and _after == "disconnected",
           f"during a backend switch it stays disconnected, and the clock starts again after ({_sw}, {_after})")
        _s7d = States(); _s7d._wifi_state("wlan0"); _mono7[0] += 25
        _s7d.nm = "disconnected"; _s7d._wifi_state("wlan0"); _s7d.nm = "unavailable"
        ok(_s7d._wifi_state("wlan0") == "disconnected", "a state that went away starts its clock again")
        ok(States(stuck={"iw"})._wifi_state("wlan0") == "not_responding",
           "a stuck iw reads not_responding at once")
    finally:
        m.time.monotonic = _real_mono7
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    m.time.monotonic = _real_mono7

section("D8 NetworkManager left asleep is said, and the diagnostics carry its state")
try:
    _real_mono8 = m.time.monotonic
    _mono8 = [5000.0]
    class Asleep(m.Plugin):
        def _nm_general_state(self): return ("asleep", "enabled")
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/nmcli" and "STATE,WIFI" in cmd:
                return _ok("asleep:enabled\n")
            if cmd[0] == "/usr/bin/nmcli" and "DEVICE,TYPE,STATE" in cmd:
                return _ok("wlan0:wifi:unmanaged\n")
            return _ok()
    m.time.monotonic = lambda: _mono8[0]
    try:
        _a8 = Asleep(); _a8._wifi_state("wlan0"); _mono8[0] += 25
        _v8 = _a8._wifi_state("wlan0")
    finally:
        m.time.monotonic = _real_mono8
    ok(_v8 == "asleep", f"NetworkManager asleep reads asleep (contract C1) ({_v8})")
    _x8 = m.Plugin._diagnostic_extras(type("A8", (m.Plugin,), {
        "_get_current_backend": lambda self: "wpa_supplicant",
        "_run_cmd": lambda self, cmd, timeout=5: _ok("asleep:enabled\n") if "STATE,WIFI" in cmd else _ok()})(),
        "rtw88", "wlan0")
    ok(_x8.get("nm_general_state") == {"state": "asleep", "wifi_radio": "enabled"},
       f"and the diagnostics carry NetworkManager's general state ({_x8.get('nm_general_state')})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D9 iwd's unquoted network name leaves nothing behind in the diagnostics")
try:
    _l9 = m.Plugin._diag_service_lines(
        "[  9.200000] iwd[600]: event: connect-info, ssid: Marriott Bonvoy Guest, bss: <mac>, signal: -52, load: 3/255\n"
        "[  9.300000] iwd[600]: event: connect-info, ssid: Jason's iPhone, bss: <mac>, signal: -60, load: 0/255\n"
        "[  9.400000] iwd[600]: event: connect-info, ssid: Corner Cafe Free WiFi\n", 40, ())
    _f9 = " ".join(_l9)
    ok(not any(w in _f9 for w in ("Marriott", "Bonvoy", "Guest", "Jason", "iPhone", "Corner", "Cafe")),
       f"no word of a multi-word name survives, quote inside it or not ({_l9})")
    ok("signal: -52" in _f9 and "load: 3/255" in _f9, "while the fields after it stay")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D10 a profile's file name does not carry the network's name into the diagnostics")
try:
    _l10 = m.Plugin._diag_service_lines(
        "[ 5.0] NetworkManager[700]: <info>  keyfile: delete /etc/NetworkManager/system-connections/Marriott_Guest.nmconnection\n"
        "[ 5.1] iwd[600]: Loading /var/lib/iwd/Marriott Guest.psk\n", 40, ())
    ok(not any("Marriott" in l for l in _l10) and "system-connections/<name>.nmconnection" in _l10[0]
       and "/var/lib/iwd/<name>.psk" in _l10[1], f"NetworkManager's and iwd's file names are replaced ({_l10})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D11 the device list keeps only the kinds of device that bear on WiFi")
try:
    class Devs11(m.Plugin):
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/nmcli" and "DEVICE,TYPE,STATE" in cmd:
                return _ok("wlan0:wifi:connected\np2p-dev-wlan0:wifi-p2p:disconnected\nlo:loopback:unmanaged\n"
                           "enp1s0:ethernet:unavailable\nJasonsHomeVPN:wireguard:connected\n"
                           "AA:BB:CC:DD:EE:F1:bluetooth:disconnected\n")
            return _ok()
    _n11 = m.Plugin._diagnostic_extras(Devs11(), "rtw88", "wlan0").get("nm_devices", "")
    ok("wlan0:wifi:connected" in _n11 and "lo:loopback" in _n11 and "enp1s0:ethernet" in _n11
       and "p2p-dev-wlan0:wifi-p2p" in _n11 and "JasonsHomeVPN" not in _n11 and "bluetooth" not in _n11,
       f"a WireGuard device named after its config is dropped ({_n11!r})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D12 the 6 GHz answer the card gave is kept when it cannot be asked")
try:
    class NoIw(m.Plugin):
        def _detect_device_family(self): return ("Legion Go", "legion_go", "Legion Go")
        def _detect_wifi_driver(self): return "mt7921e"
        def _radio_has_6ghz(self): return None
    m._save_settings({**m.DEFAULT_SETTINGS, "driver": "mt7921e", "supports_6ghz": True})
    ok(asyncio.run(NoIw().get_device_info()).get("supports_6ghz") is True,
       "iw stuck at load keeps the 6 GHz the card listed before (an MT7922)")
    m._save_settings({**m.DEFAULT_SETTINGS, "driver": "rtw89", "supports_6ghz": True})
    ok(asyncio.run(NoIw().get_device_info()).get("supports_6ghz") is m.DRIVER_PROFILES["mt7921e"]["supports_6ghz"],
       "a value saved for another driver is not used: the profile decides")
    m._save_settings(dict(m.DEFAULT_SETTINGS))
    ok(asyncio.run(NoIw().get_device_info()).get("supports_6ghz") is False, "and with none saved, so does it")
    ok(m.DRIVER_PROFILES["ath11k_pci"]["supports_6ghz"] is False,
       "ath11k_pci also drives the QCA6390, so its fallback is False like the others")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D13 a card that keeps the region it was built for is said, in words")
try:
    _OWN = ("global\ncountry DE: DFS-ETSI\n\t(2400 - 2483 @ 40), (N/A, 20), (N/A)\n\n"
            "phy#0\ncountry US: DFS-FCC\n\t(2400 - 2472 @ 40), (N/A, 30), (N/A)\n")
    ok(m.Plugin._parse_reg(_OWN).get("governing") == "US",
       "a phy with a country of its own, not self-managed, is what governs it")
    m._save_settings(dict(base_ap))
    asyncio.sleep = _no_sleep
    try:
        _m13 = asyncio.run(Reg(_OWN).set_regdomain("DE")).get("message", "")
        _MIX = _OWN.replace("phy#0\ncountry US", "phy#0\ncountry 98")
        m._save_settings(dict(base_ap))
        _m13b = asyncio.run(Reg(_MIX).set_regdomain("DE")).get("message", "")
    finally:
        asyncio.sleep = _d_real_sleep
    ok("US" in _m13 and "DE" in _m13, f"setting DE says the card stayed on US ({_m13!r})")
    ok(_m13b and "(98)" not in _m13b and "mix of its own region and DE" in _m13b,
       f"and cfg80211's 98 is said as a mix, not as a code ({_m13b!r})")
    ok(m.Plugin._region_words("00") == "the world default region" and m.Plugin._region_words("99") == "its own region",
       "00 and 99 are words too")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
    asyncio.sleep = _d_real_sleep

section("D14 a report send is classified by how curl ended, and worded to match")
try:
    m.REPORT_URL = _REP_URL
    _rep_reset()
    class Curl14(RepStub):
        def __init__(self, rc, out="", nm="connected:enabled"):
            super().__init__(nm=nm); self.rc, self.out = rc, out
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0] == "/usr/bin/curl":
                self.calls.append((list(cmd), timeout))
                return {"success": self.rc == 0, "stdout": self.out, "stderr": "curl: failed",
                        "returncode": self.rc}
            return RepStub._run_cmd(self, cmd, timeout)
    def _k14(rc, out="", nm="connected:enabled"):
        _rep_reset(); os.makedirs(_rep_dir, mode=0o700, exist_ok=True)
        open(_pending_path(), "w").write('{"plugin_version":"0.24.5-beta","report":{"a":1}}')
        return Curl14(rc, out, nm)._post_report(_pending_path())["kind"]
    _cases14 = [(_k14(6), "unreachable"), (_k14(7, nm="disconnected:enabled"), "offline"),
                (_k14(60), "unexpected"), (_k14(63, "\n503"), "unavailable"),
                (_k14(0, '{"ok":true,"ref":"WO-7K2M9Q"}\n200'), "sent"),
                (_k14(0, '{"ok":true,"ref":"something-new"}\n200'), "sent")]
    ok(all(a == b for a, b in _cases14),
       f"DNS failure with WiFi up / no connection / TLS / an oversized 503 / 200 ok: {_cases14}")
    _rep_reset()
    _p14 = Curl14(6)
    _pv14 = asyncio.run(_p14.preview_report())
    _sr14 = asyncio.run(_p14.send_report(_pv14["id"]))
    ok(_sr14.get("status") == "queued" and "No connection" not in _sr14.get("message", "")
       and "Couldn't reach the report service" in _sr14.get("message", ""),
       f"no message says 'No connection' while NetworkManager is connected ({_sr14.get('message')!r})")
    _wait14 = _p14._report_retry_at - time.monotonic()
    ok(abs(_wait14 - m.REPORT_RETRY_AFTER["unreachable"]) < 5 and m.REPORT_RETRY_AFTER["unreachable"] == 900,
       "and it is tried again in 15 minutes")
    _rep_reset()
    _p14b = Curl14(60)
    _sr14b = asyncio.run(_p14b.send_report(asyncio.run(_p14b.preview_report())["id"]))
    ok(_sr14b.get("message") == "Couldn't send the report. It is saved and will be tried again later."
       and m.REPORT_RETRY_AFTER["unexpected"] == 3600,
       f"a TLS failure keeps it, says so in its own words, and waits an hour ({_sr14b.get('message')!r})")
    _rep_reset()
    _p14c = Curl14(0, '{"ok":true,"ref":"something-new"}\n200')
    _sr14c = asyncio.run(_p14c.send_report(asyncio.run(_p14c.preview_report())["id"]))
    ok(_sr14c.get("status") == "sent" and _sr14c.get("message") == "Report sent." and _sr14c.get("ref") == ""
       and not os.path.exists(_pending_path()),
       f"a 200 ok with a reference this version does not know is sent, with no reference shown (contract C6) ({_sr14c})")
    _rep_reset()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D15 a report carries its id, so the receiver takes a repeat once")
try:
    m.REPORT_URL = _REP_URL
    _rep_reset()
    _p15 = RepStub(curl=[("", 7)], nm="disconnected:enabled")
    _pv15 = asyncio.run(_p15.preview_report())
    asyncio.run(_p15.send_report(_pv15["id"]))
    _c15 = _p15.curl_calls()[0]
    _bg15 = RepStub(curl=[(json.dumps({"ok": True, "ref": "WO-7K2M9Q"}), "200")])
    asyncio.run(_bg15._send_pending_report_if_due())
    _c15b = _bg15.curl_calls()[0] if _bg15.curl_calls() else []
    _key = f"Idempotency-Key: {_pv15['id']}"
    ok(_c15[_c15.index(_key) - 1] == "-H" and _key in _c15b
       and _pv15["id"] == m.hashlib.sha256(_p15.bodies[0].encode()).hexdigest(),
       "the curl call carries Idempotency-Key: the body's sha256, the preview's id, on the retry too")
    _rep_reset()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D16 a waiting report can be withdrawn, is said when dropped, and goes when the feature is off")
try:
    m.REPORT_URL = _REP_URL
    _rep_reset()
    _p16 = RepStub(curl=[("", 7)], nm="disconnected:enabled")
    asyncio.run(_p16.send_report(asyncio.run(_p16.preview_report())["id"]))
    ok(os.path.exists(_pending_path()) and asyncio.run(_p16.cancel_pending_report()) == {"success": True}
       and not os.path.exists(_pending_path()) and asyncio.run(_p16.get_report_state())["pending"] is False,
       "cancel_pending_report removes the waiting report")
    os.makedirs(_rep_dir, mode=0o700, exist_ok=True)
    open(_pending_path(), "w").write('{"plugin_version":"0.24.5-beta","report":{"a":1}}')
    _bg16 = RepStub(curl=[('{"ok":false}', "400")])
    asyncio.run(_bg16._send_pending_report_if_due())
    _st16 = asyncio.run(RepStub().get_report_state())
    ok(_st16.get("dropped") is True and not os.path.exists(_pending_path()),
       "a waiting report the receiver refuses sets dropped")
    ok(asyncio.run(RepStub().dismiss_report_notice("dropped")).get("success") is True
       and asyncio.run(RepStub().get_report_state()).get("dropped") is False,
       "until the panel dismisses it")
    m.REPORT_URL = ""
    open(_pending_path(), "w").write('{"plugin_version":"0.24.5-beta","report":{"a":1}}')
    _old16 = time.time() - 15 * 24 * 3600
    os.utime(_pending_path(), (_old16, _old16))
    _q16 = RepStub()
    asyncio.run(_q16._send_pending_report_if_due())
    ok(not os.path.exists(_pending_path()) and not _q16.calls,
       "with REPORT_URL empty a 15-day-old waiting report is dropped by the background watch, running no command")
    open(_pending_path(), "w").write('{"plugin_version":"0.24.5-beta","report":{"a":1}}')
    os.utime(_pending_path(), (_old16, _old16))
    asyncio.run(RepStub().get_report_state())
    ok(not os.path.exists(_pending_path()), "and by get_report_state")
    m.REPORT_URL = _REP_URL
    _gone16 = _d_template()[_d_template().index('if [ ! -d "$PLUGIN_DIR" ]'):]
    _gone16 = _gone16[:_gone16.index("\nfi\n")]
    ok("rm -rf /var/lib/wifi-optimizer" in _gone16, "the dispatcher's own clean-up removes the report directory")
    os.makedirs(_rep_dir, mode=0o700, exist_ok=True)
    open(os.path.join(_rep_dir, m.REPORT_DROPPED_NAME), "w").write("x")
    m._forget_reports()
    ok(not os.path.exists(_rep_dir), "and so does uninstalling, the dropped note included")
    _rep_reset()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D17 a report sent or kept retires the wake notice")
try:
    m.REPORT_URL = _REP_URL
    _rep_reset()
    _wj17 = _p4_unix(_P4_UP + _p4_cycle(1000, errors=_P4_LCD_ERRORS))
    _p17 = RepStub(curl=[("", 7)], nm="disconnected:enabled", journal=_wj17)
    ok(asyncio.run(_p17.get_report_state()).get("wake_failure") is True, "a failed wake shows the notice")
    _sr17 = asyncio.run(_p17.send_report(asyncio.run(_p17.preview_report())["id"]))
    ok(_sr17.get("status") == "queued" and asyncio.run(_p17.get_report_state()).get("wake_failure") is False
       and asyncio.run(RepStub(journal=_wj17).get_report_state()).get("wake_failure") is False,
       "a queued send retires it, across a reload too")
    _rep_reset()
    _p17b = RepStub(curl=[(json.dumps({"ok": True, "ref": "WO-7K2M9Q"}), "200")], journal=_wj17)
    asyncio.run(_p17b.send_report(asyncio.run(_p17b.preview_report())["id"]))
    ok(asyncio.run(RepStub(journal=_wj17).get_report_state()).get("wake_failure") is False,
       "and so does a sent one")
    _rep_reset()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D18 a report too large keeps the previous boot's kernel log longest")
try:
    _big18 = dict(_REP_DIAG)
    _big18["kernel_log_previous_boot"] = ["[ 1.0] rtw88_8822ce: " + "x" * 280] * 200
    _big18["kernel_log_this_boot"] = ["[ 1.0] rtw88_8822ce: " + "y" * 280] * 100
    _big18["plugin_log_this_boot"] = ["[ 1.0] wifi-optimizer: " + "z" * 280] * 60
    _big18["network_log_previous_boot"] = ["NetworkManager: line"] * 10
    _made18 = m._report_payload(_big18, "1.0.0")
    _r18 = _made18[0]["report"] if _made18 else {}
    ok("kernel_log_previous_boot" in _r18 and "kernel_log_this_boot" not in _r18
       and "plugin_log_this_boot" not in _r18,
       f"this boot's logs go first while the previous boot's kernel log, the biggest, stays ({_r18.get('left_out')})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D19a backend switch failures say what to do, with no 'please' and no 'reboot'")
try:
    _p19 = m.Plugin()
    _o19 = [_p19._friendly_backend_error(d) for d in (
        "symbol lookup error: x", "Permission denied", "bash: x: command not found", "timed out",
        "Network is unreachable", "anything else")]
    ok(all("please" not in o.lower() and "reboot" not in o.lower() and "WiFi" in o for o in _o19),
       f"every outcome names WiFi and none says please or reboot ({_o19})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D19b diagnostics read the PCIe link registers of the WiFi card and its port")
try:
    _LSPCI = {
        "0000:01:00.0": ("01:00.0 Network controller: Realtek Semiconductor Co., Ltd. RTL8822CE 802.11ac PCIe Wireless Network Adapter\n"
                         "\tSubsystem: AzureWave Device 3750\n"
                         "\tCapabilities: [70] Express (v2) Endpoint, MSI 00\n"
                         "\t\tLnkCap:\tPort #0, Speed 2.5GT/s, Width x1, ASPM L0s L1, Exit Latency L0s <2us, L1 <64us\n"
                         "\t\t\tClockPM+ Surprise- LLActRep- BwNot- ASPMOptComp+\n"
                         "\t\tLnkCtl:\tASPM L1 Enabled; RCB 64 bytes, Disabled- CommClk+\n"
                         "\t\t\tExtSynch- ClockPM+ AutWidDis- BWInt- AutBWInt-\n"
                         "\t\tLnkSta:\tSpeed 2.5GT/s, Width x1\n"
                         "\t\t\tTrErr- Train- SlotClk+ DLActive- BWMgmt- ABWMgmt-\n"
                         "\t\tDevCap2: Completion Timeout: Range ABCD, TimeoutDis+ NROPrPrP- LTR+\n"
                         "\t\tLnkCtl2: Target Link Speed: 2.5GT/s, EnterCompliance- SpeedDis-\n"
                         "\tCapabilities: [158 v1] Device Serial Number 00-e0-4c-ff-fe-c8-22-01\n"
                         "\tCapabilities: [178 v1] L1 PM Substates\n"
                         "\t\tL1SubCap: PCI-PM_L1.2+ PCI-PM_L1.1+ ASPM_L1.2+ ASPM_L1.1+ L1_PM_Substates+\n"
                         "\t\t\t  PortCommonModeRestoreTime=150us PortTPowerOnTime=150us\n"
                         "\t\tL1SubCtl1: PCI-PM_L1.2+ PCI-PM_L1.1+ ASPM_L1.2+ ASPM_L1.1+\n"
                         "\t\t\t   T_CommonMode=0us LTR1.2_Threshold=166912ns\n"
                         "\t\tL1SubCtl2: T_PwrOn=150us\n"
                         "\tKernel driver in use: rtw_8822ce\n"),
        "0000:00:01.2": ("00:01.2 PCI bridge: Advanced Micro Devices, Inc. [AMD] Family 17h-19h PCIe GPP Bridge\n"
                         "\t\tLnkCtl:\tASPM L1 Enabled; RCB 64 bytes, Disabled- CommClk+\n"
                         "\t\t\tExtSynch- ClockPM- AutWidDis- BWInt+ AutBWInt-\n"
                         "\t\tLnkSta:\tSpeed 2.5GT/s, Width x1\n"),
    }
    _r19 = tempfile.mkdtemp()
    _port = os.path.join(_r19, "devices", "pci0000:00", "0000:00:01.2")
    _card = os.path.join(_port, "0000:01:00.0")
    os.makedirs(os.path.join(_card, "link")); os.makedirs(os.path.join(_card, "power"))
    os.makedirs(os.path.join(_r19, "net", "wlan0"))
    os.symlink(_card, os.path.join(_r19, "net", "wlan0", "device"))
    _tools19 = os.path.join(_r19, "bin"); os.makedirs(_tools19)
    open(os.path.join(_tools19, "lspci"), "w").write("")
    _seen19 = []
    class Link19(m.Plugin):
        def _get_current_backend(self): return "wpa_supplicant"
        def _run_cmd(self, cmd, timeout=5):
            if cmd[0].endswith("/lspci"):
                _seen19.append((cmd, timeout))
                return _ok(_LSPCI.get(cmd[cmd.index("-s") + 1], ""))
            return _ok()
    _sn19, _td19 = m.SYSFS_NET, m.TOOL_DIRS
    try:
        m.SYSFS_NET, m.TOOL_DIRS = os.path.join(_r19, "net"), (_tools19,)
        _x19 = asyncio.run(type("D19", (Link19,), {
            "_get_wifi_interface": lambda self: "wlan0",
            "get_device_info": lambda self: asyncio.sleep(0, {"success": True, "driver": "rtw88"}),
            "_detect_distro": lambda self: {"id": "steamos", "name": "SteamOS"},
            "_get_support_tier": lambda self: 1})().get_diagnostic_info())
        _pl = _x19.get("pcie_link", {})
        m.TOOL_DIRS = (os.path.join(_r19, "none"),)
        _nol = m.Plugin._diagnostic_extras(Link19(), "rtw88", "wlan0").get("pcie_link", {})
    finally:
        m.SYSFS_NET, m.TOOL_DIRS = _sn19, _td19
    _w = _pl.get("wifi", [])
    ok(_pl.get("wifi_device") == "0000:01:00.0" and _pl.get("upstream_port") == "0000:00:01.2"
       and any(l.startswith("LnkCtl: ASPM L1 Enabled") and "ClockPM+" in l for l in _w)
       and any(l.startswith("L1SubCtl1:") and "LTR1.2_Threshold=166912ns" in l for l in _w)
       and any(l.startswith("LnkSta:") for l in _w) and any(l.startswith("LnkCap:") for l in _w),
       f"the card's LnkCap, LnkCtl, LnkSta and L1 substate lines, continuations joined ({_w})")
    ok(any(l.startswith("LnkCtl:") and "ClockPM-" in l for l in _pl.get("upstream", [])),
       "and the upstream port's")
    _flat19 = json.dumps(_pl)
    ok("Serial" not in _flat19 and "00-e0-4c" not in _flat19 and "AzureWave" not in _flat19
       and "LnkCtl2" not in _flat19 and "DevCap2" not in _flat19,
       "nothing else from lspci: no serial number, no vendor names")
    ok(all(c[1:3] == ["-vv", "-s"] and t <= 3 for c, t in _seen19) and len(_seen19) == 2,
       f"read only, two bounded lspci calls ({[c for c, _t in _seen19]})")
    ok(_nol.get("wifi") == ["unavailable: no lspci or setpci"],
       f"no tool found is said, not an error ({_nol})")
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("D20 the stale report message names the button")
try:
    ok("Tap Send report to the developer again" in m.Plugin._REPORT_MESSAGES["stale"],
       "the out-of-date message names the button as the panel labels it")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

section("C6 the report says whether the wake notice found a failed wake, and keeps saying it")
try:
    m.REPORT_URL = _REP_URL
    _rep_reset()
    _wjc6 = _p4_unix(_P4_UP + _p4_cycle(1000, errors=_P4_LCD_ERRORS))
    _yes = asyncio.run(RepStub(journal=_wjc6).preview_report()).get("payload", {}).get("report", {})
    _no = asyncio.run(RepStub(journal="").preview_report()).get("payload", {}).get("report", {})
    ok(_yes.get("wake_failure") is True and _no.get("wake_failure") is False,
       "report.wake_failure is true after a failed wake and false otherwise")
    _huge = dict(_REP_DIAG, os_release="x" * 200000)
    _hm = m._report_payload(_huge, "1.0.0", True)
    ok(_hm is not None and _hm[0]["report"].get("wake_failure") is True and "wake_failure" in m.REPORT_KEEP_KEYS,
       "and trimming a report too large never drops it")
    _ee = m._report_payload({"success": True, "extras_error": "boom", "device_info": {}, "os_release": "y" * 200000},
                            "1.0.0")
    ok(_ee is not None and _ee[0]["report"].get("extras_error") == "boom",
       "a report whose extras failed keeps extras_error, which the receiver takes in place of plugin_version")
    _rep_reset()
except Exception as _e:
    import traceback; traceback.print_exc()
    ok(False, f"section raised: {type(_e).__name__}: {_e}")
finally:
    try:
        m.REPORT_URL = _rep_url_real
    except NameError:
        pass


section("this file: no section rebinds a helper the others share")
try:
    import ast as _hg_ast
    _hg_shared = {"ok", "section", "_ok", "_between"}

    def _hg_bound(target):
        """The plain names an assignment target binds (not m.X or d[k])."""
        if isinstance(target, _hg_ast.Name):
            return [target]
        if isinstance(target, (_hg_ast.Tuple, _hg_ast.List)):
            return [n for e in target.elts for n in _hg_bound(e)]
        if isinstance(target, _hg_ast.Starred):
            return _hg_bound(target.value)
        return []

    _hg_bad = []
    for _hg_n in _hg_ast.walk(_hg_ast.parse(open(__file__).read())):
        _hg_targets = (_hg_n.targets if isinstance(_hg_n, _hg_ast.Assign)
                       else [_hg_n.target] if isinstance(_hg_n, (_hg_ast.AugAssign, _hg_ast.AnnAssign,
                                                                _hg_ast.For, _hg_ast.NamedExpr))
                       else [])
        for _hg_t in _hg_targets:
            _hg_bad += [(n.id, _hg_n.lineno) for n in _hg_bound(_hg_t) if n.id in _hg_shared]
    ok(not _hg_bad, f"a section that rebinds a shared helper breaks every section after it ({_hg_bad})")
except Exception as _e:
    ok(False, f"section raised: {type(_e).__name__}: {_e}")

print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
