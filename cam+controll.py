import cv2
import time
import threading
import json
import os
import math
import multiprocessing as mp
import queue
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from PIL import Image, ImageTk

try:
    import keyboard
    HAS_KEYBOARD = True
except Exception:
    HAS_KEYBOARD = False

from stend_discovery import (
    apply_layout,
    current_gateway,
    discover_stend,
    layout_from_manual_host,
    reset_manual_stend_mode,
    set_manual_stend_mode,
)


# =============================================================================
# FFMPEG / RTSP настройки
# =============================================================================
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"


# =============================================================================
# Надёжный TCP-канал (ReliableLink и пр.)
# =============================================================================
USE_CMD_ID_PROTOCOL = True
COMPAT_ANY_ACK = True
ONE_SHOT_SERVER = True
DEFAULT_TIMEOUT = 12.0
KEEPALIVE_TIMEOUT = 1.5
DEFAULT_RETRIES = 5
BASE_BACKOFF = 0.35
MAX_BACKOFF = 4.0


def _new_cmd_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class _Job:
    payload: str
    cmd_id: str
    timeout: float
    retries: int
    coalesce_key: Optional[str]
    done: threading.Event = field(default_factory=threading.Event)
    success: bool = False
    response: str = ""
    error: str = ""
    callback: Optional[Callable[[bool, str], None]] = None


class ReliableLink:
    """Один канал к хосту: очередь → connect/reuse → sendall → ACK → retry."""

    def __init__(
        self,
        host: str,
        port: int,
        name: str = "link",
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        one_shot: Optional[bool] = None,
    ):
        self.host = host
        self.port = port
        self.name = name
        self.timeout = timeout
        self.retries = retries
        self.one_shot = ONE_SHOT_SERVER if one_shot is None else one_shot

        self._sock: Optional[socket.socket] = None
        self._io_lock = threading.RLock()
        self._q: queue.Queue = queue.Queue()
        self._coalesce: dict = {}
        self._coalesce_lock = threading.Lock()
        self._closed = False

        self._worker = threading.Thread(
            target=self._worker_loop, name=f"ReliableLink-{name}", daemon=True
        )
        self._worker.start()

    def send(
        self,
        payload: str,
        *,
        wait: bool = True,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        coalesce_key: Optional[str] = None,
        cmd_id: Optional[str] = None,
        callback: Optional[Callable[[bool, str], None]] = None,
    ) -> bool:
        if self._closed:
            return False

        job = _Job(
            payload=payload,
            cmd_id=cmd_id or _new_cmd_id(),
            timeout=self.timeout if timeout is None else timeout,
            retries=self.retries if retries is None else retries,
            coalesce_key=coalesce_key,
            callback=callback,
        )

        wait_event: Optional[threading.Event] = None
        wait_job: Optional[_Job] = None
        enqueue = True

        if coalesce_key:
            with self._coalesce_lock:
                old = self._coalesce.get(coalesce_key)
                if old is not None and not old.done.is_set():
                    old.payload = payload
                    old.cmd_id = job.cmd_id
                    old.timeout = job.timeout
                    old.retries = job.retries
                    old.callback = callback or old.callback
                    enqueue = False
                    if wait:
                        wait_event = old.done
                        wait_job = old
                else:
                    self._coalesce[coalesce_key] = job
                    if wait:
                        wait_event = job.done
                        wait_job = job
        elif wait:
            wait_event = job.done
            wait_job = job

        if enqueue:
            self._q.put(job)

        if wait_event is not None and wait_job is not None:
            wait_event.wait()
            return wait_job.success
        return True

    def keepalive(self, payload: str = "ONLINE") -> bool:
        return self.send(
            payload,
            wait=True,
            timeout=KEEPALIVE_TIMEOUT,
            retries=1,
            coalesce_key=f"{self.name}:keepalive",
        )

    def close(self) -> None:
        self._closed = True
        self._q.put(None)
        with self._io_lock:
            self._close_sock()

    def _worker_loop(self) -> None:
        while not self._closed:
            job = self._q.get()
            if job is None:
                break
            try:
                self._execute_job(job)
            finally:
                if job.coalesce_key:
                    with self._coalesce_lock:
                        if self._coalesce.get(job.coalesce_key) is job:
                            self._coalesce.pop(job.coalesce_key, None)
                job.done.set()
                if job.callback:
                    try:
                        job.callback(job.success, job.response)
                    except Exception as e:
                        print(f"[{self.name}] callback error: {e}")

    def _execute_job(self, job: _Job) -> None:
        delay = BASE_BACKOFF
        last_err = ""
        for attempt in range(1, job.retries + 1):
            ok, resp, err = self._attempt(job.payload, job.cmd_id, job.timeout)
            if ok:
                job.success = True
                job.response = resp
                if attempt > 1:
                    print(f"[{self.name}] OK после попытки {attempt}: {job.payload!r}")
                return
            last_err = err
            print(
                f"[{self.name}] попытка {attempt}/{job.retries} "
                f"не удалась ({job.payload!r}): {err}"
            )
            with self._io_lock:
                self._close_sock()
            if attempt < job.retries:
                time.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)

        job.success = False
        job.error = last_err
        job.response = ""
        print(f"[{self.name}] команда окончательно не доставлена: {job.payload!r}")

    def _attempt(self, payload: str, cmd_id: str, timeout: float):
        with self._io_lock:
            try:
                sock = self._ensure_connected(timeout)
                wire = self._encode(payload, cmd_id)
                sock.settimeout(timeout)
                sock.sendall(wire)
                raw = sock.recv(1024)
                if not raw:
                    self._close_sock()
                    return False, "", "пустой ответ / соединение закрыто"
                text = raw.decode(errors="replace").strip()
                if self._is_ack(text, cmd_id):
                    if self.one_shot:
                        self._close_sock()
                    return True, text, ""
                self._close_sock()
                return False, text, f"неожиданный ACK: {text!r}"
            except Exception as e:
                self._close_sock()
                return False, "", str(e)

    def _encode(self, payload: str, cmd_id: str) -> bytes:
        body = payload.strip()
        if USE_CMD_ID_PROTOCOL and body.upper() != "ONLINE":
            line = f"{body} id={cmd_id}\n"
        else:
            line = f"{body}\n"
        return line.encode()

    def _is_ack(self, text: str, cmd_id: str) -> bool:
        upper = text.upper()
        if upper.startswith("OK") or upper.startswith("DUP"):
            if cmd_id and cmd_id in text:
                return True
            parts = text.replace("|", " ").split()
            if len(parts) >= 2 and parts[1] and parts[1] != cmd_id:
                return False
            return True
        if COMPAT_ANY_ACK and text:
            return True
        return False

    def _ensure_connected(self, timeout: float) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        self._enable_keepalive(sock)
        sock.connect((self.host, self.port))
        self._sock = sock
        print(f"[{self.name}] подключено к {self.host}:{self.port}")
        return sock

    def set_endpoint(self, host: str, port: int) -> None:
        with self._io_lock:
            self.host = host
            self.port = port
            self._close_sock()
        print(f"[{self.name}] endpoint → {host}:{port}")

    def _close_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    @staticmethod
    def _enable_keepalive(sock: socket.socket) -> None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except OSError:
                pass
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            try:
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 3000))
            except OSError:
                pass


# --- сеть ---
WAN_HOST = "37.9.243.135"
LOCAL_RASB1_HOST = "192.168.8.21"
LOCAL_RASB2_HOST = "192.168.8.20"
LOCAL_STEND_HOST = "192.168.8.21"
PORT_RASB1 = 12345
PORT_RASB2 = 12346
PORT_STEND = 12345

hostname = WAN_HOST
hostname_local_rasb_1 = LOCAL_RASB1_HOST
hostname_local_rasb_2 = LOCAL_RASB2_HOST
port = PORT_RASB1
port2 = PORT_RASB2

use_wan = True
use_unified_stend = True

ARDUINO_BASE = "http://192.168.8.54"
ARDUINO_TIMEOUT = 0.35
VOLTAGE_CRITICAL_V = 21.0


def needs_rasb2() -> bool:
    return not use_unified_stend


link_rasb1 = ReliableLink(WAN_HOST, PORT_RASB1, name="rasb1", one_shot=False)
link_rasb2 = ReliableLink(WAN_HOST, PORT_RASB2, name="rasb2", one_shot=False)
link_stend = ReliableLink(WAN_HOST, PORT_STEND, name="stend", one_shot=False)


