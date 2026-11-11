#!/usr/bin/env python3
"""
ZeroTier Alternative UI
=======================

Małe okienko (Tkinter, bez zewnętrznych bibliotek) pokazujące wszystkie sieci
ZeroTier na tym komputerze wraz ze statusem połączenia, adresem IP itd.

Rozmawia bezpośrednio z lokalną usługą ZeroTier One przez jej HTTP API
(domyślnie http://127.0.0.1:9993). Token autoryzacyjny odczytywany jest
z pliku authtoken.secret (patrz _token_candidates() poniżej).

Uruchomienie:  python zerotier_ui.py
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import platform
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
import webbrowser
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Callable, Optional

if platform.system() == "Windows":
    from ctypes import wintypes

APP_TITLE = "ZeroTier - sieci"
DEFAULT_PORT = 9993
REFRESH_SECONDS = 5
IS_WINDOWS = platform.system() == "Windows"

# --------------------------------------------------------------------------
# Lokalizacje plików ZeroTier (kolejność = priorytet)
# --------------------------------------------------------------------------


def _zt_home_candidates() -> list[Path]:
    homes: list[Path] = []
    if IS_WINDOWS:
        program_data = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        homes.append(Path(program_data) / "ZeroTier" / "One")
    elif platform.system() == "Darwin":
        homes.append(Path("/Library/Application Support/ZeroTier/One"))
    else:
        homes.append(Path("/var/lib/zerotier-one"))
    return homes


def _token_candidates() -> list[Path]:
    """Miejsca, w których może leżeć authtoken.secret.

    Na Windows usługa trzyma go w C:\\ProgramData\\ZeroTier\\One (wymaga
    uprawnień administratora), ale oficjalne UI kopiuje go też do profilu
    użytkownika, więc sprawdzamy wszystkie warianty.
    """
    cands: list[Path] = []
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(Path(local) / "ZeroTier" / "authtoken.secret")
            cands.append(Path(local) / "ZeroTier" / "One" / "authtoken.secret")
    for home in _zt_home_candidates():
        cands.append(home / "authtoken.secret")
    cands.append(Path.home() / ".zeroTierOneAuthToken")
    # Token można też podać ręcznie przez zmienną środowiskową
    return cands


def _user_token_path() -> Optional[Path]:
    """Ścieżka, do której możemy skopiować token, by nie wymagać admina."""
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "ZeroTier" / "authtoken.secret"
        return None
    return Path.home() / ".zeroTierOneAuthToken"


def read_auth_token() -> tuple[Optional[str], list[str]]:
    """Zwraca (token, lista_sprawdzonych_ścieżek)."""
    env = os.environ.get("ZT_AUTHTOKEN")
    if env:
        return env.strip(), ["ZT_AUTHTOKEN (zmienna środowiskowa)"]

    checked: list[str] = []
    for path in _token_candidates():
        checked.append(str(path))
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, PermissionError):
            continue
        if text:
            return text, checked
    return None, checked


def read_service_port() -> int:
    env = os.environ.get("ZT_PORT")
    if env and env.isdigit():
        return int(env)
    for home in _zt_home_candidates():
        try:
            text = (home / "zerotier-one.port").read_text(encoding="utf-8").strip()
            if text.isdigit():
                return int(text)
        except OSError:
            pass
    return DEFAULT_PORT


def copy_token_to_user_profile() -> Optional[Path]:
    """Kopiuje authtoken.secret z katalogu usługi do profilu użytkownika.

    Działa tylko, jeśli mamy prawo odczytu źródła (np. proces uruchomiony
    jako administrator). Zwraca ścieżkę docelową albo None.
    """
    dest = _user_token_path()
    if dest is None:
        return None
    for home in _zt_home_candidates():
        src = home / "authtoken.secret"
        try:
            if src.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
                return dest
        except OSError:
            continue
    return None


def is_admin() -> bool:
    if not IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Ponownie uruchamia ten skrypt z podniesionymi uprawnieniami (UAC)."""
    if not IS_WINDOWS:
        return False
    try:
        params = " ".join(f'"{a}"' for a in sys.argv)
        rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", sys.executable, params, None, 1
        )
        return int(rc) > 32
    except Exception:
        return False


# --------------------------------------------------------------------------
# Klient lokalnego API ZeroTier
# --------------------------------------------------------------------------


class ZeroTierError(Exception):
    pass


class AuthError(ZeroTierError):
    pass


class ServiceUnavailable(ZeroTierError):
    pass


@dataclass
class Network:
    id: str
    name: str
    status: str
    type: str
    addresses: list[str] = field(default_factory=list)
    device: str = ""
    mac: str = ""
    mtu: int = 0
    allow_managed: bool = True
    allow_global: bool = False
    allow_default: bool = False
    allow_dns: bool = False
    auth_url: str = ""
    routes: int = 0

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Network":
        return cls(
            id=str(d.get("id") or d.get("nwid") or ""),
            name=str(d.get("name") or ""),
            status=str(d.get("status") or "UNKNOWN"),
            type=str(d.get("type") or ""),
            addresses=list(d.get("assignedAddresses") or []),
            device=str(d.get("portDeviceName") or ""),
            mac=str(d.get("mac") or ""),
            mtu=int(d.get("mtu") or 0),
            allow_managed=bool(d.get("allowManaged", True)),
            allow_global=bool(d.get("allowGlobal", False)),
            allow_default=bool(d.get("allowDefault", False)),
            allow_dns=bool(d.get("allowDNS", False)),
            auth_url=str(d.get("authenticationURL") or ""),
            routes=len(d.get("routes") or []),
        )

    @property
    def connected(self) -> bool:
        return self.status == "OK"


@dataclass
class NodeStatus:
    address: str = ""
    online: bool = False
    version: str = ""
    tcp_fallback: bool = False

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "NodeStatus":
        return cls(
            address=str(d.get("address") or ""),
            online=bool(d.get("online", False)),
            version=str(d.get("version") or ""),
            tcp_fallback=bool(d.get("tcpFallbackActive", False)),
        )


STATUS_LABELS = {
    "OK": "Połączono",
    "REQUESTING_CONFIGURATION": "Pobieranie konfiguracji…",
    "ACCESS_DENIED": "Brak autoryzacji (zatwierdź w panelu)",
    "NOT_FOUND": "Sieć nie istnieje",
    "PORT_ERROR": "Błąd interfejsu sieciowego",
    "CLIENT_TOO_OLD": "Za stary klient ZeroTier",
    "AUTHENTICATION_REQUIRED": "Wymagane logowanie (SSO)",
}


