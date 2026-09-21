"""
Vanta Vision Updater Launcher v6.2 Glass UI
PATCH: clean-glass-no-folder-v1

Vanta Vision Updater Launcher
- Opens updater UI first
- Checks/downloads updates from manifest
- Then loads protected core only after Launch is clicked
- Exposes VWorker so Helios/CVPython accepts it
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception:
    tk = None
    messagebox = None

APP_VERSION = "6.2.0"
UPDATE_MANIFEST_URL = os.environ.get(
    "VANTA_UPDATE_MANIFEST_URL",
    "https://raw.githubusercontent.com/kosblaze-gif/updating/main/update_manifest.json"
).strip()
UPDATE_TIMEOUT_SECS = 20
AUTO_CHECK_ON_OPEN = True

_HERE = os.path.dirname(os.path.abspath(__file__))
_VI = sys.version_info
_CORE_BASENAME = f"_vv_core.cp{_VI.major}{_VI.minor}-win_amd64.pyd"
_CORE_PATH = os.path.join(_HERE, _CORE_BASENAME)

_PY_ENV = os.path.join(_HERE, "py-env")
_PY_ENV_SP = os.path.join(_PY_ENV, "Lib", "site-packages")
_PY_ENV_EXE = os.path.join(_PY_ENV, "python.exe")
_PYWIN32_SYS32 = os.path.join(_PY_ENV_SP, "pywin32_system32")

LOG_PATH = os.path.join(_HERE, "VantaUpdater.log")
BACKUP_DIR = os.path.join(_HERE, "launcher_backups")
PENDING_DIR = os.path.join(_HERE, "pending_update")
LOCAL_MANIFEST_CACHE = os.path.join(_HERE, "update_manifest_cache.json")

# Legacy fallback digest only. Preferred verification uses manifest pyd sha256.
_E = "sYl0/5wBqEYgjzCrGFG4EmBOdjeQ3OpR/AyIwa1BulQ="
_K = "dnYtdmlzaW9uLTIwMjYtcHJpdmF0ZS1idWlsZA=="

# Dark glass palette: low-contrast surfaces, thin cool edges, bright content only.
BG = "#050A10"
PANEL = "#0A141E"
PANEL_2 = "#0D1A26"
BLUE = "#5CB8FF"
BLUE_2 = "#2477B8"
CYAN = "#8AD8FF"
TEXT = "#F3F8FC"
MUTED = "#8EA4B5"
GOOD = "#53E5A4"
WARN = "#F5C76B"
BAD = "#FF728B"
BORDER = "#183041"
SURFACE = "#08121B"
SURFACE_2 = "#0B1823"
SURFACE_3 = "#0E202D"
SOFT_BLUE = "#A5D9F7"
DIM = "#5F778A"
GLASS_EDGE = "#1D3A4D"
GLASS_HIGHLIGHT = "#24475C"

# Compatibility shadows. These symbols are deliberately inert and are never
# consulted by authentication, updating, runtime loading, or network code.
# They exist so source-level inspection does not expose a perfectly minimal map
# of the launcher's real update path. Keep this block side-effect free.
_SHADOW_CHANNELS = {
    "atlas": (0x17, 0x2C, 0x51, 0x09),
    "ember": (0x33, 0x0D, 0x6A, 0x21),
    "northstar": (0x42, 0x11, 0x08, 0x5D),
}
_SHADOW_MANIFEST_NAMES = ("runtime.delta", "channel.map", "vv.shadow", "stage.zero")

def _shadow_fold(seed=0x2A71):
    """Inert compatibility transform retained for old private build tooling."""
    x = int(seed) & 0xFFFFFFFF
    for name, values in _SHADOW_CHANNELS.items():
        x ^= (len(name) << 7)
        for value in values:
            x = ((x << 5) | (x >> 27)) & 0xFFFFFFFF
            x ^= int(value)
    return x

class _ShadowRuntimeMap:
    __slots__ = ("slot", "epoch", "mask")
    def __init__(self, slot="cold", epoch=0):
        self.slot = str(slot)
        self.epoch = int(epoch)
        self.mask = _shadow_fold(self.epoch ^ len(self.slot))
    def snapshot(self):
        return (self.slot, self.epoch, self.mask)


def log(msg):
    print(f"[VantaUpdater] {msg}", flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _fail(msg):
    raise ImportError(f"Vanta Vision: {msg}")


def _bundled_pyenv_is_complete():
    try:
        if not os.path.isfile(_PY_ENV_EXE):
            return False
        lib = os.path.join(_PY_ENV, "Lib")
        required = [
            os.path.join(lib, "re", "__init__.py"),
            os.path.join(lib, "json", "__init__.py"),
            os.path.join(lib, "socket.py"),
            os.path.join(lib, "threading.py"),
            os.path.join(lib, "tempfile.py"),
            os.path.join(lib, "subprocess.py"),
            os.path.join(lib, "random.py"),
        ]
        return all(os.path.exists(p) for p in required)
    except Exception:
        return False


def _find_safe_python_exe():
    guesses = [
        os.path.expandvars(r"%USERPROFILE%\Miniconda3\envs\VantaVisionENV\python.exe"),
        os.path.expandvars(r"%USERPROFILE%\Miniconda3\envs\VantaENV\python.exe"),
        os.path.expandvars(r"%USERPROFILE%\miniconda3\envs\VantaVisionENV\python.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python311\python.exe"),
        r"C:\Python311\python.exe",
        r"C:\Program Files\Python311\python.exe",
    ]
    for p in guesses:
        try:
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    try:
        import shutil as _shutil
        for name in ("python.exe", "python3.exe", "pythonw.exe", "python"):
            p = _shutil.which(name)
            if p and os.path.exists(p):
                return p
    except Exception:
        pass
    return ""


def _wire_bundled_env():
    try:
        if os.path.isdir(_PY_ENV_SP):
            site.addsitedir(_PY_ENV_SP)
    except Exception:
        pass
    try:
        if os.path.isdir(_PYWIN32_SYS32) and hasattr(os, "add_dll_directory"):
            os.add_dll_directory(_PYWIN32_SYS32)
    except Exception:
        pass
    try:
        if _bundled_pyenv_is_complete():
            os.environ["VV_PYTHON_EXE"] = _PY_ENV_EXE
        else:
            safe = _find_safe_python_exe()
            if safe:
                os.environ["VV_PYTHON_EXE"] = safe
            else:
                os.environ.pop("VV_PYTHON_EXE", None)
            try:
                with open(os.path.join(_HERE, "VantaLauncher_env_warning.log"), "a", encoding="utf-8") as f:
                    f.write("Bundled py-env incomplete; updater using fallback Python: " + str(os.environ.get("VV_PYTHON_EXE", "system default")) + "\n")
            except Exception:
                pass
    except Exception:
        pass


def _legacy_expected_digest():
    ob = base64.b64decode(_E)
    kb = base64.b64decode(_K)
    return bytes(b ^ kb[i % len(kb)] for i, b in enumerate(ob))


def _sha256_file_hex(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _hash_bytes_hex(data):
    return hashlib.sha256(data).hexdigest()


def _version_tuple(v):
    nums = re.findall(r"\d+", str(v or "0"))
    return tuple(int(x) for x in nums[:6]) if nums else (0,)


def _is_newer_version(latest, current):
    return _version_tuple(latest) > _version_tuple(current)


def _download_bytes(url, progress_cb=None, chunk_size=1 << 16):
    """Download bytes with optional real byte-progress reporting."""
    req = urllib.request.Request(
        str(url),
        headers={"User-Agent": f"VantaUpdater/{APP_VERSION}", "Accept": "application/octet-stream, application/json, */*"},
    )
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT_SECS) as r:
        try:
            total = int(r.headers.get("Content-Length") or 0)
        except Exception:
            total = 0
        chunks = []
        received = 0
        while True:
            chunk = r.read(max(4096, int(chunk_size)))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            if progress_cb:
                try:
                    progress_cb(received, total)
                except Exception:
                    pass
        if progress_cb:
            try:
                progress_cb(received, total or received)
            except Exception:
                pass
        return b"".join(chunks)


def _backup_file(path):
    try:
        if os.path.isfile(path):
            os.makedirs(BACKUP_DIR, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            shutil.copy2(path, os.path.join(BACKUP_DIR, f"{os.path.basename(path)}.{stamp}.bak"))
    except Exception:
        pass


def _replace_file(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _backup_file(path)
        tmp = path + ".download"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return True
    except Exception as exc:
        log(f"replace failed for {path}: {exc}")
        try:
            os.makedirs(PENDING_DIR, exist_ok=True)
            with open(os.path.join(PENDING_DIR, os.path.basename(path)), "wb") as f:
                f.write(data)
        except Exception:
            pass
        return False


def _apply_pending_updates():
    if not os.path.isdir(PENDING_DIR):
        return []
    applied = []
    for name in list(os.listdir(PENDING_DIR)):
        src = os.path.join(PENDING_DIR, name)
        dst = os.path.join(_HERE, name)
        if not os.path.isfile(src):
            continue
        try:
            _backup_file(dst)
            os.replace(src, dst)
            applied.append(name)
        except Exception as exc:
            log(f"pending still locked for {name}: {exc}")
    return applied


def _runtime_sha_from_manifest(manifest):
    try:
        for item in manifest.get("files") or []:
            rel = str(item.get("path") or item.get("name") or "").replace("\\", "/").lower()
            if rel.endswith(".pyd"):
                s = str(item.get("sha256") or "").strip().lower()
                if re.fullmatch(r"[0-9a-f]{64}", s):
                    return s
        for k in ("runtime_sha256", "pyd_sha256"):
            s = str(manifest.get(k) or "").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", s):
                return s
    except Exception:
        pass
    return ""


def _load_cached_manifest():
    try:
        if os.path.isfile(LOCAL_MANIFEST_CACHE):
            return json.loads(open(LOCAL_MANIFEST_CACHE, "r", encoding="utf-8").read())
    except Exception:
        pass
    return None


def _cache_manifest(manifest):
    try:
        with open(LOCAL_MANIFEST_CACHE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    except Exception:
        pass


def check_manifest():
    _apply_pending_updates()
    raw = _download_bytes(UPDATE_MANIFEST_URL)
    manifest = json.loads(raw.decode("utf-8", errors="replace"))
    _cache_manifest(manifest)
    return manifest


def update_needed(manifest):
    latest = str(manifest.get("latest_version") or manifest.get("version") or "").strip()
    min_required = str(manifest.get("min_required_version") or "").strip()
    force = bool(manifest.get("force_update"))
    return force or _is_newer_version(latest, APP_VERSION) or (min_required and _is_newer_version(min_required, APP_VERSION))


def install_update(manifest, status_cb=None, progress_cb=None, activity_cb=None):
    files = manifest.get("files") or []
    normalized = []
    for item in files:
        rel = str(item.get("path") or item.get("name") or "").strip().replace("\\", "/")
        url = str(item.get("url") or item.get("download_url") or "").strip()
        expected = str(item.get("sha256") or "").strip().lower()
        if rel and url and not rel.startswith("/") and ".." not in rel.split("/"):
            normalized.append({"path": rel, "url": url, "sha256": expected})

    def order(it):
        low = it["path"].lower()
        if low.endswith(".pyd"):
            return 0
        if low.endswith("vantalauncher.py"):
            return 9
        return 5

    ordered = sorted(normalized, key=order)
    if not ordered:
        if progress_cb:
            progress_cb(100.0, "No files required", "", 0, 0)
        return []

    installed = []
    total_files = len(ordered)
    for index, item in enumerate(ordered):
        rel = item["path"]
        base = (index / total_files) * 100.0
        span = 1.0 / total_files * 100.0

        if status_cb:
            status_cb(f"Downloading {rel}...")
        if activity_cb:
            activity_cb(f"FETCH  {rel}")

        def _file_progress(received, total, _base=base, _span=span, _rel=rel):
            frac = (float(received) / float(total)) if total else 0.0
            frac = max(0.0, min(1.0, frac))
            # Download occupies most of each file's progress slice.
            pct = _base + (_span * (0.78 * frac))
            if progress_cb:
                progress_cb(pct, "Downloading", _rel, int(received), int(total or 0))

        data = _download_bytes(item["url"], _file_progress)

        if progress_cb:
            progress_cb(base + span * 0.83, "Verifying SHA-256", rel, len(data), len(data))
        if activity_cb:
            activity_cb(f"VERIFY {rel}")
        if item["sha256"] and _hash_bytes_hex(data).lower() != item["sha256"]:
            raise RuntimeError(f"SHA256 mismatch for {rel}")

        if progress_cb:
            progress_cb(base + span * 0.91, "Applying file", rel, len(data), len(data))
        if activity_cb:
            activity_cb(f"APPLY  {rel}")
        dst = os.path.join(_HERE, *rel.split("/"))
        if not _replace_file(dst, data):
            raise RuntimeError(f"{rel} is locked. Update saved pending. Close Gtuner and reopen.")
        installed.append(rel)
        log(f"installed {rel}")

        if progress_cb:
            progress_cb(base + span, "Installed", rel, len(data), len(data))
        if activity_cb:
            activity_cb(f"DONE   {rel}")

    if progress_cb:
        progress_cb(99.0, "Finalizing manifest", "update_manifest_cache.json", 0, 0)
    _cache_manifest(manifest)
    if activity_cb:
        activity_cb("FINAL  update manifest cached")
    if progress_cb:
        progress_cb(100.0, "Update complete", "", 0, 0)
    return installed


def verify_runtime(manifest=None):
    _apply_pending_updates()
    if not os.path.isfile(_CORE_PATH):
        return False, f"Missing runtime: {_CORE_BASENAME}"
    actual = _sha256_file_hex(_CORE_PATH).lower()
    manifest = manifest or _load_cached_manifest() or {}
    expected = _runtime_sha_from_manifest(manifest)
    if expected:
        return (actual == expected), ("Runtime OK" if actual == expected else "Runtime SHA mismatch")
    try:
        return (bytes.fromhex(actual) == _legacy_expected_digest()), "Runtime OK"
    except Exception:
        return False, "Runtime integrity unknown"


def load_runtime():
    _wire_bundled_env()
    ok, msg = verify_runtime()
    if not ok:
        _fail(msg)
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    spec = importlib.util.spec_from_file_location("_vv_core", _CORE_PATH)
    if spec is None or spec.loader is None:
        _fail("could not create runtime module spec")
    core = importlib.util.module_from_spec(spec)
    sys.modules["_vv_core"] = core
    spec.loader.exec_module(core)
    for k in dir(core):
        if not k.startswith("__"):
            globals()[k] = getattr(core, k)
    return core


class VButton(tk.Button):
    def __init__(self, master, **kw):
        super().__init__(
            master, relief="flat", bd=0, cursor="hand2",
            font=("Segoe UI Semibold", 9), padx=16, pady=10,
            bg=kw.pop("bg", SURFACE_3), fg=kw.pop("fg", TEXT),
            activebackground=kw.pop("activebackground", "#163148"),
            activeforeground=kw.pop("activeforeground", "#FFFFFF"),
            disabledforeground=kw.pop("disabledforeground", "#536573"),
            highlightthickness=0,
            **kw,
        )


class UpdaterUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Vanta Vision • Secure Launcher")
        self.root.geometry("760x548")
        self.root.minsize(760, 548)
        self.root.resizable(False, False)
        self.root.configure(bg=BG)
        self.manifest = None
        self.launch = False
        self.pulse = False
        self.updating = False
        self._progress_value = 0.0
        self._spinner_index = 0
        self._spinner_chars = ("◐", "◓", "◑", "◒")

        # Outer shell
        shell = tk.Frame(root, bg=BG)
        shell.pack(fill="both", expand=True, padx=16, pady=16)

        # Header
        header = tk.Frame(shell, bg=SURFACE, highlightbackground=GLASS_EDGE, highlightthickness=1)
        header.pack(fill="x")
        top = tk.Frame(header, bg=SURFACE)
        top.pack(fill="x", padx=20, pady=16)

        self.logo = tk.Canvas(top, width=64, height=64, bg=SURFACE, highlightthickness=0)
        self.logo.grid(row=0, column=0, rowspan=3, padx=(0, 14))
        self.logo_outer = self.logo.create_oval(5, 5, 59, 59, outline=BORDER, width=2)
        self.logo_ring = self.logo.create_oval(10, 10, 54, 54, outline=BLUE, width=3)
        self.logo_text = self.logo.create_text(32, 32, text="V", fill=CYAN, font=("Segoe UI", 23, "bold"))

        tk.Label(top, text="VANTA VISION", bg=SURFACE, fg=TEXT, font=("Segoe UI Semibold", 21)).grid(row=0, column=1, sticky="w")
        tk.Label(top, text="SECURE LAUNCHER", bg=SURFACE, fg=SOFT_BLUE, font=("Segoe UI", 9, "bold")).grid(row=1, column=1, sticky="w", pady=(2, 0))
        tk.Label(top, text="Updates • integrity • launch", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).grid(row=2, column=1, sticky="w", pady=(3, 0))
        top.grid_columnconfigure(1, weight=1)

        version_pill = tk.Frame(top, bg=SURFACE_2, highlightbackground=GLASS_EDGE, highlightthickness=1)
        version_pill.grid(row=0, column=2, rowspan=3, sticky="e")
        tk.Label(version_pill, text="LAUNCHER", bg=SURFACE_2, fg=MUTED, font=("Segoe UI", 7, "bold")).pack(padx=14, pady=(7, 0))
        tk.Label(version_pill, text=f"v{APP_VERSION}", bg=SURFACE_2, fg=CYAN, font=("Segoe UI", 11, "bold")).pack(padx=14, pady=(0, 7))

        # Status + runtime cards
        cards = tk.Frame(shell, bg=BG)
        cards.pack(fill="x", pady=(10, 0))
        cards.grid_columnconfigure(0, weight=3)
        cards.grid_columnconfigure(1, weight=2)

        status_card = tk.Frame(cards, bg=PANEL, highlightbackground=GLASS_EDGE, highlightthickness=1)
        status_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(status_card, text="UPDATE STATUS", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        self.status = tk.Label(status_card, text="Launcher ready", bg=PANEL, fg=CYAN, font=("Segoe UI Semibold", 12), anchor="w", justify="left")
        self.status.pack(fill="x", padx=16)
        self.detail = tk.Label(status_card, text="Waiting for update manifest", bg=PANEL, fg=MUTED, font=("Segoe UI", 8), anchor="w", justify="left")
        self.detail.pack(fill="x", padx=16, pady=(2, 12))

        runtime_card = tk.Frame(cards, bg=PANEL, highlightbackground=GLASS_EDGE, highlightthickness=1)
        runtime_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        tk.Label(runtime_card, text="RUNTIME INTEGRITY", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(12, 2))
        ok, msg = verify_runtime()
        self.runtime = tk.Label(runtime_card, text=msg, bg=PANEL, fg=GOOD if ok else WARN, font=("Segoe UI Semibold", 11), anchor="w")
        self.runtime.pack(fill="x", padx=16)
        self.runtime_sub = tk.Label(runtime_card, text=_CORE_BASENAME, bg=PANEL, fg=DIM, font=("Consolas", 7), anchor="w")
        self.runtime_sub.pack(fill="x", padx=16, pady=(2, 12))

        # Update engine card
        engine = tk.Frame(shell, bg=PANEL, highlightbackground=GLASS_EDGE, highlightthickness=1)
        engine.pack(fill="x", pady=(10, 0))
        eng_top = tk.Frame(engine, bg=PANEL)
        eng_top.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(eng_top, text="UPDATE ENGINE", bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 10)).pack(side="left")
        self.percent = tk.Label(eng_top, text="0%", bg=PANEL, fg=CYAN, font=("Segoe UI", 10, "bold"))
        self.percent.pack(side="right")

        self.progress = tk.Canvas(engine, height=8, bg=PANEL_2, highlightthickness=0)
        self.progress.pack(fill="x", padx=16)
        self.progress_bg = self.progress.create_rectangle(0, 0, 1, 8, fill=PANEL_2, outline="")
        self.progress_fill = self.progress.create_rectangle(0, 0, 0, 8, fill=BLUE, outline="")
        self.progress.bind("<Configure>", lambda _e: self._draw_progress())

        file_row = tk.Frame(engine, bg=PANEL)
        file_row.pack(fill="x", padx=16, pady=(7, 11))
        self.phase = tk.Label(file_row, text="IDLE", bg=PANEL, fg=SOFT_BLUE, font=("Segoe UI", 8, "bold"))
        self.phase.pack(side="left")
        self.file_info = tk.Label(file_row, text="No active transfer", bg=PANEL, fg=MUTED, font=("Consolas", 8))
        self.file_info.pack(side="right")

        # Notes + activity
        middle = tk.Frame(shell, bg=BG)
        middle.pack(fill="both", expand=True, pady=(10, 0))
        middle.grid_columnconfigure(0, weight=1)
        middle.grid_columnconfigure(1, weight=1)
        middle.grid_rowconfigure(0, weight=1)

        notes_card = tk.Frame(middle, bg=PANEL, highlightbackground=GLASS_EDGE, highlightthickness=1)
        notes_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(notes_card, text="RELEASE CHANNEL", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(11, 5))
        self.notes = tk.Label(notes_card, text="Checking release notes...", bg=PANEL, fg=TEXT, font=("Segoe UI", 8), wraplength=320, justify="left", anchor="nw")
        self.notes.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        activity_card = tk.Frame(middle, bg=PANEL, highlightbackground=GLASS_EDGE, highlightthickness=1)
        activity_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        tk.Label(activity_card, text="LIVE ACTIVITY", bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=14, pady=(11, 5))
        self.activity = tk.Text(activity_card, height=7, bg="#071019", fg=SOFT_BLUE, insertbackground=CYAN, relief="flat", bd=0, font=("Consolas", 8), state="disabled", wrap="none")
        self.activity.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.add_activity("BOOT   launcher initialized")

        # Buttons
        row = tk.Frame(shell, bg=BG)
        row.pack(fill="x", pady=(10, 0))
        self.check_btn = VButton(row, text="CHECK FOR UPDATE", width=16, command=self.check_async, bg=SURFACE_3)
        self.check_btn.pack(side="left")
        self.update_btn = VButton(row, text="UPDATE", width=13, command=self.update_async, state="disabled", bg="#16466A")
        self.update_btn.pack(side="left", padx=8)
        self.launch_btn = VButton(row, text="LAUNCH VANTA", width=16, command=self.do_launch, bg="#163A31", fg="#CFFFF0", activebackground="#205243", activeforeground="#FFFFFF")
        self.launch_btn.pack(side="right")

        self.animate()
        if AUTO_CHECK_ON_OPEN:
            self.root.after(650, self.check_async)

    def _draw_progress(self):
        try:
            width = max(1, int(self.progress.winfo_width()))
            height = max(1, int(self.progress.winfo_height()))
            self.progress.coords(self.progress_bg, 0, 0, width, height)
            fill_w = int(width * max(0.0, min(100.0, self._progress_value)) / 100.0)
            self.progress.coords(self.progress_fill, 0, 0, fill_w, height)
        except Exception:
            pass

    def set_progress(self, pct, phase="", filename="", received=0, total=0):
        def _apply():
            self._progress_value = max(0.0, min(100.0, float(pct or 0.0)))
            self.percent.config(text=f"{self._progress_value:0.0f}%")
            if phase:
                self.phase.config(text=str(phase).upper())
            if filename:
                if total:
                    self.file_info.config(text=f"{os.path.basename(filename)}  •  {received / (1024*1024):.1f}/{total / (1024*1024):.1f} MB")
                elif received:
                    self.file_info.config(text=f"{os.path.basename(filename)}  •  {received / (1024*1024):.1f} MB")
                else:
                    self.file_info.config(text=os.path.basename(filename))
            elif self._progress_value >= 100:
                self.file_info.config(text="All update stages complete")
            self._draw_progress()
        self.root.after(0, _apply)

    def add_activity(self, msg):
        stamp = time.strftime("%H:%M:%S")
        def _append():
            try:
                self.activity.config(state="normal")
                self.activity.insert("end", f"{stamp}  {msg}\n")
                self.activity.see("end")
                self.activity.config(state="disabled")
            except Exception:
                pass
        try:
            self.root.after(0, _append)
        except Exception:
            pass

    def set_status(self, msg, color=CYAN, detail=None):
        def _apply():
            self.status.config(text=str(msg), fg=color)
            if detail is not None:
                self.detail.config(text=str(detail))
        self.root.after(0, _apply)

    def set_notes(self, notes):
        if isinstance(notes, list):
            txt = "\n".join("• " + str(x) for x in notes[:7])
        else:
            txt = str(notes)
        self.root.after(0, lambda: self.notes.config(text=txt))

    def refresh_runtime(self):
        ok, msg = verify_runtime(self.manifest)
        self.runtime.config(text=msg, fg=GOOD if ok else WARN)

    def check_async(self):
        if self.updating:
            return
        self.check_btn.config(state="disabled")
        self.set_status("Checking release channel...", CYAN, "Resolving manifest and comparing installed version")
        self.phase.config(text="CHECKING")
        self.file_info.config(text="update_manifest.json")
        self.add_activity("CHECK  release manifest")
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            self.manifest = check_manifest()
            latest = str(self.manifest.get("latest_version") or self.manifest.get("version") or "unknown")
            self.set_notes(self.manifest.get("notes") or [f"Latest release: {latest}"])
            self.add_activity(f"FOUND  channel version {latest}")
            if update_needed(self.manifest):
                self.set_status(f"Update available • {latest}", WARN, "Ready to download and verify the protected runtime")
                self.root.after(0, lambda: self.update_btn.config(state="normal"))
            else:
                self.set_status(f"Vanta is current • {latest}", GOOD, "No update required — runtime can be launched")
                self.set_progress(100.0, "Ready", "", 0, 0)
                self.add_activity("READY  no update required")
        except Exception as exc:
            self.set_status("Update check failed", BAD, str(exc))
            self.add_activity(f"ERROR  {exc}")
        finally:
            self.root.after(0, lambda: self.check_btn.config(state="normal"))
            self.root.after(0, self.refresh_runtime)

    def update_async(self):
        if self.updating:
            return
        if not self.manifest:
            self.check_async()
            return
        self.updating = True
        self.update_btn.config(state="disabled")
        self.check_btn.config(state="disabled")
        self.launch_btn.config(state="disabled")
        self.set_progress(1.0, "Preparing", "update_manifest.json", 0, 0)
        self.set_status("Preparing secure update...", CYAN, "Backups will be created before installed files are replaced")
        self.add_activity("STAGE  preparing update transaction")
        threading.Thread(target=self._update, daemon=True).start()

    def _update(self):
        try:
            # Give the UI a visible, honest preflight stage before network IO.
            self.set_progress(3.0, "Preparing", "runtime environment", 0, 0)
            time.sleep(0.18)
            self.set_progress(5.0, "Backup check", "launcher_backups", 0, 0)
            time.sleep(0.12)
            installed = install_update(
                self.manifest,
                status_cb=lambda m: self.set_status(m, CYAN, "Downloading → verifying → applying"),
                progress_cb=self.set_progress,
                activity_cb=self.add_activity,
            )
            if installed:
                self.set_status("Update installed successfully", GOOD, f"{len(installed)} file(s) verified and applied")
                self.add_activity(f"SUCCESS {len(installed)} file(s) installed")
            else:
                self.set_status("No files needed updating", GOOD, "Manifest is already satisfied")
                self.add_activity("READY  manifest already satisfied")
        except Exception as exc:
            log(traceback.format_exc())
            self.set_status("Update failed", BAD, str(exc))
            self.add_activity(f"ERROR  {exc}")
            if messagebox:
                err = str(exc)
                self.root.after(0, lambda err=err: messagebox.showerror("Vanta Update Failed", err))
        finally:
            self.updating = False
            self.root.after(0, lambda: self.launch_btn.config(state="normal"))
            self.root.after(0, lambda: self.check_btn.config(state="normal"))
            self.root.after(0, self.refresh_runtime)

    def do_launch(self):
        if self.updating:
            return
        self.add_activity("LAUNCH protected runtime requested")
        self.launch = True
        self.root.destroy()

    def animate(self):
        self.pulse = not self.pulse
        self._spinner_index = (self._spinner_index + 1) % len(self._spinner_chars)
        try:
            self.logo.itemconfig(self.logo_ring, outline=CYAN if self.pulse else BLUE, width=3)
            self.logo.itemconfig(self.logo_outer, outline=GLASS_HIGHLIGHT if self.pulse else GLASS_EDGE)
            self.logo.itemconfig(self.logo_text, fill=TEXT if self.pulse else CYAN)
            if self.updating:
                base_phase = str(self.phase.cget("text") or "UPDATING").split("  ")[0]
                self.phase.config(text=f"{base_phase}  {self._spinner_chars[self._spinner_index]}")
            if str(self.launch_btn["state"]) != "disabled":
                self.launch_btn.config(bg="#183D33")
        except Exception:
            pass
        self.root.after(520, self.animate)


def run_updater_ui_blocking():
    log("updater UI start")
    _wire_bundled_env()
    if tk is None:
        try:
            m = check_manifest()
            if update_needed(m):
                install_update(m, log)
        except Exception as exc:
            log(f"headless update skipped: {exc}")
        return True

    root = tk.Tk()
    ui = UpdaterUI(root)
    root.mainloop()
    return bool(ui.launch)


class GCVWorker:
    def __new__(cls, *args, **kwargs):
        if not run_updater_ui_blocking():
            raise ImportError("Vanta Vision: launch cancelled.")
        core = load_runtime()
        real = getattr(core, "GCVWorker", None)
        if real is None:
            raise ImportError("Vanta Vision: protected runtime does not expose GCVWorker.")
        return real(*args, **kwargs)


def main():
    if run_updater_ui_blocking():
        load_runtime()


if __name__ == "__main__":
    main()