def link_motor():
    return link_stend if use_unified_stend else link_rasb1


def link_elevator():
    return link_stend if use_unified_stend else link_rasb2


def current_endpoints():
    if use_unified_stend:
        host = WAN_HOST if use_wan else LOCAL_STEND_HOST
        return (host, PORT_STEND), (host, PORT_STEND)
    if use_wan:
        return (WAN_HOST, PORT_RASB1), (WAN_HOST, PORT_RASB2)
    return (LOCAL_RASB1_HOST, PORT_RASB1), (LOCAL_RASB2_HOST, PORT_RASB2)


def refresh_local_stend(*, progress=None) -> bool:
    layout = discover_stend(
        quick_hosts=(LOCAL_RASB1_HOST, LOCAL_RASB2_HOST, LOCAL_STEND_HOST),
        progress=progress,
    )
    if not layout:
        return False
    apply_layout(layout, globals(), respect_manual_mode=True)
    return True


def apply_manual_stend_ip(host: str) -> bool:
    layout = layout_from_manual_host(host)
    if not layout:
        print(f"Автопоиск: некорректный IP {host!r}")
        return False
    apply_layout(layout, globals(), respect_manual_mode=True)
    print(f"Автопоиск (вручную): {layout.detail}")
    return True


def prompt_manual_stend_ip() -> bool:
    gateway = current_gateway()
    hint = f"\nТекущий шлюз: {gateway}" if gateway else ""
    parent = _filter_ui.get("root")
    kwargs = {}
    if parent is not None:
        kwargs["parent"] = parent
    text = simpledialog.askstring(
        "Стенд не найден",
        "Автопоиск LAN ничего не нашёл."
        f"{hint}\nВведите IP-адрес стенда (например 192.168.0.20):",
        initialvalue=LOCAL_STEND_HOST,
        **kwargs,
    )
    if not text:
        print("Автопоиск: IP не введён")
        return False
    return apply_manual_stend_ip(text)


def apply_network_mode(*, prompt_if_missing=False):
    if not use_wan:
        found = refresh_local_stend()
        if not found and prompt_if_missing:
            prompt_manual_stend_ip()
    ep1, ep2 = current_endpoints()
    if use_unified_stend:
        link_stend.set_endpoint(*ep1)
    else:
        link_rasb1.set_endpoint(*ep1)
        link_rasb2.set_endpoint(*ep2)
    net = "ИНТЕРНЕТ" if use_wan else "ЛОКАЛЬ"
    stend = "StendRasb2" if use_unified_stend else "server3+serverrasb2"
    print(f"Режим: {net} | стенд: {stend} | {ep1[0]}:{ep1[1]}" + (
        "" if use_unified_stend else f" / {ep2[0]}:{ep2[1]}"
    ))
    return net, ep1, ep2


def toggle_network_mode(*, prompt_if_missing=False):
    global use_wan
    use_wan = not use_wan
    if not use_wan:
        reset_manual_stend_mode()
    return apply_network_mode(prompt_if_missing=prompt_if_missing)


def toggle_stend_mode():
    global use_unified_stend
    set_manual_stend_mode()
    use_unified_stend = not use_unified_stend
    return apply_network_mode()


filtrochistki_isOn = True
periodvkl = 1
timeon = 0.5

_filter_ui = {"root": None, "on_pulse": None}


def _notify_filter_pulse(active: bool) -> None:
    root = _filter_ui.get("root")
    on_pulse = _filter_ui.get("on_pulse")
    if root is not None and on_pulse is not None:
        root.after(0, lambda a=active: on_pulse(a))


def lift(arg_lift, time_on, *, wait=False, callback=None):
    message = f"lift {arg_lift} {time_on}"
    return link_elevator().send(
        message,
        wait=wait,
        coalesce_key="lift",
        callback=callback,
    )


def set_pump(state):
    ok = link_motor().send(f"pump {state}", wait=True, coalesce_key="pump")
    print("pump ACK" if ok else "pump FAIL")
    return ok


def set_mustache(state):
    ok = link_motor().send(f"mustache {state}", wait=True, coalesce_key="mustache")
    print("mustache ACK" if ok else "mustache FAIL")
    return ok


def run_elevator_new(polozhenie):
    message = "elevator " + str(polozhenie)
    print(str(polozhenie))
    ok = link_elevator().send(
        message,
        wait=True,
        coalesce_key="elevator",
    )
    print("Server elevator:", "OK" if ok else "FAIL")
    return ok


def Start_filtr_ochistki():
    global filtrochistki_isOn, periodvkl, timeon

    while True:
        if filtrochistki_isOn:
            try:
                interval = float(periodvkl)
                pulse = float(timeon)
            except (TypeError, ValueError):
                time.sleep(0.5)
                continue

            time.sleep(interval)
            if not filtrochistki_isOn:
                continue

            _notify_filter_pulse(True)
            link_motor().send("filter_relay 0", wait=True, coalesce_key="filter_relay")
            time.sleep(pulse)
            link_motor().send("filter_relay 1", wait=True, coalesce_key="filter_relay")
            _notify_filter_pulse(False)
            time.sleep(1)
        else:
            time.sleep(0.5)


def _arduino_get(path: str) -> Optional[dict]:
    url = ARDUINO_BASE.rstrip("/") + path
    req = Request(url, headers={"User-Agent": "AtlantClient/1.0"})
    try:
        with urlopen(req, timeout=ARDUINO_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, OSError):
        return None


def fetch_arduino_gps() -> Optional[dict]:
    return _arduino_get("/gps")


def fetch_arduino_a0() -> Optional[dict]:
    return _arduino_get("/a0")


def fetch_arduino_a1() -> Optional[dict]:
    return _arduino_get("/a1")


def Wake_On_Lan():
    while True:
        time.sleep(1)
        wake_UP()