class ZeroTierClient:
    def __init__(self, token: str, port: int = DEFAULT_PORT, host: str = "127.0.0.1"):
        self.token = token
        self.base = f"http://{host}:{port}"

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        data = None
        headers = {"X-ZT1-Auth": self.token, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError("Usługa ZeroTier odrzuciła token autoryzacyjny (HTTP %d)." % e.code) from e
            raise ZeroTierError(f"HTTP {e.code} dla {method} {path}") from e
        except urllib.error.URLError as e:
            raise ServiceUnavailable(
                "Brak połączenia z usługą ZeroTier One pod %s. Czy usługa działa?" % self.base
            ) from e
        except OSError as e:
            raise ServiceUnavailable(str(e)) from e
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise ZeroTierError("Niepoprawna odpowiedź JSON z usługi.") from e

    # ---- odczyt --------------------------------------------------------
    def status(self) -> NodeStatus:
        return NodeStatus.from_json(self._request("GET", "/status") or {})

    def networks(self) -> list[Network]:
        data = self._request("GET", "/network") or []
        return [Network.from_json(n) for n in data]

    # ---- akcje ---------------------------------------------------------
    def join(self, network_id: str, options: Optional[dict] = None) -> None:
        self._request("POST", f"/network/{network_id}", dict(options or {}))

    def leave(self, network_id: str) -> None:
        self._request("DELETE", f"/network/{network_id}")

    def set_option(self, network_id: str, key: str, value: bool) -> None:
        self._request("POST", f"/network/{network_id}", {key: value})


# --------------------------------------------------------------------------
# Konfiguracja własna (pamięta sieci, autoconnect i ustawienia)
# --------------------------------------------------------------------------
#
# ZeroTier nie ma "rozłączenia" sieci bez jej opuszczenia (leave), a po
# opuszczeniu usługa o sieci zapomina. Dlatego program trzyma własną listę
# znanych sieci (ID, nazwa, opcje allow*, flaga autoconnect) w pliku JSON.

CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "ZeroTierAlt" / "config.json"

DEFAULT_SETTINGS: dict[str, bool] = {
    "disconnect_on_exit": False,   # przy zamykaniu programu / wyłączaniu komputera opuść sieci bez autoconnect
    "disconnect_on_start": False,  # przy starcie programu opuść sieci bez autoconnect
    "connect_on_start": True,      # przy starcie programu połącz sieci z autoconnect
    "close_to_tray": True,         # przycisk X chowa okno do zasobnika
    "notifications": True,         # dymki z zasobnika przy zmianach stanu sieci
}

AUTOSTART_NAME = "ZeroTierAlt"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, AUTOSTART_NAME)
        return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Wpis w HKCU\\...\\Run - start z Windows, od razu schowany do zasobnika."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            exe = Path(sys.executable)
            pythonw = exe.with_name("pythonw.exe")
            if pythonw.exists():
                exe = pythonw
            cmd = f'"{exe}" "{Path(__file__).resolve()}" --minimized'
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


class Config:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self.settings: dict[str, bool] = dict(DEFAULT_SETTINGS)
        self.networks: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, list[str]] = {}   # nazwa profilu -> ID sieci, które mają być połączone
        self._lock = threading.Lock()

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            data = json.loads(cfg.path.read_text(encoding="utf-8"))
            saved = data.get("settings") or {}
            for key in DEFAULT_SETTINGS:
                if isinstance(saved.get(key), bool):
                    cfg.settings[key] = saved[key]
            nets = data.get("networks")
            if isinstance(nets, dict):
                cfg.networks = {str(k): v for k, v in nets.items() if isinstance(v, dict)}
            profs = data.get("profiles")
            if isinstance(profs, dict):
                cfg.profiles = {str(k): [str(i) for i in v] for k, v in profs.items() if isinstance(v, list)}
        except (OSError, ValueError, AttributeError):
            pass
        return cfg

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps({"settings": self.settings, "networks": self.networks,
                                "profiles": self.profiles},
                               indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                os.replace(tmp, self.path)
            except (OSError, RuntimeError):
                pass

    def is_auto(self, network_id: str) -> bool:
        return bool(self.networks.get(network_id, {}).get("autoconnect"))

    def remember(self, n: Network) -> bool:
        """Zapamiętuje nazwę i opcje aktywnej sieci. Zwraca True, jeśli coś się zmieniło."""
        created = n.id not in self.networks
        entry = self.networks.setdefault(n.id, {"autoconnect": False})
        new = {
            "name": n.name or entry.get("name", ""),
            "options": {
                "allowManaged": n.allow_managed,
                "allowGlobal": n.allow_global,
                "allowDefault": n.allow_default,
                "allowDNS": n.allow_dns,
            },
        }
        changed = created
        for k, v in new.items():
            if entry.get(k) != v:
                entry[k] = v
                changed = True
        return changed


def job_connect(client: ZeroTierClient, config: Config, network_id: str) -> str:
    client.join(network_id, config.networks.get(network_id, {}).get("options"))
    return f"Łączenie z {network_id}…"


def job_disconnect(client: ZeroTierClient, config: Config, network_id: str) -> str:
    client.leave(network_id)
    return f"Rozłączono sieć {network_id}."


def job_disconnect_all(client: ZeroTierClient, config: Config) -> str:
    ids = [n.id for n in client.networks()]
    for nid in ids:
        client.leave(nid)
    return f"Rozłączono sieci: {len(ids)}."


def job_connect_auto(client: ZeroTierClient, config: Config) -> str:
    active = {n.id for n in client.networks()}
    todo = [nid for nid in list(config.networks) if config.is_auto(nid) and nid not in active]
    for nid in todo:
        client.join(nid, config.networks[nid].get("options"))
    return f"Połączono sieci z autoconnect: {len(todo)}."


def job_startup(client: ZeroTierClient, config: Config, do_leave: bool, do_join: bool) -> str:
    left = joined = 0
    active = {n.id for n in client.networks()}
    if do_leave:
        for nid in active:
            if not config.is_auto(nid):
                client.leave(nid)
                left += 1
    if do_join:
        for nid in list(config.networks):
            if config.is_auto(nid) and nid not in active:
                client.join(nid, config.networks[nid].get("options"))
                joined += 1
    return f"Start: rozłączono {left}, połączono {joined}."


def job_apply_profile(client: ZeroTierClient, config: Config, name: str) -> str:
    """Przełącza na profil: łączy jego sieci, rozłącza pozostałe."""
    wanted = set(config.profiles.get(name, []))
    active = {n.id for n in client.networks()}
    for nid in active - wanted:
        client.leave(nid)
    for nid in wanted - active:
        client.join(nid, config.networks.get(nid, {}).get("options"))
    return f"Profil „{name}”: połączono {len(wanted - active)}, rozłączono {len(active - wanted)}."


def disconnect_for_exit(client: ZeroTierClient, config: Config) -> None:
    """Opuszcza sieci bez autoconnect (zapamiętując je wcześniej na liście)."""
    for n in client.networks():
        if not config.is_auto(n.id):
            config.remember(n)
            client.leave(n.id)
    config.save()


# --------------------------------------------------------------------------
# Ikona w zasobniku systemowym (Windows, czyste ctypes - bez pystray)
# --------------------------------------------------------------------------

if IS_WINDOWS:

    class _WNDCLASSW(ctypes.Structure):
        pass

    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
    _WNDCLASSW._fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON), ("szTip", ctypes.c_wchar * 128),
            ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
            ("szInfo", ctypes.c_wchar * 256), ("uVersion", wintypes.UINT),
            ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON),
        ]

    class _ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP),
        ]


