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
from tkinter import messagebox, ttk
from typing import Any, Optional

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
    def join(self, network_id: str) -> None:
        self._request("POST", f"/network/{network_id}", {})

    def leave(self, network_id: str) -> None:
        self._request("DELETE", f"/network/{network_id}")

    def set_option(self, network_id: str, key: str, value: bool) -> None:
        self._request("POST", f"/network/{network_id}", {key: value})


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

        self.auto_refresh = tk.BooleanVar(value=True)
        self.always_on_top = tk.BooleanVar(value=False)

        self._build_ui()
        self._connect()
        self.after(100, self._poll_results)

    # ---- budowa interfejsu --------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            if IS_WINDOWS:
                style.theme_use("vista")
        except tk.TclError:
            pass

        # Pasek statusu węzła
        top = ttk.Frame(self, padding=(8, 6, 8, 0))
        top.pack(fill="x")
        self.node_label = ttk.Label(top, text="Łączenie z usługą ZeroTier…", foreground=COLORS["muted"])
        self.node_label.pack(side="left")
        self.online_label = ttk.Label(top, text="", font=("TkDefaultFont", 9, "bold"))
        self.online_label.pack(side="right")

        # Tabela sieci
        cols = ("name", "id", "status", "ip", "type")
        table_frame = ttk.Frame(self, padding=(8, 6, 8, 0))
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", selectmode="browse")
        headings = {
            "name": ("Nazwa", 150),
            "id": ("ID sieci", 150),
            "status": ("Status", 230),
            "ip": ("Adres IP", 130),
            "type": ("Typ", 70),
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
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", lambda e: self._show_details())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

        # Menu kontekstowe
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="Szczegóły…", command=self._show_details)
        self.menu.add_command(label="Kopiuj ID sieci", command=lambda: self._copy_selected("id"))
        self.menu.add_command(label="Kopiuj adres IP", command=lambda: self._copy_selected("ip"))
        self.menu.add_separator()
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
        self.menu.add_separator()
        self.menu.add_command(label="Opuść sieć", command=self._leave_selected)

        # Dolny pasek: dołączanie + przyciski
        bottom = ttk.Frame(self, padding=8)
        bottom.pack(fill="x")

        ttk.Label(bottom, text="ID sieci:").pack(side="left")
        self.join_entry = ttk.Entry(bottom, width=20)
        self.join_entry.pack(side="left", padx=(4, 4))
        self.join_entry.bind("<Return>", lambda e: self._join())
        self.join_btn = ttk.Button(bottom, text="Dołącz", command=self._join)
        self.join_btn.pack(side="left")

        self.leave_btn = ttk.Button(bottom, text="Opuść", command=self._leave_selected, state="disabled")
        self.leave_btn.pack(side="left", padx=(8, 0))
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

        ver = f"  ·  v{node.version}" if node.version else ""
        self.node_label.config(text=f"Adres węzła: {node.address}{ver}", foreground="")
        if node.online:
            txt = "ONLINE" + ("  (TCP fallback)" if node.tcp_fallback else "")
            self.online_label.config(text=txt, foreground=COLORS["ok"])
        else:
            self.online_label.config(text="OFFLINE", foreground=COLORS["err"])

        selected = self._selected_id()
        self.tree.delete(*self.tree.get_children())
        for n in sorted(networks, key=lambda x: (x.name.lower(), x.id)):
            ip = ", ".join(a.split("/")[0] for a in n.addresses) or "—"
            tag = "ok" if n.connected else ("warn" if n.status == "REQUESTING_CONFIGURATION" else "err")
            self.tree.insert(
                "",
                "end",
                iid=n.id,
                values=(n.name or "(bez nazwy)", n.id, STATUS_LABELS.get(n.status, n.status), ip,
                        n.type.capitalize() if n.type else ""),
                tags=(tag,),
            )
        if selected and selected in self.networks:
            self.tree.selection_set(selected)

        total = len(networks)
        ok = sum(1 for n in networks if n.connected)
        if time.monotonic() < self._sticky_until:
            pass  # zostaw komunikat o ostatniej akcji
        elif total == 0:
            self._set_message("Brak sieci. Wpisz ID sieci poniżej i kliknij „Dołącz”.", "muted")
        else:
            self._set_message(f"Sieci: {total}, połączone: {ok}", "muted")
        self._update_buttons()

    def _set_message(self, text: str, level: str = "muted", sticky: float = 0.0) -> None:
        if len(text) > 90:
            text = text[:87] + "…"
        self.msg_label.config(text=text, foreground=COLORS.get(level, COLORS["muted"]))
        self._sticky_until = time.monotonic() + sticky

    def _update_buttons(self) -> None:
        self.leave_btn.config(state="normal" if self._selected_id() else "disabled")

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
        self._set_message(f"Dołączanie do {nid}…", "muted")
        self._run_async("action", lambda c, i: (c.join(i), f"Wysłano żądanie dołączenia do {i}.")[1], self.client, nid)

    def _leave_selected(self) -> None:
        n = self._selected_network()
        if n is None or self.client is None:
            return
        label = n.name or n.id
        if not messagebox.askyesno("Opuścić sieć?", f"Czy na pewno opuścić sieć „{label}” ({n.id})?"):
            return
        self._run_async("action", lambda c, i: (c.leave(i), f"Opuszczono sieć {i}.")[1], self.client, n.id)

    def _toggle_option(self, key: str) -> None:
        n = self._selected_network()
        if n is None or self.client is None:
            return
        value = self._opt_vars[key].get()
        self._run_async(
            "action",
            lambda c, i, k, v: (c.set_option(i, k, v), f"Ustawiono {k}={'tak' if v else 'nie'} dla {i}.")[1],
            self.client, n.id, key, value,
        )

    def _copy_selected(self, what: str) -> None:
        n = self._selected_network()
        if n is None:
            return
        text = n.id if what == "id" else ", ".join(a.split("/")[0] for a in n.addresses)
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
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _show_details(self) -> None:
        n = self._selected_network()
        if n is None:
            return
        yn = lambda v: "tak" if v else "nie"  # noqa: E731
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