def wake_UP():
    if use_unified_stend:
        if not link_stend.keepalive("ONLINE"):
            print("Единый стенд офлайн")
        return

    results = {}

    def ping(name, link):
        results[name] = link.keepalive("ONLINE")

    threads = [threading.Thread(target=ping, args=("rasb1", link_rasb1), daemon=True)]
    if needs_rasb2():
        threads.append(threading.Thread(target=ping, args=("rasb2", link_rasb2), daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if not results.get("rasb1"):
        print("Расбери 1 офлайн")
    if needs_rasb2() and not results.get("rasb2"):
        print("Расбери 2 офлайн")


def set_reverse_left(direction):
    ok = link_motor().send(
        f"reverse_left {direction}",
        wait=False,
        coalesce_key="reverse_left",
    )
    if not ok:
        print("Попытка установки реверса левого двигателя неудачна")


def set_reverse_right(direction):
    ok = link_motor().send(
        f"reverse_right {direction}",
        wait=False,
        coalesce_key="reverse_right",
    )
    if not ok:
        print("Попытка установки реверса правого двигателя неудачна")


def set_gear_right(arg):
    global gear
    level = int(arg)
    if level <= 0:
        gear = abs(level)
        reverse_dir = 1
    else:
        gear = level
        reverse_dir = -1
    print(gear)
    link_motor().send(
        f"reverse_right {reverse_dir}",
        wait=False,
        coalesce_key="reverse_right",
    )
    link_motor().send(
        f"gear_right {gear}",
        wait=False,
        coalesce_key="gear_right",
    )


def set_gear_left(arg):
    level = int(arg)
    if level <= 0:
        gear = abs(level)
        reverse_dir = 1
    else:
        gear = level
        reverse_dir = -1
    print(gear)
    link_motor().send(
        f"reverse_left {reverse_dir}",
        wait=False,
        coalesce_key="reverse_left",
    )
    link_motor().send(
        f"gear_left {gear}",
        wait=False,
        coalesce_key="gear_left",
    )


# =============================================================================
# Панель управления — встраиваемая в родительский фрейм
# =============================================================================

def create_control_panel(parent: tk.Misc, width: int = 420) -> tk.Frame:
    """
    Панель управления внутри parent.
    Сверху — кликабельные кнопки статусов в 2 столбца,
    под ними — блоки скоростей (квадраты) на Canvas,
    снизу — встроенная миникарта.
    """
    root = parent.winfo_toplevel()
    outer = tk.Frame(parent, bg="black", width=width)

    # ------------------------------------------------------------------
    # 1) Контейнер миникарты — пакуем СНИЗУ
    # ------------------------------------------------------------------
    minimap_holder = tk.Frame(outer, bg="#1e1e1e")
    minimap_holder.pack(side=tk.BOTTOM, fill=tk.X)
    # НЕ фиксируем высоту — пусть Frame подстроится под миникарту

    # ------------------------------------------------------------------
    # 2) Верхний контейнер: сетка кнопок + Canvas с квадратами
    # ------------------------------------------------------------------
    top = tk.Frame(outer, bg="black")
    top.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    # ---- сетка кнопок ----
    grid_frame = tk.Frame(top, bg="black", padx=4, pady=4)
    grid_frame.pack(side=tk.TOP, fill=tk.X)

    grid_frame.grid_columnconfigure(0, weight=1)
    grid_frame.grid_columnconfigure(1, weight=1)

    # ---- состояния ----
    elevator_level = 2        # 0/2 = стоп, 1 = вперёд, 3 = назад
    mustache_level = 0
    lift_level = 0
    lift_last_ok = None
    lift_busy = False
    pump_level = 0
    filter_level = 0

    filter_interval = ""
    filter_period = ""

    voltage_a0 = None
    voltage_a1 = None

    left_level = 0
    right_level = 0

    dialog_active = False

    # Цвета
    COL_ON  = "#2e7d32"   # зелёный
    COL_OFF = "#8b2020"   # красный
    COL_STOP = "#404040"  # серый
    COL_WARN = "#f9a825"  # жёлтый (импульс)
    COL_DIM = "#3a3a3a"   # нейтральный

    # Шрифт уменьшен на ~30%: было 10-11 → стало 7
    FN = ("Arial", 7, "bold")

    # ------------------------------------------------------------------
    # Кнопки-статусы
    # ------------------------------------------------------------------
    # В каждой ячейке создаём Frame с кнопкой и, опционально,
    # второй маленькой кнопкой-«стрелкой» (для элеватора и подъёмников).

    def make_cell(row, col, label):
        f = tk.Frame(grid_frame, bg="black")
        f.grid(row=row, column=col, sticky="ew", padx=2, pady=2)
        f.grid_columnconfigure(0, weight=1)
        return f

    # --- Элеватор (row 0, col 0): [ ● Элеватор (E) ] [ ▼ ]
    cell_el = make_cell(0, 0, "Элеватор")
    btn_elev = tk.Button(
        cell_el, text="● Элеватор (E)",
        font=FN, bg=COL_STOP, fg="white",
        relief="flat", padx=4, pady=3, cursor="hand2",
        command=lambda: on_elevator_key(),
    )
    btn_elev.grid(row=0, column=0, sticky="ew")
    btn_elev_back = tk.Button(
        cell_el, text="▼", font=FN, bg=COL_STOP, fg="white",
        relief="flat", padx=4, pady=3, cursor="hand2", width=2,
        command=lambda: on_elevator_key_down(),
    )
    btn_elev_back.grid(row=0, column=1, sticky="ew", padx=(2, 0))

    # --- Усы (row 0, col 1)
    cell_m = make_cell(0, 1, "Усы")
    btn_must = tk.Button(
        cell_m, text="● Усы (Y)",
        font=FN, bg=COL_OFF, fg="white",
        relief="flat", padx=4, pady=3, cursor="hand2",
        command=lambda: on_mustache_key(),
    )
    btn_must.grid(row=0, column=0, sticky="ew")

    # --- Подъёмники (row 1, col 0): [ ● Подъёмники (U) ] [ ▼ ]
    cell_l = make_cell(1, 0, "Подъёмники")
    btn_lift = tk.Button(
        cell_l, text="● Подъёмники (U)",
        font=FN, bg=COL_OFF, fg="white",
        relief="flat", padx=4, pady=3, cursor="hand2",
        command=lambda: on_lift_key(),
    )
    btn_lift.grid(row=0, column=0, sticky="ew")
    btn_lift_down = tk.Button(
        cell_l, text="▼", font=FN, bg=COL_OFF, fg="white",
        relief="flat", padx=4, pady=3, cursor="hand2", width=2,
        command=lambda: on_lift_key_down(),
    )
    btn_lift_down.grid(row=0, column=1, sticky="ew", padx=(2, 0))

    # --- Помпа (row 1, col 1)
    cell_p = make_cell(1, 1, "Помпа")
    btn_pump = tk.Button(
        cell_p, text="● Помпа (P)",
        font=FN, bg=COL_OFF, fg="white",
        relief="flat", padx=4, pady=3, cursor="hand2",
        command=lambda: on_pump_key(),
    )
    btn_pump.grid(row=0, column=0, sticky="ew")

    # --- Фильтр (row 2, col 0)
    cell_f = make_cell(2, 0, "Фильтр")
    btn_filt = tk.Button(
        cell_f, text="● Фильтр (F)",
        font=FN, bg=COL_OFF, fg="white",
        relief="flat", padx=4, pady=3, cursor="hand2",
        command=lambda: on_filter_key(),
    )
    btn_filt.grid(row=0, column=0, sticky="ew")

    # --- Напряжение A0 (row 3, col 0) — индикатор
    lbl_a0 = tk.Label(
        grid_frame, text="A0: — В", font=FN, bg=COL_DIM, fg="white",
        anchor="w", padx=6, pady=4,
    )
    lbl_a0.grid(row=3, column=0, sticky="ew", padx=2, pady=2)

    # --- Напряжение A1 (row 3, col 1) — индикатор
    lbl_a1 = tk.Label(
        grid_frame, text="A1: — В", font=FN, bg=COL_DIM, fg="white",
        anchor="w", padx=6, pady=4,
    )
    lbl_a1.grid(row=3, column=1, sticky="ew", padx=2, pady=2)

    # --- Сеть (row 4, col 0) — тумблер
    btn_net = tk.Button(
        grid_frame, text="Сеть: ЛОКАЛЬ (I)",
        font=FN, bg=COL_DIM, fg="white",
        relief="flat", padx=4, pady=4, cursor="hand2",
        command=lambda: on_network_mode_key(),
    )
    btn_net.grid(row=4, column=0, sticky="ew", padx=2, pady=2)

    # --- Стенд (row 4, col 1) — тумблер
    btn_stend = tk.Button(
        grid_frame, text="Стенд: 2 Pi (T)",
        font=FN, bg=COL_DIM, fg="white",
        relief="flat", padx=4, pady=4, cursor="hand2",
        command=lambda: on_stend_mode_key(),
    )
    btn_stend.grid(row=4, column=1, sticky="ew", padx=2, pady=2)

    # ------------------------------------------------------------------
    # 3) Canvas с блоками скоростей — уменьшенные размеры
    # ------------------------------------------------------------------
    square_size = 28          # было 40 (−30%)
    num_squares = 7
    spacing = 7

    canvas = tk.Canvas(top, bg="black", highlightthickness=0)
    canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

    def create_triangle_up(x, y, size, color, outline):
        half_size = size // 2
        points = [x + half_size, y, x, y + size, x + size, y + size]
        return canvas.create_polygon(points, fill=color, outline=outline, width=2)

    def create_triangle_down(x, y, size, color, outline):
        half_size = size // 2
        points = [x, y, x + size, y, x + half_size, y + size]
        return canvas.create_polygon(points, fill=color, outline=outline, width=2)

    # Начальные координаты блоков — зададим при первом ресайзе,
    # чтобы центрировать. Создаём с placeholder-координатами.
    left_shapes = []
    right_shapes = []
    for _ in range(num_squares):
        left_shapes.append(canvas.create_rectangle(0, 0, 0, 0, fill="gray", outline="white", width=2))
        right_shapes.append(canvas.create_rectangle(0, 0, 0, 0, fill="gray", outline="white", width=2))

    # Позиционирование блоков на Canvas
    def redraw_blocks(_event=None):
        canvas.delete("tri")
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 10 or ch < 10:
            return
        blocks_width = square_size * 2 + spacing
        start_x = max(10, (cw - blocks_width) // 2)
        start_y = 10

        # Левый блок
        for i in range(num_squares):
            y1 = start_y + i * square_size
            x1 = start_x
            x2 = x1 + square_size
            y2 = y1 + square_size
            if i == 0:
                # верх — треугольник вверх
                pts = [x1 + square_size // 2, y1, x1, y2, x2, y2]
                canvas.itemconfig(left_shapes[i], fill="")
                canvas.coords(left_shapes[i], x1, y1, x2, y2)
                tag = f"L{i}"
                canvas.delete(tag)
                canvas.create_polygon(pts, fill="gray", outline="white",
                                      width=2, tags=("tri", tag))
            elif i == num_squares - 1:
                pts = [x1, y1, x2, y1, x1 + square_size // 2, y2]
                canvas.coords(left_shapes[i], x1, y1, x2, y2)
                tag = f"L{i}"
                canvas.delete(tag)
                canvas.create_polygon(pts, fill="gray", outline="white",
                                      width=2, tags=("tri", tag))
            else:
                canvas.coords(left_shapes[i], x1, y1, x2, y2)

        # Правый блок
        for i in range(num_squares):
            y1 = start_y + i * square_size
            x1 = start_x + square_size + spacing
            x2 = x1 + square_size
            y2 = y1 + square_size
            if i == 0:
                pts = [x1 + square_size // 2, y1, x1, y2, x2, y2]
                canvas.coords(right_shapes[i], x1, y1, x2, y2)
                tag = f"R{i}"
                canvas.delete(tag)
                canvas.create_polygon(pts, fill="gray", outline="white",
                                      width=2, tags=("tri", tag))
            elif i == num_squares - 1:
                pts = [x1, y1, x2, y1, x1 + square_size // 2, y2]
                canvas.coords(right_shapes[i], x1, y1, x2, y2)
                tag = f"R{i}"
                canvas.delete(tag)
                canvas.create_polygon(pts, fill="gray", outline="white",
                                      width=2, tags=("tri", tag))
            else:
                canvas.coords(right_shapes[i], x1, y1, x2, y2)

    canvas.bind("<Configure>", redraw_blocks)

    # ------------------------------------------------------------------
    # Апдейты индикаторов
    # ------------------------------------------------------------------
    def update_network_mode_display():
        if use_wan:
            btn_net.config(text=f"Сеть: ИНТЕРНЕТ (I) {WAN_HOST}", bg="#b26a00")
        else:
            btn_net.config(text="Сеть: ЛОКАЛЬ (I) .21 / .20", bg="#006b6b")

    def update_stend_mode_display():
        if use_unified_stend:
            btn_stend.config(text="Стенд: StendRasb2 (T)", bg="#7b1fa2")
        else:
            btn_stend.config(text="Стенд: 2 Pi (T)", bg="#33691e")

    def _set_voltage_row(label_widget, label_text, value):
        if value is None:
            label_widget.config(text=f"{label_text}: — В", bg=COL_DIM)
            return
        volts = round(float(value), 3)
        label_widget.config(
            text=f"{label_text}: {volts} В",
            bg=COL_OFF if volts <= VOLTAGE_CRITICAL_V else COL_ON,
        )

    def update_voltage_display():
        _set_voltage_row(lbl_a0, "A0", voltage_a0)
        _set_voltage_row(lbl_a1, "A1", voltage_a1)

    def apply_arduino_telemetry(gps, a0, a1):
        nonlocal voltage_a0, voltage_a1
        if a0 and "volt" in a0:
            voltage_a0 = a0["volt"]
        elif a0 is None:
            voltage_a0 = None
        if a1 and "volt" in a1:
            voltage_a1 = a1["volt"]
        elif a1 is None:
            voltage_a1 = None
        update_voltage_display()
        if not gps or not gps.get("fix"):
            return
        try:
            lat = float(gps["lat"]); lon = float(gps["lon"])
        except (KeyError, TypeError, ValueError):
            return
        heading = gps.get("course")
        try:
            heading = float(heading) if heading is not None else None
        except (TypeError, ValueError):
            heading = None
        mm = getattr(root, "_minimap", None)
        if mm is not None:
            mm.apply_live_gps(lat, lon, heading)

    def arduino_poll_loop():
        while True:
            t0 = time.time()
            gps = fetch_arduino_gps()
            a0 = fetch_arduino_a0()
            a1 = fetch_arduino_a1()
            try:
                root.after(0, lambda g=gps, v0=a0, v1=a1:
                           apply_arduino_telemetry(g, v0, v1))
            except tk.TclError:
                break
            time.sleep(max(0.0, 0.5 - (time.time() - t0)))

    # --- индикаторы кнопок-статусов ---
    def _paint_elev():
        # зелёный при 1/3, серый при 0/2
        btn_elev.config(bg=COL_ON if elevator_level in (1, 3) else COL_STOP)
        btn_elev_back.config(bg=COL_ON if elevator_level in (1, 3) else COL_STOP)

    def _paint_must():
        btn_must.config(bg=COL_ON if mustache_level else COL_OFF)

    def _paint_lift():
        btn_lift.config(bg=COL_ON if lift_level else COL_OFF)
        btn_lift_down.config(bg=COL_ON if lift_level else COL_OFF)

    def _paint_pump():
        btn_pump.config(bg=COL_ON if pump_level else COL_OFF)

    def _paint_filt():
        btn_filt.config(bg=COL_ON if filter_level else COL_OFF)

    def update_all_buttons():
        _paint_elev(); _paint_must(); _paint_lift()
        _paint_pump(); _paint_filt()

    def update_filter_display():
        if filter_interval and filter_period and filter_level == 1:
            btn_filt.config(
                text=f"● Фильтр (F) [{filter_interval}/{filter_period}]"
            )
        else:
            btn_filt.config(text="● Фильтр (F)")

    def on_filter_pulse(active: bool):
        if active:
            btn_filt.config(bg=COL_WARN)
            print("Фильтр: импульс очистки ВКЛ (жёлтый)")
        else:
            _paint_filt()
            print(f"Фильтр: импульс очистки ВЫКЛ → {['красный', 'зелёный'][filter_level]}")

    _filter_ui["root"] = root
    _filter_ui["on_pulse"] = on_filter_pulse

    # ------------------------------------------------------------------
    # Обновление блоков скоростей
    # ------------------------------------------------------------------
    def _set_block(shapes, level):
        for i in range(num_squares):
            color = "gray"
            if level == -3:
                if i in (0, 1, 2): color = "red"
            elif level == -2:
                if i in (1, 2): color = "red"
            elif level == -1:
                if i == 2: color = "red"
            elif level == 0:
                if i == 3: color = "yellow"
            elif level == 1:
                if i == 4: color = "green"
            elif level == 2:
                if i in (4, 5): color = "green"
            elif level == 3:
                if i in (4, 5, 6): color = "green"
            canvas.itemconfig(shapes[i], fill=color)

    def update_squares(send: bool = True):
        _set_block(left_shapes, left_level)
        _set_block(right_shapes, right_level)
        if send:
            threading.Thread(target=set_gear_right, args=(right_level,), daemon=True).start()
            threading.Thread(target=set_gear_left, args=(left_level,), daemon=True).start()

    # ------------------------------------------------------------------
    # Диалог фильтра
    # ------------------------------------------------------------------
    def show_filter_dialog():
        global dialog_active, periodvkl, timeon

        def ask_parameters():
            nonlocal filter_level, filter_interval, filter_period
            global periodvkl, timeon
            try:
                period_str = simpledialog.askstring(
                    "Интервал включения",
                    "Введите интервал включения (сек):", parent=root)
                timeon_str = simpledialog.askstring(
                    "Время включения",
                    "Введите время включения (сек):", parent=root)
                if period_str and timeon_str:
                    periodvkl = float(period_str); timeon = float(timeon_str)
                    filter_interval = timeon_str; filter_period = period_str
                    root.after(0, update_filter_display)
                else:
                    global filtrochistki_isOn
                    filtrochistki_isOn = False
                    filter_level = 0
                    root.after(0, _paint_filt)
            except Exception as e:
                print(f"Ошибка: {e}")
            finally:
                dialog_active = False

        threading.Thread(target=ask_parameters, daemon=True).start()

    def autopilot_blocks_motors() -> bool:
        mm = getattr(root, "_minimap", None)
        if mm is None:
            return False
        try:
            return bool(mm.autopilot.is_running)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Обработчики — те же, что и раньше, только вызывают _paint_*
    # ------------------------------------------------------------------
    def on_up():
        nonlocal left_level, right_level
        if autopilot_blocks_motors(): return
        if left_level < 3: left_level += 1
        if right_level < 3: right_level += 1
        update_squares()

    def on_down():
        nonlocal left_level, right_level
        if autopilot_blocks_motors(): return
        if left_level > -3: left_level -= 1
        if right_level > -3: right_level -= 1
        update_squares()

    def on_left():
        nonlocal left_level, right_level
        if autopilot_blocks_motors(): return
        if right_level < 3: right_level += 1
        if left_level > -3: left_level -= 1
        update_squares()

    def on_right():
        nonlocal left_level, right_level
        if autopilot_blocks_motors(): return
        if right_level > -3: right_level -= 1
        if left_level < 3: left_level += 1
        update_squares()

    def on_space():
        nonlocal left_level, right_level
        if autopilot_blocks_motors(): return
        left_level = 0; right_level = 0
        update_squares()

    def on_left_up():
        nonlocal left_level
        if autopilot_blocks_motors(): return
        if left_level < 3: left_level += 1
        update_squares()

    def on_left_down():
        nonlocal left_level
        if autopilot_blocks_motors(): return
        if left_level > -3: left_level -= 1
        update_squares()

    def on_right_up():
        nonlocal right_level
        if autopilot_blocks_motors(): return
        if right_level < 3: right_level += 1
        update_squares()

    def on_right_down():
        nonlocal right_level
        if autopilot_blocks_motors(): return
        if right_level > -3: right_level -= 1
        update_squares()

    def on_elevator_key():
        nonlocal elevator_level
        if elevator_level == 1:
            elevator_level = 2
            _paint_elev()
            print("Элеватор: стоп (2)")
            threading.Thread(target=run_elevator_new, args=(2,), daemon=True).start()
            return
        if elevator_level == 3:
            elevator_level = 2
            _paint_elev()
            print("Элеватор: после R → стоп (2)")
            threading.Thread(target=run_elevator_new, args=(2,), daemon=True).start()
            return
        elevator_level = 1
        _paint_elev()
        print("Элеватор: вперёд (1)")
        threading.Thread(target=run_elevator_new, args=(1,), daemon=True).start()

    def on_elevator_key_down():
        nonlocal elevator_level
        if elevator_level == 3:
            elevator_level = 0
            _paint_elev()
            print("Элеватор: стоп (0)")
            threading.Thread(target=run_elevator_new, args=(0,), daemon=True).start()
            return
        if elevator_level == 1:
            elevator_level = 0
            _paint_elev()
            print("Элеватор: после E → стоп (0)")
            threading.Thread(target=run_elevator_new, args=(0,), daemon=True).start()
            return
        elevator_level = 3
        _paint_elev()
        print("Элеватор: назад (3)")
        threading.Thread(target=run_elevator_new, args=(3,), daemon=True).start()

    def on_mustache_key():
        nonlocal mustache_level
        mustache_level = (mustache_level + 1) % 2
        _paint_must()
        print(f"Усы: {['красный', 'зелёный'][mustache_level]}")
        threading.Thread(target=set_mustache, args=(mustache_level,), daemon=True).start()

    def on_lift_key_down():
        nonlocal lift_level, lift_last_ok, lift_busy
        if lift_last_ok == "down":
            print("Подъёмники вниз уже выполнены — сначала U")
            return
        lift_level = 0; lift_last_ok = "down"
        _paint_lift()
        print("Подъёмники: вниз")

        def on_done(ok, _resp):
            nonlocal lift_last_ok, lift_busy
            lift_busy = False
            if not ok:
                lift_last_ok = None
                print("Подъёмники вниз: сервер не подтвердил")

        lift_busy = True
        lift("-1", "4", wait=False, callback=on_done)

    def on_lift_key():
        nonlocal lift_level, lift_last_ok, lift_busy
        if lift_last_ok == "up":
            print("Подъёмники вверх уже выполнены — сначала J")
            return
        lift_level = 1; lift_last_ok = "up"
        _paint_lift()
        print("Подъёмники: вверх")

        def on_done(ok, _resp):
            nonlocal lift_last_ok, lift_busy
            lift_busy = False
            if not ok:
                lift_last_ok = None
                print("Подъёмники вверх: сервер не подтвердил")

        lift_busy = True
        lift("1", "4", wait=False, callback=on_done)

    def on_pump_key():
        nonlocal pump_level
        pump_level = (pump_level + 1) % 2
        _paint_pump()
        print(f"Помпа: {['красный', 'зелёный'][pump_level]}")
        threading.Thread(target=set_pump, args=(pump_level,), daemon=True).start()

    def on_filter_key():
        nonlocal filter_level, filter_interval, filter_period, dialog_active
        global filtrochistki_isOn
        if dialog_active:
            print("Диалог уже открыт")
            return
        old = filter_level
        new = (filter_level + 1) % 2
        if old == 0 and new == 1:
            filtrochistki_isOn = True
            dialog_active = True
            filter_level = new
            _paint_filt()
            threading.Thread(target=show_filter_dialog, daemon=True).start()
        elif old == 1 and new == 0:
            filtrochistki_isOn = False
            filter_level = new
            filter_interval = ""; filter_period = ""
            _paint_filt(); update_filter_display()
            print("Фильтр: КРАСНЫЙ (параметры сброшены)")
        else:
            filter_level = new
            _paint_filt()

    def on_network_mode_key():
        def work():
            toggle_network_mode(prompt_if_missing=True)
            update_network_mode_display()
            update_stend_mode_display()
        root.after(0, work)

    def on_stend_mode_key():
        toggle_stend_mode()
        update_stend_mode_display()
        update_network_mode_display()

    # --- глобальные горячие клавиши ---
    root.bind("<Up>",      lambda e: on_up())
    root.bind("<Down>",    lambda e: on_down())
    root.bind("<Right>",   lambda e: on_right())
    root.bind("<Left>",    lambda e: on_left())
    root.bind("<space>",   lambda e: on_space())
    root.bind("7",         lambda e: on_left_up())
    root.bind("1",         lambda e: on_left_down())
    root.bind("9",         lambda e: on_right_up())
    root.bind("3",         lambda e: on_right_down())
    root.bind("e",         lambda e: on_elevator_key())
    root.bind("r",         lambda e: on_elevator_key_down())
    root.bind("y",         lambda e: on_mustache_key())
    root.bind("u",         lambda e: on_lift_key())
    root.bind("j",         lambda e: on_lift_key_down())
    root.bind("p",         lambda e: on_pump_key())
    root.bind("f",         lambda e: on_filter_key())
    root.bind("i",         lambda e: on_network_mode_key())
    root.bind("t",         lambda e: on_stend_mode_key())

    if HAS_KEYBOARD:
        try:
            keyboard.add_hotkey("up", on_up)
            keyboard.add_hotkey("down", on_down)
            keyboard.add_hotkey("right", on_right)
            keyboard.add_hotkey("left", on_left)
            keyboard.add_hotkey("space", on_space)
            keyboard.add_hotkey("7", on_left_up)
            keyboard.add_hotkey("1", on_left_down)
            keyboard.add_hotkey("9", on_right_up)
            keyboard.add_hotkey("3", on_right_down)
            keyboard.add_hotkey("e", on_elevator_key)
            keyboard.add_hotkey("r", on_elevator_key_down)
            keyboard.add_hotkey("y", on_mustache_key)
            keyboard.add_hotkey("u", on_lift_key)
            keyboard.add_hotkey("j", on_lift_key_down)
            keyboard.add_hotkey("p", on_pump_key)
            keyboard.add_hotkey("f", on_filter_key)
            keyboard.add_hotkey("i", on_network_mode_key)
            keyboard.add_hotkey("t", on_stend_mode_key)
        except Exception as e:
            print(f"keyboard.add_hotkey: {e}")

    # Инициализация отображения
    update_network_mode_display()
    update_stend_mode_display()
    update_voltage_display()
    update_all_buttons()
    update_squares()

    # ------------------------------------------------------------------
    # Миникарта снизу
    # ------------------------------------------------------------------
    try:
        from show_map_new import MiniMapApp

        def sync_motor_ui(left: int, right: int) -> None:
            nonlocal left_level, right_level
            left_level = max(-3, min(3, int(left)))
            right_level = max(-3, min(3, int(right)))
            root.after(0, lambda: update_squares(send=False))

        root._minimap = MiniMapApp(
            master=root,
            container=minimap_holder,
            embedded=True,
            motor_api={
                "set_left": set_gear_left,
                "set_right": set_gear_right,
                "sync_ui": sync_motor_ui,
            },
        )
    except Exception as e:
        print(f"Миникарта не запущена: {e}")

    threading.Thread(target=arduino_poll_loop, daemon=True, name="ArduinoPoll").start()

    return outer


# =============================================================================
# Камеры — CameraManager, процесс воркер, CameraFeed, DetachedWindow
# =============================================================================

class CameraManager:
    def __init__(self, config_file='camera_config.json'):
        self.cameras = []
        self.config_file = config_file
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.cameras = data.get('cameras', [])
                    print(f"✅ Загружено {len(self.cameras)} камер из конфигурации")
            except Exception as e:
                print(f"❌ Ошибка загрузки конфигурации: {e}")
                self.cameras = []
        else:
            print("ℹ️ Файл конфигурации не найден, создаем новый")
            self.cameras = []
            self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump({'cameras': self.cameras}, f, ensure_ascii=False, indent=2)
            print("✅ Конфигурация сохранена")
        except Exception as e:
            print(f"❌ Ошибка сохранения конфигурации: {e}")

    def add_camera(self, name, ip, port, login, password):
        camera_id = max([c['id'] for c in self.cameras] + [-1]) + 1
        camera = {
            'id': camera_id, 'name': name, 'ip': ip, 'port': port,
            'login': login, 'password': password, 'enabled': True,
        }
        self.cameras.append(camera)
        self.save_config()
        return camera_id

    def remove_camera(self, camera_id):
        self.cameras = [c for c in self.cameras if c['id'] != camera_id]
        self.save_config()

    def get_cameras(self):
        return self.cameras


def camera_process_worker(camera_info, frame_queue, status_queue, stop_event):
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;5000000"

    info = camera_info
    ip, port = info['ip'], info['port']
    login, password, name = info['login'], info['password'], info['name']

    urls = [
        f"rtsp://{login}:{password}@{ip}:{port}/cam/realmonitor?channel=1&subtype=0",
        f"rtsp://{login}:{password}@{ip}:{port}/cam/realmonitor?channel=1&subtype=1",
        f"rtsp://{login}:{password}@{ip}:{port}/streaming/channels/101",
        f"rtsp://{login}:{password}@{ip}:{port}/live",
        f"rtsp://{login}:{password}@{ip}:{port}/streaming/channels/1",
        f"rtsp://{login}:{password}@{ip}:{port}/h264",
        f"rtsp://{login}:{password}@{ip}:{port}/h265",
    ]

    cap = None
    connected = False

    def try_connect():
        nonlocal cap, connected
        for url in urls:
            if stop_event.is_set():
                return False
            try:
                print(f"  📹 [{name}] Подключение: {url}")
                c = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if c.isOpened():
                    ret, frame = c.read()
                    if ret and frame is not None:
                        cap = c
                        connected = True
                        status_queue.put(('connected', True))
                        print(f"  ✅ [{name}] подключена")
                        return True
                c.release()
            except Exception as e:
                print(f"  ❌ [{name}] {e}")
            time.sleep(0.3)
        return False

    while not stop_event.is_set() and not connected:
        if try_connect():
            break
        time.sleep(2)

    last_frame_time = time.time()
    frame_count = 0
    start_time = time.time()
    no_frame_count = 0

    while not stop_event.is_set():
        try:
            if cap is None or not connected:
                status_queue.put(('connected', False))
                time.sleep(1)
                connected = False
                while not stop_event.is_set() and not connected:
                    if try_connect():
                        break
                    time.sleep(2)
                last_frame_time = time.time()
                no_frame_count = 0
                continue

            ret, frame = cap.read()
            if ret and frame is not None:
                try:
                    if frame_queue.full():
                        try:
                            frame_queue.get_nowait()
                        except Exception:
                            pass
                    frame_queue.put_nowait((frame, time.time()))
                except Exception:
                    pass

                no_frame_count = 0
                frame_count += 1
                last_frame_time = time.time()
            else:
                no_frame_count += 1
                if time.time() - last_frame_time > 5 and no_frame_count > 10:
                    print(f"🔄 [{name}] Переподключение...")
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap = None
                    connected = False
                    status_queue.put(('connected', False))
                    no_frame_count = 0

            time.sleep(0.001)

        except Exception as e:
            print(f"❌ [{name}] ошибка чтения: {e}")
            time.sleep(1)

    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass


class CameraFeed:
    def __init__(self, camera_info):
        self.camera_info = camera_info
        self.frame = None
        self.frame_lock = threading.Lock()
        self.connected = False
        self.is_running = False
        self.last_frame_time = 0
        self.fps = 0
        self.frame_count = 0
        self.start_time = None

        self.frame_queue = mp.Queue(maxsize=2)
        self.status_queue = mp.Queue(maxsize=10)
        self.stop_event = mp.Event()
        self.process = None
        self.reader_thread = None

    def start(self):
        if self.is_running:
            return True
        self.is_running = True
        self.start_time = time.time()
        self.process = mp.Process(
            target=camera_process_worker,
            args=(self.camera_info, self.frame_queue, self.status_queue, self.stop_event),
            daemon=True,
        )
        self.process.start()
        self.reader_thread = threading.Thread(target=self._read_queue, daemon=True)
        self.reader_thread.start()
        return True

    def _read_queue(self):
        while self.is_running:
            try:
                while True:
                    msg = self.status_queue.get_nowait()
                    if msg[0] == 'connected':
                        self.connected = msg[1]
            except Exception:
                pass
            try:
                frame, ts = self.frame_queue.get(timeout=0.05)
                with self.frame_lock:
                    self.frame = frame
                self.last_frame_time = ts
                self.frame_count += 1
                if self.frame_count % 30 == 0:
                    elapsed = time.time() - self.start_time
                    if elapsed > 0:
                        self.fps = self.frame_count / elapsed
            except Exception:
                pass
            time.sleep(0.001)

    def get_frame(self):
        with self.frame_lock:
            return self.frame.copy() if self.frame is not None else None

    def stop(self):
        self.is_running = False
        self.stop_event.set()
        if self.reader_thread:
            self.reader_thread.join(timeout=1)
        if self.process:
            self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=1)
        try:
            while not self.frame_queue.empty():
                self.frame_queue.get_nowait()
        except Exception:
            pass
        try:
            while not self.status_queue.empty():
                self.status_queue.get_nowait()
        except Exception:
            pass
        self.connected = False


class DetachedWindow:
    def __init__(self, parent_app, camera_info, feed):
        self.parent_app = parent_app
        self.camera_info = camera_info
        self.feed = feed
        self.camera_id = camera_info['id']
        self._image = None
        self._running = True

        self.win = tk.Toplevel(parent_app.root)
        self.win.title(f"Камера: {camera_info['name']}")
        self.win.geometry("800x600")
        self.win.minsize(320, 240)
        self.win.configure(bg='black')

        toolbar = tk.Frame(self.win, bg='lightgray', height=30)
        toolbar.pack(side=tk.TOP, fill=tk.X)
        tk.Button(toolbar, text="↩ Вернуть в сетку",
                  command=self.close).pack(side=tk.LEFT, padx=5, pady=2)
        self.info_var = tk.BooleanVar(value=True)
        tk.Checkbutton(toolbar, text="📊 Информация",
                       variable=self.info_var, bg='lightgray').pack(side=tk.LEFT, padx=10)
        self.status_label = tk.Label(toolbar, text="", bg='lightgray')
        self.status_label.pack(side=tk.RIGHT, padx=10)

        self.canvas = tk.Canvas(self.win, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self._update()

    def _update(self):
        if not self._running:
            return
        try:
            w = self.canvas.winfo_width()
            h = self.canvas.winfo_height()
            if w < 10 or h < 10:
                self.win.after(50, self._update)
                return
            frame = self.feed.get_frame()
            self.canvas.delete("all")
            if frame is not None:
                fh, fw = frame.shape[:2]
                scale = min(w / fw, h / fh)
                new_w = max(1, int(fw * scale))
                new_h = max(1, int(fh * scale))
                resized = cv2.resize(frame, (new_w, new_h))
                rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
                if self.info_var.get():
                    name = self.camera_info['name']
                    status = f"✅ {self.feed.fps:.1f} FPS" if self.feed.connected else "❌ Отключено"
                    cv2.putText(rgb, name, (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                    cv2.putText(rgb, status, (10, 60),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)
                    cv2.putText(rgb, datetime.now().strftime('%H:%M:%S'),
                                (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
                img = Image.fromarray(rgb)
                imgtk = ImageTk.PhotoImage(image=img)
                self.canvas.create_image(w // 2, h // 2, anchor=tk.CENTER, image=imgtk)
                self._image = imgtk
                self.status_label.config(text=f"{self.camera_info['name']} — {self.feed.fps:.1f} FPS")
            else:
                self.canvas.create_text(w // 2, h // 2,
                                        text=f"{self.camera_info['name']}\nНет сигнала",
                                        fill='white', font=('Arial', 16), justify=tk.CENTER)
                self.status_label.config(text=f"{self.camera_info['name']} — нет сигнала")
        except Exception as e:
            print(f"❌ DetachedWindow [{self.camera_info['name']}]: {e}")
        self.win.after(33, self._update)

    def close(self):
        self._running = False
        try:
            self.win.destroy()
        except Exception:
            pass
        self.parent_app.detached.pop(self.camera_id, None)


# =============================================================================
# Главное приложение
# =============================================================================

class CameraGridApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Система видеонаблюдения RTSP + Пульт управления")
        self.root.geometry("1700x900")
        self.root.minsize(900, 500)
        self.root.configure(bg='black')

        self.camera_manager = CameraManager()
        self.feeds = {}
        self.display_cameras = []
        self._images = []
        self.detached = {}

        self.create_menu()
        self.create_toolbar()
        self.create_status_bar()

        # ---------- Основное разбиение: слева камеры, справа пульт ----------
        main_pane = tk.PanedWindow(self.root, orient=tk.HORIZONTAL,
                                   bg='black', sashwidth=4, sashrelief=tk.RAISED)
        main_pane.pack(fill=tk.BOTH, expand=True)

        # Левый контейнер — сетка камер
        self.canvas_frame = tk.Frame(main_pane, bg='black')
        main_pane.add(self.canvas_frame, stretch="always", minsize=400)

        self.canvas = tk.Canvas(self.canvas_frame, bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)

        # Правый контейнер — панель управления (фиксированная ширина)
        self.control_panel = create_control_panel(main_pane, width=440)
        main_pane.add(self.control_panel, stretch="never", minsize=440)

        self.load_cameras()

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.bind('<Configure>', self.on_resize)

        self.update_video()

    def create_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        camera_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Камеры", menu=camera_menu)
        camera_menu.add_command(label="Добавить камеру", command=self.add_camera_dialog)
        camera_menu.add_command(label="Управление камерами", command=self.manage_cameras_dialog)
        camera_menu.add_separator()
        camera_menu.add_command(label="Обновить все", command=self.reload_cameras)

        view_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Вид", menu=view_menu)
        view_menu.add_command(label="Показать информацию", command=self.toggle_info)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Помощь", menu=help_menu)
        help_menu.add_command(label="О программе", command=self.show_about)

    def create_toolbar(self):
        toolbar = tk.Frame(self.root, bg='lightgray', height=40)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        tk.Button(toolbar, text="➕ Добавить камеру",
                  command=self.add_camera_dialog).pack(side=tk.LEFT, padx=5, pady=5)
        tk.Button(toolbar, text="🔄 Обновить",
                  command=self.reload_cameras).pack(side=tk.LEFT, padx=5, pady=5)

        self.info_var = tk.BooleanVar(value=True)
        tk.Checkbutton(toolbar, text="📊 Информация",
                       variable=self.info_var,
                       command=self.toggle_info,
                       bg='lightgray').pack(side=tk.LEFT, padx=10)
        tk.Button(toolbar, text="⛶ На весь экран",
                  command=self.toggle_fullscreen).pack(side=tk.LEFT, padx=5)

        self.status_label = tk.Label(toolbar, text="Готов", bg='lightgray')
        self.status_label.pack(side=tk.RIGHT, padx=10)
        self.cam_counter = tk.Label(toolbar, text="Камер: 0", bg='lightgray')
        self.cam_counter.pack(side=tk.RIGHT, padx=10)

    def create_status_bar(self):
        self.status_bar = tk.Label(self.root, text="Готов к работе",
                                   bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def toggle_info(self):
        pass

    def toggle_fullscreen(self):
        self.root.attributes('-fullscreen', not self.root.attributes('-fullscreen'))

    def on_resize(self, event):
        if event.widget == self.root:
            # canvas сам растягивается через pack, ничего руками не задаём
            pass

    def load_cameras(self):
        self.status_bar.config(text="Загрузка камер...")
        for feed in self.feeds.values():
            feed.stop()
        self.feeds.clear()
        self.display_cameras = []

        for camera_info in self.camera_manager.get_cameras():
            if camera_info.get('enabled', True):
                self.display_cameras.append(camera_info)
                self.add_camera_feed(camera_info)

        self.update_counter()
        self.status_bar.config(
            text=f"Загружено {len(self.display_cameras)} камер (подключение в фоне...)"
        )

    def add_camera_feed(self, camera_info):
        camera_id = camera_info['id']
        if camera_id in self.feeds:
            return
        feed = CameraFeed(camera_info)
        feed.start()
        self.feeds[camera_id] = feed
        self.status_bar.config(text=f"Подключение: {camera_info['name']}...")

    def reload_cameras(self):
        for win in list(self.detached.values()):
            try:
                win._running = False
                win.win.destroy()
            except Exception:
                pass
        self.detached.clear()

        for feed in self.feeds.values():
            feed.stop()
        self.feeds.clear()
        self.display_cameras = []
        self.load_cameras()

    def update_counter(self):
        total = len(self.display_cameras)
        active = sum(1 for feed in self.feeds.values() if feed.connected)
        self.cam_counter.config(text=f"Камер: {active}/{total}")

    def _get_camera_id_at(self, x, y):
        if not self.display_cameras:
            return None
        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        if canvas_width < 10 or canvas_height < 10:
            return None
        num_cameras = len(self.display_cameras)
        cols = math.ceil(math.sqrt(num_cameras))
        rows = math.ceil(num_cameras / cols)
        margin = 2
        cell_width = (canvas_width - margin * (cols + 1)) // cols
        cell_height = (canvas_height - margin * (rows + 1)) // rows
        for idx, camera_info in enumerate(self.display_cameras):
            row = idx // cols
            col = idx % cols
            x1 = margin + col * (cell_width + margin)
            y1 = margin + row * (cell_height + margin)
            x2 = x1 + cell_width
            y2 = y1 + cell_height
            if x1 <= x <= x2 and y1 <= y <= y2:
                return camera_info['id']
        return None

    def _on_canvas_double_click(self, event):
        camera_id = self._get_camera_id_at(event.x, event.y)
        if camera_id is None:
            return
        if camera_id in self.detached:
            try:
                self.detached[camera_id].win.lift()
            except Exception:
                pass
            return
        self.detach_camera(camera_id)

    def detach_camera(self, camera_id):
        feed = self.feeds.get(camera_id)
        if feed is None:
            return
        camera_info = next((c for c in self.display_cameras if c['id'] == camera_id), None)
        if camera_info is None:
            return
        win = DetachedWindow(self, camera_info, feed)
        self.detached[camera_id] = win
        self.status_bar.config(text=f"Камера '{camera_info['name']}' вынесена в отдельное окно")

    def update_video(self):
        if not self.display_cameras:
            self.canvas.delete("all")
            self._images.clear()
            self.canvas.create_text(
                self.canvas.winfo_width() // 2,
                self.canvas.winfo_height() // 2,
                text="Нет подключенных камер\nНажмите 'Добавить камеру'",
                fill='white', font=('Arial', 20), justify=tk.CENTER,
            )
            self.root.after(100, self.update_video)
            return

        canvas_width = self.canvas.winfo_width()
        canvas_height = self.canvas.winfo_height()
        if canvas_width < 10 or canvas_height < 10:
            self.root.after(100, self.update_video)
            return

        num_cameras = len(self.display_cameras)
        cols = math.ceil(math.sqrt(num_cameras))
        rows = math.ceil(num_cameras / cols)

        margin = 2
        cell_width = (canvas_width - margin * (cols + 1)) // cols
        cell_height = (canvas_height - margin * (rows + 1)) // rows

        self.canvas.delete("all")
        self._images.clear()

        for idx, camera_info in enumerate(self.display_cameras):
            row = idx // cols
            col = idx % cols
            x1 = margin + col * (cell_width + margin)
            y1 = margin + row * (cell_height + margin)
            x2 = x1 + cell_width
            y2 = y1 + cell_height

            if camera_info['id'] in self.detached:
                self.canvas.create_rectangle(x1, y1, x2, y2,
                                             fill='#1a1a2b', outline='#444', width=1)
                self.canvas.create_text(x1 + cell_width // 2, y1 + cell_height // 2,
                                        text=f"{camera_info['name']}\n(в отдельном окне)\n\nДвойной клик — вернуть фокус",
                                        fill='#88aaff', font=('Arial', 11), justify=tk.CENTER)
                continue

            feed = self.feeds.get(camera_info['id'])
            frame = feed.get_frame() if feed is not None else None

            if frame is not None:
                h, w = frame.shape[:2]
                scale = min(cell_width / w, cell_height / h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                resized = cv2.resize(frame, (new_w, new_h))
                rgb_frame = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

                if self.info_var.get():
                    info_text = f"{camera_info['name']}"
                    status_text = f"✅ {feed.fps:.1f} FPS" if feed.connected else "❌ Отключено"
                    cv2.putText(rgb_frame, info_text, (10, 30),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                    cv2.putText(rgb_frame, status_text, (10, 55),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
                    cv2.putText(rgb_frame, datetime.now().strftime('%H:%M:%S'),
                                (10, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

                img = Image.fromarray(rgb_frame)
                imgtk = ImageTk.PhotoImage(image=img)
                self.canvas.create_image(x1 + cell_width // 2, y1 + cell_height // 2,
                                         anchor=tk.CENTER, image=imgtk)
                self._images.append(imgtk)
            else:
                self.canvas.create_rectangle(x1, y1, x2, y2,
                                             fill='#2b2b2b', outline='#444', width=2)
                status = "Подключение..." if feed is not None else "Нет сигнала"
                self.canvas.create_text(x1 + cell_width // 2, y1 + cell_height // 2,
                                        text=f"{camera_info['name']}\n{status}",
                                        fill='white', font=('Arial', 12), justify=tk.CENTER)

            self.canvas.create_rectangle(x1, y1, x2, y2, outline='#444', width=1)

        active_cams = sum(1 for feed in self.feeds.values() if feed.connected)
        total_cams = len(self.display_cameras)
        self.status_bar.config(text=f"Камер: {active_cams}/{total_cams} активны")
        self.cam_counter.config(text=f"Камер: {active_cams}/{total_cams}")

        self.root.after(30, self.update_video)

    def add_camera_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Добавить камеру")
        dialog.geometry("400x350")
        dialog.transient(self.root)
        dialog.grab_set()

        fields = [
            ('Название:', 'entry', 'Камера'),
            ('IP адрес:', 'entry', ''),
            ('Порт:', 'entry', '554'),
            ('Логин:', 'entry', 'admin'),
            ('Пароль:', 'entry', ''),
        ]
        entries = {}
        for i, (label, _type, default) in enumerate(fields):
            tk.Label(dialog, text=label).grid(row=i, column=0, padx=10, pady=5, sticky='e')
            entry = tk.Entry(dialog, width=25, show='*' if 'Пароль' in label else '')
            entry.insert(0, default)
            entry.grid(row=i, column=1, padx=10, pady=5, sticky='w')
            entries[label] = entry

        def save_camera():
            try:
                name = entries['Название:'].get()
                ip = entries['IP адрес:'].get()
                port = int(entries['Порт:'].get())
                login = entries['Логин:'].get()
                password = entries['Пароль:'].get()
                if not ip:
                    messagebox.showerror("Ошибка", "Введите IP адрес")
                    return
                camera_id = self.camera_manager.add_camera(name, ip, port, login, password)
                for cam in self.camera_manager.get_cameras():
                    if cam['id'] == camera_id:
                        self.display_cameras.append(cam)
                        self.add_camera_feed(cam)
                        break
                self.update_counter()
                dialog.destroy()
                self.status_bar.config(text=f"Камера {name} добавлена")
            except ValueError:
                messagebox.showerror("Ошибка", "Неверный формат порта")
            except Exception as e:
                messagebox.showerror("Ошибка", f"Ошибка добавления: {e}")

        tk.Button(dialog, text="Добавить", command=save_camera).grid(row=len(fields), column=0, pady=20)
        tk.Button(dialog, text="Отмена", command=dialog.destroy).grid(row=len(fields), column=1, pady=20)

    def manage_cameras_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Управление камерами")
        dialog.geometry("600x400")
        dialog.transient(self.root)
        dialog.grab_set()

        tree = ttk.Treeview(dialog, columns=('ID', 'Название', 'IP', 'Порт', 'Статус'),
                            show='headings')
        tree.heading('ID', text='ID')
        tree.heading('Название', text='Название')
        tree.heading('IP', text='IP адрес')
        tree.heading('Порт', text='Порт')
        tree.heading('Статус', text='Статус')
        tree.column('ID', width=50)
        tree.column('Название', width=150)
        tree.column('IP', width=150)
        tree.column('Порт', width=80)
        tree.column('Статус', width=100)
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        def fill_tree():
            for item in tree.get_children():
                tree.delete(item)
            for cam in self.camera_manager.get_cameras():
                feed = self.feeds.get(cam['id'])
                status = "✅ Активна" if feed and feed.connected else "❌ Отключена"
                tree.insert('', 'end', values=(cam['id'], cam['name'], cam['ip'], cam['port'], status))

        fill_tree()

        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=10)

        def delete_camera():
            selection = tree.selection()
            if not selection:
                messagebox.showwarning("Предупреждение", "Выберите камеру")
                return
            if messagebox.askyesno("Подтверждение", "Удалить выбранную камеру?"):
                item = tree.item(selection[0])
                camera_id = item['values'][0]

                if camera_id in self.detached:
                    try:
                        self.detached[camera_id]._running = False
                        self.detached[camera_id].win.destroy()
                    except Exception:
                        pass
                    del self.detached[camera_id]

                self.camera_manager.remove_camera(camera_id)
                if camera_id in self.feeds:
                    self.feeds[camera_id].stop()
                    del self.feeds[camera_id]
                self.display_cameras = [c for c in self.display_cameras if c['id'] != camera_id]
                tree.delete(selection[0])
                self.update_counter()
                self.status_bar.config(text=f"Камера {camera_id} удалена")

        tk.Button(btn_frame, text="❌ Удалить", command=delete_camera).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="🔄 Обновить", command=fill_tree).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Закрыть", command=dialog.destroy).pack(side=tk.RIGHT, padx=5)

    def show_about(self):
        messagebox.showinfo(
            "О программе",
            "Система видеонаблюдения RTSP + Пульт управления\n"
            "Версия 4.0\n\n"
            "Камеры отображаются слева, пульт управления — справа.\n"
            "Двойной клик по ячейке — вынести камеру в отдельное окно.\n"
            "Управление: Up/Down/Left/Right/Space, 7/1/9/3,\n"
            "E/R (элеватор), Y (усы), U/J (подъёмники), P (помпа), F (фильтр),\n"
            "I (сеть), T (режим стенда)."
        )

    def on_closing(self):
        for win in list(self.detached.values()):
            try:
                win._running = False
                win.win.destroy()
            except Exception:
                pass
        self.detached.clear()

        for feed in self.feeds.values():
            feed.stop()
        self.root.destroy()


if __name__ == "__main__":
    mp.freeze_support()

    print("Надёжный канал: очередь + retry + keepalive + cmd_id")
    mode, ep1, ep2 = apply_network_mode()
    if use_unified_stend:
        print(f"Старт: {mode} | единый стенд {ep1[0]}:{ep1[1]} (rasb2 не используется)")
    else:
        print(f"Старт: {mode} | rasb1 {ep1[0]}:{ep1[1]} | rasb2 {ep2[0]}:{ep2[1]}")

    threading.Thread(target=Wake_On_Lan, daemon=True).start()
    threading.Thread(target=Start_filtr_ochistki, daemon=True).start()

    app = CameraGridApp()
    app.root.mainloop()