class TrayIcon:
    """Ikona w zasobniku z menu i obsługą zamykania sesji Windows.

    Cała obsługa komunikatów działa we własnym wątku (ukryte okno + pętla
    komunikatów). Do reszty programu komunikuje się przez callbacki, które
    nie mogą dotykać Tkintera - wrzucają polecenia do kolejki.
    """

    WM_TRAY = 0x0400 + 20
    COLORS = {"ok": (34, 160, 70), "idle": (140, 140, 140), "err": (200, 40, 50)}

    def __init__(self, menu_provider: Callable[[], list], on_command: Callable[[int], None],
                 on_session_end: Callable[[], None], tooltip: str) -> None:
        self._menu_provider = menu_provider
        self._on_command = on_command
        self._on_session_end = on_session_end
        self._kind = "idle"
        self._tip = tooltip
        self._icons: dict[str, int] = {}
        self.hwnd: Optional[int] = None
        self._ready = threading.Event()

    # ---- API publiczne (wołane z wątku Tk) ------------------------------
    def start(self) -> bool:
        threading.Thread(target=self._thread_main, daemon=True).start()
        self._ready.wait(5.0)
        return self.hwnd is not None

    def set_state(self, kind: str, tip: str) -> None:
        self._kind, self._tip = kind, tip
        if self.hwnd:
            self._notify(1)  # NIM_MODIFY

    def stop(self) -> None:
        if self.hwnd:
            self._notify(2)  # NIM_DELETE - od razu, żeby nie został "duch" ikony
            self._user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
            self.hwnd = None

    # ---- wątek z pętlą komunikatów --------------------------------------
    def _thread_main(self) -> None:
        try:
            self._setup()
        except Exception:  # noqa: BLE001 - brak ikony nie może zatrzymać programu
            self.hwnd = None
            self._ready.set()
            return
        msg = wintypes.MSG()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))

    def _setup(self) -> None:
        u = self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32")
        k = ctypes.WinDLL("kernel32")
        c = ctypes
        w = wintypes

        k.GetModuleHandleW.restype = w.HMODULE
        k.GetModuleHandleW.argtypes = [w.LPCWSTR]
        u.DefWindowProcW.restype = c.c_ssize_t
        u.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.RegisterClassW.argtypes = [c.POINTER(_WNDCLASSW)]
        u.CreateWindowExW.restype = w.HWND
        u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, c.c_int, c.c_int,
                                      c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
        u.RegisterWindowMessageW.argtypes = [w.LPCWSTR]
        u.GetMessageW.argtypes = [c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        u.TranslateMessage.argtypes = [c.POINTER(w.MSG)]
        u.DispatchMessageW.argtypes = [c.POINTER(w.MSG)]
        u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        u.PostQuitMessage.argtypes = [c.c_int]
        u.CreatePopupMenu.restype = w.HMENU
        u.AppendMenuW.argtypes = [w.HMENU, w.UINT, c.c_size_t, w.LPCWSTR]
        u.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, c.c_int, c.c_int, c.c_int, w.HWND, c.c_void_p]
        u.DestroyMenu.argtypes = [w.HMENU]
        u.GetCursorPos.argtypes = [c.POINTER(w.POINT)]
        u.SetForegroundWindow.argtypes = [w.HWND]
        u.CreateIconIndirect.restype = w.HICON
        u.CreateIconIndirect.argtypes = [c.POINTER(_ICONINFO)]
        self._gdi32.CreateBitmap.restype = w.HBITMAP
        self._gdi32.CreateBitmap.argtypes = [c.c_int, c.c_int, w.UINT, w.UINT, c.c_void_p]
        self._gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
        self._shell32.Shell_NotifyIconW.argtypes = [w.DWORD, c.POINTER(_NOTIFYICONDATAW)]

        self._wndproc = _WNDPROC(self._on_message)  # trzymamy referencję
        self._wm_taskbar = u.RegisterWindowMessageW("TaskbarCreated")
        hinst = k.GetModuleHandleW(None)
        wc = _WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "ZeroTierAltTrayWindow"
        u.RegisterClassW(c.byref(wc))
        # Zwykłe (ukryte) okno najwyższego poziomu - tylko takie dostaje
        # WM_QUERYENDSESSION / WM_ENDSESSION przy wyłączaniu komputera.
        hwnd = u.CreateWindowExW(0, wc.lpszClassName, "ZeroTier tray", 0, 0, 0, 0, 0,
                                 None, None, hinst, None)
        if not hwnd:
            raise OSError("CreateWindowExW failed")
        self.hwnd = hwnd
        self._notify(0)  # NIM_ADD
        self._ready.set()

    # ---- ikona ----------------------------------------------------------
    def _make_icon(self, kind: str) -> int:
        size = max(16, self._user32.GetSystemMetrics(49))  # SM_CXSMICON
        r0, g0, b0 = self.COLORS.get(kind, self.COLORS["idle"])
        buf = (ctypes.c_ubyte * (size * size * 4))()
        center = (size - 1) / 2
        radius = size / 2 - 0.5
        for y in range(size):
            for x in range(size):
                d = math.hypot(x - center, y - center)
                alpha = max(0.0, min(1.0, radius + 0.5 - d))
                shade = 0.65 if d > radius - 1.6 else 1.0
                i = (y * size + x) * 4
                buf[i], buf[i + 1], buf[i + 2] = int(b0 * shade), int(g0 * shade), int(r0 * shade)
                buf[i + 3] = int(alpha * 255)
        mask = (ctypes.c_ubyte * (((size + 15) // 16) * 2 * size))()
        color_bm = self._gdi32.CreateBitmap(size, size, 1, 32, buf)
        mask_bm = self._gdi32.CreateBitmap(size, size, 1, 1, mask)
        info = _ICONINFO(True, 0, 0, mask_bm, color_bm)
        hicon = self._user32.CreateIconIndirect(ctypes.byref(info))
        self._gdi32.DeleteObject(color_bm)
        self._gdi32.DeleteObject(mask_bm)
        return hicon

    def balloon(self, title: str, text: str) -> None:
        """Dymek powiadomienia przy ikonie (kliknięcie w niego wywołuje polecenie 20)."""
        self._notify(1, (title, text))

    def _notify(self, action: int, info: Optional[tuple[str, str]] = None) -> None:
        if not self.hwnd:
            return
        if self._kind not in self._icons:
            self._icons[self._kind] = self._make_icon(self._kind)
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 1 | 2 | 4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = self.WM_TRAY
        nid.hIcon = self._icons[self._kind]
        nid.szTip = self._tip[:127]
        if info:
            nid.uFlags |= 0x10  # NIF_INFO
            nid.szInfoTitle = info[0][:63]
            nid.szInfo = info[1][:255]
            nid.dwInfoFlags = 1  # NIIF_INFO
        self._shell32.Shell_NotifyIconW(action, ctypes.byref(nid))

    # ---- komunikaty okna ------------------------------------------------
    def _on_message(self, hwnd, msg, wparam, lparam) -> int:
        try:
            if msg == self.WM_TRAY:
                event = lparam & 0xFFFF
                if event == 0x0202:    # WM_LBUTTONUP
                    self._on_command(1)
                elif event == 0x0205:  # WM_RBUTTONUP
                    self._popup(hwnd)
                elif event == 0x0405:  # NIN_BALLOONUSERCLICK
                    self._on_command(20)
                return 0
            if msg == 0x0011:          # WM_QUERYENDSESSION - nie blokujemy wyłączania
                return 1
            if msg == 0x0016:          # WM_ENDSESSION
                if wparam:
                    self._on_session_end()
                return 0
            if msg == self._wm_taskbar:  # Explorer się zrestartował - dodaj ikonę ponownie
                self._notify(0)
                return 0
            if msg == 0x0002:          # WM_DESTROY
                self._user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001
            pass
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _popup(self, hwnd) -> None:
        u = self._user32
        menu = u.CreatePopupMenu()
        for item_id, label, checked in self._menu_provider():
            if item_id == 0:
                u.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
            else:
                u.AppendMenuW(menu, 0x0 | (0x8 if checked else 0), item_id, label)  # MF_STRING | MF_CHECKED
        pt = wintypes.POINT()
        u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(hwnd)
        cmd = u.TrackPopupMenu(menu, 0x0100 | 0x0002, pt.x, pt.y, 0, hwnd, None)  # RETURNCMD | RIGHTBUTTON
        u.PostMessageW(hwnd, 0x0000, 0, 0)
        u.DestroyMenu(menu)
        if cmd:
            self._on_command(cmd)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

COLORS = {
    "ok": "#1b7f3b",
    "warn": "#b36b00",
    "err": "#b00020",
    "muted": "#666666",
}


def normalize_network_id(text: str) -> Optional[str]:
    t = text.strip().lower().replace(" ", "")
    if len(t) == 16 and all(c in "0123456789abcdef" for c in t):
        return t
    return None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(640, 300)
        self.geometry("760x380")

        self.client: Optional[ZeroTierClient] = None
        self.networks: dict[str, Network] = {}
        self.node = NodeStatus()
        self._results: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._refresh_job: Optional[str] = None
        self._busy = False
        self._sticky_until = 0.0  # do kiedy nie nadpisywać komunikatu po akcji

        self.config = Config.load()
        self._forgotten: set[str] = set()   # sieci właśnie usunięte z listy
        self._startup_done = False
        self.tray: Optional[TrayIcon] = None
        self._tray_nets: list[tuple[str, str, bool]] = []  # (id, etykieta, połączona)

        self.auto_refresh = tk.BooleanVar(value=True)
        self.always_on_top = tk.BooleanVar(value=False)
        self.setting_vars = {k: tk.BooleanVar(value=v) for k, v in self.config.settings.items()}
        self._auto_var = tk.BooleanVar()
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self._prev_status: Optional[dict[str, str]] = None   # do powiadomień o zmianach
        self._prev_online: Optional[bool] = None
        self._pending_url = ""                               # co otworzyć po kliknięciu w dymek

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._connect()
        self.after(100, self._poll_results)
        self._start_tray()

    # ---- budowa interfejsu --------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            if IS_WINDOWS:
                style.theme_use("vista")
        except tk.TclError:
            pass

        # Menu "Opcje"
        menubar = tk.Menu(self)
        opts = tk.Menu(menubar, tearoff=0)
        for key, label in (
            ("disconnect_on_exit", "Rozłączaj sieci bez autoconnect przy wyłączaniu komputera / zamykaniu programu"),
            ("disconnect_on_start", "Przy starcie programu rozłącz sieci bez autoconnect"),
            ("connect_on_start", "Przy starcie programu połącz sieci z autoconnect"),
            ("close_to_tray", "Przycisk X chowa okno do zasobnika (ikona przy zegarku)"),
            ("notifications", "Powiadomienia z zasobnika o zmianach stanu sieci"),
        ):
            opts.add_checkbutton(label=label, variable=self.setting_vars[key], command=self._save_settings)
        if IS_WINDOWS:
            opts.add_checkbutton(label="Uruchamiaj program z Windows (od razu w zasobniku)",
                                 variable=self.autostart_var, command=self._toggle_autostart)
        opts.add_separator()
        opts.add_command(label="Zakończ program", command=self._quit)
        menubar.add_cascade(label="Opcje", menu=opts)
        self.profiles_menu = tk.Menu(menubar, tearoff=0, postcommand=self._rebuild_profiles_menu)
        menubar.add_cascade(label="Profile", menu=self.profiles_menu)
        self.config_menu = menubar
        self.configure(menu=menubar)

        # Pasek statusu węzła
        top =ttk.Frame(self, padding=(8, 6, 8, 0))
        top.pack(fill="x")
        self.node_label = ttk.Label(top, text="Łączenie z usługą ZeroTier…", foreground=COLORS["muted"])
        self.node_label.pack(side="left")
        self.online_label = ttk.Label(top, text="", font=("TkDefaultFont", 9, "bold"))
        self.online_label.pack(side="right")

        # Tabela sieci
        cols = ("name", "id", "status", "ip", "type", "auto")
        table_frame = ttk.Frame(self, padding=(8, 6, 8, 0))
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        headings = {
            "name": ("Nazwa", 150),
            "id": ("ID sieci", 150),
            "status": ("Status", 230),
            "ip": ("Adres IP", 130),
            "type": ("Typ", 70),
            "auto": ("Auto", 50),
        }
        for key, (label, width) in headings.items():
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=50, anchor="w", stretch=(key in ("name", "status")))
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("ok", foreground=COLORS["ok"])
        self.tree.tag_configure("warn", foreground=COLORS["warn"])
        self.tree.tag_configure("err", foreground=COLORS["err"])
        self.tree.tag_configure("off", foreground=COLORS["muted"])
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", lambda e: self._show_details())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        # Menu kontekstowe
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Rozłącz", command=self._toggle_selected)
        self._menu_toggle_index = self.menu.index("end")
        self.menu.add_checkbutton(label="Autoconnect (łącz automatycznie)", variable=self._auto_var,
                                  command=self._toggle_autoconnect)
        self.menu.add_separator()
        self.menu.add_command(label="Otwórz panel sieci w przeglądarce (autoryzacja)", command=self._open_panel)
        self.menu.add_command(label="Szczegóły…", command=self._show_details)
        self.menu.add_command(label="Kopiuj ID sieci", command=lambda: self._copy_selected("id"))
        self.menu.add_command(label="Kopiuj adres IP", command=lambda: self._copy_selected("ip"))
        self.menu.add_separator()
        self._menu_opt_indices: list[int] = []
        self._opt_vars = {
            "allowManaged": tk.BooleanVar(),
            "allowGlobal": tk.BooleanVar(),
            "allowDefault": tk.BooleanVar(),
            "allowDNS": tk.BooleanVar(),
        }
        opt_labels = {
            "allowManaged": "Zezwól na zarządzane adresy (allowManaged)",
            "allowGlobal": "Zezwól na globalne adresy (allowGlobal)",
            "allowDefault": "Zezwól na trasę domyślną (allowDefault)",
            "allowDNS": "Zezwól na DNS (allowDNS)",
        }
        for key, var in self._opt_vars.items():
            self.menu.add_checkbutton(
                label=opt_labels[key], variable=var, command=lambda k=key: self._toggle_option(k)
            )
            self._menu_opt_indices.append(self.menu.index("end"))
        self.menu.add_separator()
        self.menu.add_command(label="Usuń z listy (opuść i zapomnij)", command=self._forget_selected)

        # Dolny pasek: dołączanie + przyciski
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")

        ttk.Label(bottom, text="ID sieci:").pack(side="left")
        self.join_entry = ttk.Entry(bottom, width=20)
        self.join_entry.pack(side="left", padx=(4, 4))
        self.join_entry.bind("<Return>", lambda e: self._join())
        self.join_btn = ttk.Button(bottom, text="Dołącz", command=self._join)
        self.join_btn.pack(side="left")

        self.toggle_btn = ttk.Button(bottom, text="Rozłącz", command=self._toggle_selected, state="disabled")
        self.toggle_btn.pack(side="left", padx=(8, 0))
        self.refresh_btn = ttk.Button(bottom, text="Odśwież", command=self.refresh)
        self.refresh_btn.pack(side="left", padx=(8, 0))

        # Linia komunikatów + opcje okna
        status_bar = ttk.Frame(self, padding=(8, 0, 8, 6))
        status_bar.pack(fill="x")
        self.msg_label = ttk.Label(status_bar, text="", foreground=COLORS["muted"])
        self.msg_label.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(status_bar, text="Zawsze na wierzchu", variable=self.always_on_top,
                        command=self._apply_topmost).pack(side="right")
        ttk.Checkbutton(status_bar, text=f"Auto-odświeżanie ({REFRESH_SECONDS}s)",
                        variable=self.auto_refresh, command=self._schedule_refresh).pack(side="right", padx=(0, 8))

    # ---- połączenie z usługą -----------------------------------------
    def _connect(self) -> None:
        token, checked = read_auth_token()
        if token is None:
            self._handle_missing_token(checked)
            return
        self.client = ZeroTierClient(token, read_service_port())
        self.refresh()

    def _handle_missing_token(self, checked: list[str]) -> None:
        self.node_label.config(text="Brak dostępu do tokenu ZeroTier", foreground=COLORS["err"])
        self._set_message("Nie udało się odczytać pliku authtoken.secret.", "err")
        paths = "\n".join(f"  • {p}" for p in checked)

        if IS_WINDOWS and not is_admin():
            answer = messagebox.askyesno(
                "Wymagane uprawnienia",
                "Nie można odczytać tokenu autoryzacyjnego ZeroTier.\n\n"
                "Sprawdzone lokalizacje:\n" + paths + "\n\n"
                "Plik w C:\\ProgramData wymaga uprawnień administratora. "
                "Uruchomić program jako administrator? Token zostanie wtedy jednorazowo "
                "skopiowany do Twojego profilu użytkownika, więc kolejne uruchomienia "
                "nie będą już wymagały uprawnień.",
            )
            if answer and relaunch_as_admin():
                self.destroy()
                return
        else:
            messagebox.showerror(
                "Brak tokenu",
                "Nie można odczytać tokenu autoryzacyjnego ZeroTier.\n\n"
                "Sprawdzone lokalizacje:\n" + paths + "\n\n"
                "Upewnij się, że ZeroTier One jest zainstalowany, albo ustaw zmienną "
                "środowiskową ZT_AUTHTOKEN.",
            )

    def _maybe_persist_token(self) -> None:
        """Po udanym uruchomieniu jako admin skopiuj token do profilu użytkownika."""
        if not IS_WINDOWS or not is_admin():
            return
        dest = _user_token_path()
        if dest is None or dest.is_file():
            return
        copied = copy_token_to_user_profile()
        if copied:
            self._set_message(f"Token skopiowano do {copied} – następnym razem uprawnienia admina nie będą potrzebne.", "ok")

    # ---- wątki / kolejka -----------------------------------------------
    def _run_async(self, kind: str, fn, *args) -> None:
        def worker() -> None:
            try:
                result = fn(*args)
                self._results.put((kind, result))
            except Exception as e:  # noqa: BLE001 - chcemy pokazać każdy błąd w UI
                self._results.put(("error", e))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self) -> None:
        try:
            while True:
                kind, payload = self._results.get_nowait()
                self._handle_result(kind, payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_results)

    def _handle_result(self, kind: str, payload: Any) -> None:
        if kind == "refresh":
            self._busy = False
            self.refresh_btn.config(state="normal")
            node, networks = payload
            self._apply_state(node, networks)
            self._maybe_persist_token()
            if not self._startup_done:
                self._startup_done = True
                self._run_startup()
        elif kind == "tray":
            self._tray_command(payload)
        elif kind == "action":
            self._set_message(str(payload), "ok", sticky=4.0)
            self.refresh()
        elif kind == "error":
            self._busy = False
            self.refresh_btn.config(state="normal")
            self._show_error(payload)

    def _show_error(self, err: Exception) -> None:
        if isinstance(err, AuthError):
            self.node_label.config(text="Token odrzucony przez usługę", foreground=COLORS["err"])
            self._set_message(str(err), "err")
        elif isinstance(err, ServiceUnavailable):
            self.node_label.config(text="Usługa ZeroTier One nie odpowiada", foreground=COLORS["err"])
            self.online_label.config(text="OFFLINE", foreground=COLORS["err"])
            self._set_message(str(err), "err")
            if self.tray:
                self.tray.set_state("err", "ZeroTier: usługa nie odpowiada")
        else:
            self._set_message(f"Błąd: {err}", "err", sticky=6.0)

    # ---- odświeżanie ---------------------------------------------------
    def refresh(self) -> None:
        if self.client is None or self._busy:
            return
        self._busy = True
        self.refresh_btn.config(state="disabled")

        def fetch(client: ZeroTierClient):
            return client.status(), client.networks()

        self._run_async("refresh", fetch, self.client)
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self._refresh_job is not None:
            self.after_cancel(self._refresh_job)
            self._refresh_job = None
        if self.auto_refresh.get():
            self._refresh_job = self.after(REFRESH_SECONDS * 1000, self.refresh)

    def _apply_state(self, node: NodeStatus, networks: list[Network]) -> None:
        self.node = node
        self.networks = {n.id: n for n in networks}
        self._forgotten &= set(self.networks)
        changed = False
        for n in networks:
            if n.id not in self._forgotten:
                changed |= self.config.remember(n)
        if changed:
            self.config.save()
        self._notify_changes(node, networks)

        ver = f"  ·  v{node.version}" if node.version else ""
        self.node_label.config(text=f"Adres węzła: {node.address}{ver}", foreground="")
        if node.online:
            txt = "ONLINE" + ("  (TCP fallback)" if node.tcp_fallback else "")
            self.online_label.config(text=txt, foreground=COLORS["ok"])
        else:
            self.online_label.config(text="OFFLINE", foreground=COLORS["err"])

        selected = self._selected_id()
        self.tree.delete(*self.tree.get_children())
        ids = set(self.config.networks) | (set(self.networks) - self._forgotten)

        def display_name(nid: str) -> str:
            n = self.networks.get(nid)
            return (n.name if n else "") or self.config.networks.get(nid, {}).get("name", "")

        self._tray_nets = []
        for nid in sorted(ids, key=lambda i: (display_name(i).lower(), i)):
            n = self.networks.get(nid)
            auto = "✔" if self.config.is_auto(nid) else ""
            name = display_name(nid) or "(bez nazwy)"
            if n is not None:
                ip = ", ".join(a.split("/")[0] for a in n.addresses) or "—"
                tag = "ok" if n.connected else ("warn" if n.status == "REQUESTING_CONFIGURATION" else "err")
                values = (name, nid, STATUS_LABELS.get(n.status, n.status), ip,
                          n.type.capitalize() if n.type else "", auto)
            else:
                tag = "off"
                values = (name, nid, "Rozłączono", "—", "", auto)
            self.tree.insert("", "end", iid=nid, values=values, tags=(tag,))
            self._tray_nets.append((nid, f"{name}  ({nid})", n is not None))
        if selected and selected in ids:
            self.tree.selection_set(selected)

        total = len(ids)
        ok = sum(1 for n in networks if n.connected)
        if self.tray:
            kind = "ok" if ok else ("idle" if node.online else "err")
            self.tray.set_state(kind, f"ZeroTier: {'online' if node.online else 'offline'}, połączone sieci: {ok}/{total}")
        if time.monotonic() < self._sticky_until:
            pass  # zostaw komunikat o ostatniej akcji
        elif total == 0:
            self._set_message("Brak sieci. Wpisz ID sieci poniżej i kliknij „Dołącz”.", "muted")
        else:
            self._set_message(f"Sieci: {total}, połączone: {ok}", "muted")
        self._update_buttons()

    def _net_name(self, nid: str) -> str:
        n = self.networks.get(nid)
        return (n.name if n else "") or self.config.networks.get(nid, {}).get("name", "") or nid

    def _panel_url(self, nid: str) -> str:
        n = self.networks.get(nid)
        if n and n.status == "AUTHENTICATION_REQUIRED" and n.auth_url:
            return n.auth_url
        return f"https://my.zerotier.com/network/{nid}"

    def _notify_changes(self, node: NodeStatus, networks: list[Network]) -> None:
        """Dymki z zasobnika przy zmianie stanu (pierwszy odczyt po starcie jest pomijany)."""
        status = {n.id: n.status for n in networks}
        prev, self._prev_status = self._prev_status, status
        prev_online, self._prev_online = self._prev_online, node.online
        if prev is None:
            return
        events: list[tuple[str, str]] = []
        for nid, st in status.items():
            old = prev.get(nid)
            if st == old:
                continue
            name = self._net_name(nid)
            if st == "OK":
                events.append(("Połączono", f"Sieć {name} jest połączona."))
            elif st in ("ACCESS_DENIED", "AUTHENTICATION_REQUIRED"):
                events.append(("Wymagana autoryzacja", f"Sieć {name} czeka na autoryzację – kliknij, by otworzyć panel."))
                self._pending_url = self._panel_url(nid)
            elif old == "OK":
                events.append(("Utracono połączenie", f"Sieć {name}: {STATUS_LABELS.get(st, st)}."))
        for nid, old in prev.items():
            if nid not in status and old == "OK":
                events.append(("Rozłączono", f"Sieć {self._net_name(nid)} została rozłączona."))
        if prev_online is not None and node.online != prev_online:
            events.append(("Węzeł ONLINE" if node.online else "Węzeł OFFLINE",
                           "Odzyskano łączność z siecią ZeroTier." if node.online else "Brak łączności z Internetem."))
        if events and self.tray and self.config.settings["notifications"]:
            if len(events) == 1:
                title, text = events[0]
            else:
                title, text = "ZeroTier", "\n".join(f"{t}: {x}" for t, x in events[:4])
            self.tray.balloon("ZeroTier – " + title if len(events) == 1 else title, text)

    def _open_panel(self) -> None:
        nid = self._selected_id()
        if nid:
            webbrowser.open(self._panel_url(nid))

    def _toggle_autostart(self) -> None:
        try:
            set_autostart(self.autostart_var.get())
        except OSError as e:
            self.autostart_var.set(autostart_enabled())
            messagebox.showerror("Autostart", f"Nie udało się zmienić autostartu: {e}")

    # ---- profile -------------------------------------------------------
    def _rebuild_profiles_menu(self) -> None:
        m = self.profiles_menu
        m.delete(0, "end")
        m.add_command(label="Zapisz bieżące połączenia jako profil…", command=self._save_profile)
        names = sorted(self.config.profiles)
        if names:
            m.add_separator()
            for name in names:
                count = len(self.config.profiles[name])
                m.add_command(label=f"Przełącz na: {name}  ({count})",
                              command=lambda nm=name: self._apply_profile(nm))
            m.add_separator()
            for name in names:
                m.add_command(label=f"Usuń profil: {name}", command=lambda nm=name: self._delete_profile(nm))

    def _save_profile(self) -> None:
        connected = sorted(self.networks)
        if not connected:
            messagebox.showinfo("Profil", "Brak aktywnych sieci – najpierw połącz sieci, które mają wejść do profilu.")
            return
        name = simpledialog.askstring("Nowy profil", f"Nazwa profilu (sieci: {len(connected)}):", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name in self.config.profiles and not messagebox.askyesno("Profil", f"Profil „{name}” istnieje. Nadpisać?"):
            return
        self.config.profiles[name] = connected
        self.config.save()
        self._set_message(f"Zapisano profil „{name}”.", "ok", sticky=3.0)

    def _apply_profile(self, name: str) -> None:
        if self.client is None or name not in self.config.profiles:
            return
        self._run_async("action", job_apply_profile, self.client, self.config, name)

    def _delete_profile(self, name: str) -> None:
        if messagebox.askyesno("Profil", f"Usunąć profil „{name}”?"):
            self.config.profiles.pop(name, None)
            self.config.save()

    def _set_message(self, text: str, level: str = "muted", sticky: float = 0.0) -> None:
        if len(text) > 90:
            text = text[:87] + "…"
        self.msg_label.config(text=text, foreground=COLORS.get(level, COLORS["muted"]))
        self._sticky_until = time.monotonic() + sticky

    def _update_buttons(self) -> None:
        nid = self._selected_id()
        self.toggle_btn.config(
            state="normal" if nid else "disabled",
            text="Rozłącz" if nid in self.networks else "Połącz",
        )

    def _apply_topmost(self) -> None:
        self.attributes("-topmost", self.always_on_top.get())

    # ---- akcje użytkownika --------------------------------------------
    def _selected_id(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _selected_network(self) -> Optional[Network]:
        nid = self._selected_id()
        return self.networks.get(nid) if nid else None

    def _join(self) -> None:
        if self.client is None:
            return
        nid = normalize_network_id(self.join_entry.get())
        if nid is None:
            messagebox.showwarning("Niepoprawne ID", "ID sieci ZeroTier to 16 znaków szesnastkowych, np. 8056c2e21c000001.")
            return
        self.join_entry.delete(0, "end")
        self._forgotten.discard(nid)
        self.config.networks.setdefault(nid, {"autoconnect": False})
        self.config.save()
        self._set_message(f"Dołączanie do {nid}…", "muted")
        self._run_async("action", lambda c, i: (c.join(i), f"Wysłano żądanie dołączenia do {i}.")[1], self.client, nid)

    def _toggle_selected(self) -> None:
        """Połącz / rozłącz zaznaczoną sieć (rozłączona zostaje na liście)."""
        nid = self._selected_id()
        if nid is None or self.client is None:
            return
        self._toggle_network(nid)

    def _toggle_network(self, nid: str) -> None:
        if self.client is None:
            return
        job = job_disconnect if nid in self.networks else job_connect
        self._run_async("action", job, self.client, self.config, nid)

    def _forget_selected(self) -> None:
        nid = self._selected_id()
        if nid is None or self.client is None:
            return
        label = self.config.networks.get(nid, {}).get("name") or nid
        if not messagebox.askyesno("Usunąć sieć z listy?",
                                   f"Opuścić sieć „{label}” ({nid}) i usunąć ją z listy wraz z zapamiętanymi ustawieniami?"):
            return
        was_active = nid in self.networks
        self._forgotten.add(nid)
        self.config.networks.pop(nid, None)
        self.config.save()
        if was_active:
            self._run_async("action", lambda c, i: (c.leave(i), f"Usunięto sieć {i}.")[1], self.client, nid)
        else:
            self._apply_state(self.node, list(self.networks.values()))

    def _toggle_autoconnect(self) -> None:
        nid = self._selected_id()
        if nid is None:
            return
        self.config.networks.setdefault(nid, {})["autoconnect"] = self._auto_var.get()
        self.config.save()
        self._apply_state(self.node, list(self.networks.values()))
        self._set_message(f"Autoconnect {'włączony' if self._auto_var.get() else 'wyłączony'} dla {nid}.", "ok", sticky=3.0)

    def _save_settings(self) -> None:
        for key, var in self.setting_vars.items():
            self.config.settings[key] = bool(var.get())
        self.config.save()

    # ---- start programu, zasobnik, zamykanie ---------------------------
    def _run_startup(self) -> None:
        s = self.config.settings
        if self.client is None or not (s["disconnect_on_start"] or s["connect_on_start"]):
            return
        self._run_async("action", job_startup, self.client, self.config,
                        s["disconnect_on_start"], s["connect_on_start"])

    def _start_tray(self) -> None:
        if not IS_WINDOWS or self.client is None:
            return
        tray = TrayIcon(self._tray_menu, lambda cmd: self._results.put(("tray", cmd)),
                        self._on_session_end, "ZeroTier")
        if tray.start():
            self.tray = tray
            if "--minimized" in sys.argv:
                self.withdraw()

    def _tray_menu(self) -> list:
        """Wołane z wątku zasobnika - czyta tylko gotową migawkę, bez Tkintera."""
        items: list = [(1, "Pokaż okno", False), (0, "", False)]
        for i, (_nid, label, on) in enumerate(self._tray_nets):
            items.append((100 + i, label, on))
        if self._tray_nets:
            items.append((0, "", False))
        profiles = sorted(self.config.profiles)
        for i, name in enumerate(profiles):
            items.append((200 + i, f"Profil: {name}", False))
        if profiles:
            items.append((0, "", False))
        items += [(2, "Rozłącz wszystkie sieci", False), (3, "Połącz sieci z autoconnect", False),
                  (0, "", False), (9, "Zakończ", False)]
        return items

    def _tray_command(self, cmd: int) -> None:
        if cmd == 1:
            self.deiconify()
            self.lift()
            self.focus_force()
        elif cmd == 2 and self.client:
            self._run_async("action", job_disconnect_all, self.client, self.config)
        elif cmd == 3 and self.client:
            self._run_async("action", job_connect_auto, self.client, self.config)
        elif cmd == 9:
            self._quit()
        elif cmd == 20:
            if self._pending_url:
                webbrowser.open(self._pending_url)
        elif cmd >= 200:
            profiles = sorted(self.config.profiles)
            if cmd - 200 < len(profiles):
                self._apply_profile(profiles[cmd - 200])
        elif cmd >= 100 and cmd - 100 < len(self._tray_nets):
            self._toggle_network(self._tray_nets[cmd - 100][0])

    def _on_session_end(self) -> None:
        """Wyłączanie / wylogowanie Windows (wątek zasobnika, bez Tkintera)."""
        if self.config.settings["disconnect_on_exit"] and self.client is not None:
            try:
                disconnect_for_exit(self.client, self.config)
            except Exception:  # noqa: BLE001
                pass

    def _on_close(self) -> None:
        if self.tray and self.config.settings["close_to_tray"]:
            self.withdraw()
        else:
            self._quit()

    def _quit(self) -> None:
        if self.config.settings["disconnect_on_exit"] and self.client is not None:
            try:
                disconnect_for_exit(self.client, self.config)
            except Exception:  # noqa: BLE001
                pass
        self.config.save()
        if self.tray:
            self.tray.stop()
        self.destroy()

    def _toggle_option(self, key: str) -> None:
        n = self._selected_network()
        if n is None or self.client is None:
            return
        value = self._opt_vars[key].get()
        if key == "allowDefault" and value and not messagebox.askyesno(
            "Uwaga: trasa domyślna",
            f"Włączenie allowDefault dla sieci „{n.name or n.id}” skieruje CAŁY ruch internetowy tego komputera "
            "przez tę sieć ZeroTier (jeśli jej kontroler ma trasę 0.0.0.0/0).\n\n"
            "Gdy węzeł-wyjście jest wyłączony lub źle skonfigurowany, możesz stracić dostęp do Internetu.\n\n"
            "Czy na pewno włączyć?",
            icon="warning",
        ):
            self._opt_vars[key].set(False)
            return
        self._run_async(
            "action",
            lambda c, i, k, v: (c.set_option(i, k, v), f"Ustawiono {k}={'tak' if v else 'nie'} dla {i}.")[1],
            self.client, n.id, key, value,
        )

    def _copy_selected(self, what: str) -> None:
        nid = self._selected_id()
        if nid is None:
            return
        n = self.networks.get(nid)
        text = nid if what == "id" else (", ".join(a.split("/")[0] for a in n.addresses) if n else "")
        if not text:
            self._set_message("Brak adresu IP do skopiowania.", "warn")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._set_message(f"Skopiowano: {text}", "ok")

    def _show_context_menu(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        n = self.networks.get(row)
        if n:
            self._opt_vars["allowManaged"].set(n.allow_managed)
            self._opt_vars["allowGlobal"].set(n.allow_global)
            self._opt_vars["allowDefault"].set(n.allow_default)
            self._opt_vars["allowDNS"].set(n.allow_dns)
        for idx in self._menu_opt_indices:
            self.menu.entryconfigure(idx, state="normal" if n else "disabled")
        self.menu.entryconfigure(self._menu_toggle_index, label="Rozłącz" if n else "Połącz")
        self._auto_var.set(self.config.is_auto(row))
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _show_details(self) -> None:
        n = self._selected_network()
        if n is None:
            if self._selected_id():
                self._set_message("Sieć jest rozłączona – szczegóły dostępne po połączeniu.", "warn", sticky=3.0)
            return
        yn =lambda v: "tak" if v else "nie"  # noqa: E731
        rows = [
            ("Nazwa", n.name or "(bez nazwy)"),
            ("ID sieci", n.id),
            ("Status", f"{STATUS_LABELS.get(n.status, n.status)}  [{n.status}]"),
            ("Typ", n.type.capitalize()),
            ("Adresy", "\n".join(n.addresses) or "—"),
            ("Interfejs", n.device or "—"),
            ("MAC", n.mac or "—"),
            ("MTU", str(n.mtu) if n.mtu else "—"),
            ("Trasy zarządzane", str(n.routes)),
            ("allowManaged", yn(n.allow_managed)),
            ("allowGlobal", yn(n.allow_global)),
            ("allowDefault", yn(n.allow_default)),
            ("allowDNS", yn(n.allow_dns)),
        ]
        if n.auth_url:
            rows.append(("Logowanie SSO", n.auth_url))
        DetailsDialog(self, f"Sieć {n.name or n.id}", rows)


class DetailsDialog(tk.Toplevel):
    """Okno ze szczegółami sieci: siatka etykieta → wartość (wartości można zaznaczyć i skopiować)."""

    def __init__(self, parent: tk.Tk, title: str, rows: list[tuple[str, str]]) -> None:
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        grid = ttk.Frame(self, padding=12)
        grid.pack(fill="both", expand=True)
        for r, (key, value) in enumerate(rows):
            ttk.Label(grid, text=key + ":", foreground=COLORS["muted"]).grid(row=r, column=0, sticky="ne", padx=(0, 10), pady=1)
            lines = value.count("\n") + 1
            box = tk.Text(grid, height=lines, width=max(20, min(60, max(len(v) for v in value.split("\n")) + 2)),
                          relief="flat", borderwidth=0, highlightthickness=0, font="TkDefaultFont")
            box.insert("1.0", value)
            box.config(state="disabled", background=self.cget("background"))
            box.grid(row=r, column=1, sticky="w", pady=1)
        ttk.Button(self, text="Zamknij", command=self.destroy).pack(pady=(0, 10))
        self.bind("<Escape>", lambda e: self.destroy())
        # wyśrodkuj względem okna głównego
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_set()


# --------------------------------------------------------------------------


def main() -> None:
    if IS_WINDOWS:
        # Ostre renderowanie na ekranach HiDPI
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        except Exception:
            pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
