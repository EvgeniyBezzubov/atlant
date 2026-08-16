"""Kivy-клиент Android: ландшафт, стики двигателей + круглые кнопки E/R/Y/U/J/P/F."""

import socket
import threading
import time
import uuid
import queue
from dataclasses import dataclass, field
from typing import Callable, Optional

from kivy.config import Config

# Жёстко горизонтальная ориентация (телефон боком)
Config.set("graphics", "orientation", "landscape")
Config.set("graphics", "width", "1280")
Config.set("graphics", "height", "720")
Config.set("graphics", "resizable", "0")

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.graphics import Color, Ellipse, Line, RoundedRectangle
from kivy.metrics import dp
from kivy.properties import BooleanProperty, ListProperty, NumericProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
from kivy.uix.widget import Widget

# --- сеть: локаль и интернет (проброс портов на роутере) ---
WAN_HOST = "37.9.243.135"
LOCAL_RASB1_HOST = "192.168.8.21"
LOCAL_RASB2_HOST = "192.168.8.20"
LOCAL_STEND_HOST = "192.168.8.20"
RASB1_PORT = 12345
RASB2_PORT = 12346
PORT_STEND = 12345

# стартовые (интернет по умолчанию)
RASB1_HOST = WAN_HOST
RASB2_HOST = WAN_HOST
use_unified_stend = False  # False = server3+serverrasb2, True = StendRasb2

COMMAND_TIMEOUT = 5.0
COMMAND_RETRIES = 3
KEEPALIVE_INTERVAL = 1.0
BASE_BACKOFF = 0.25
MAX_BACKOFF = 2.0
LIFT_PULSE_SEC = "4"
# фильтр: как periodvkl / timeon в client.py (меняются диалогом)
FILTER_INTERVAL_SEC = 1.0
FILTER_PULSE_SEC = 0.5


@dataclass
class _Job:
    payload: str
    cmd_id: str
    timeout: float
    retries: int
    coalesce_key: Optional[str]
    done: threading.Event = field(default_factory=threading.Event)
    success: bool = False
    callback: Optional[Callable[[bool, str], None]] = None


class MotorLink:
    """Очередь + coalesce, как ReliableLink в client.py (без deadlock)."""

    def __init__(self, host, port, name="link"):
        self.host = host
        self.port = port
        self.name = name
        self._socket = None
        self._buffer = b""
        self._io_lock = threading.RLock()
        self._q: queue.Queue = queue.Queue()
        self._coalesce = {}
        self._coalesce_lock = threading.Lock()
        self._closed = False
        self._last_error = ""
        self._worker = threading.Thread(
            target=self._worker_loop, name=f"MotorLink-{name}", daemon=True
        )
        self._worker.start()

    def send(
        self,
        payload,
        *,
        wait=False,
        timeout=None,
        retries=None,
        coalesce_key=None,
        require_cmd_id=True,
        callback=None,
    ):
        if self._closed:
            return False

        body = payload.strip()
        is_online = body.upper() == "ONLINE"
        job = _Job(
            payload=body,
            cmd_id=uuid.uuid4().hex[:12],
            timeout=COMMAND_TIMEOUT if timeout is None else timeout,
            retries=COMMAND_RETRIES if retries is None else retries,
            coalesce_key=coalesce_key,
            callback=callback,
        )
        # ONLINE без id
        if is_online:
            require_cmd_id = False

        wait_event = None
        wait_job = None
        enqueue = True

        if coalesce_key:
            with self._coalesce_lock:
                old = self._coalesce.get(coalesce_key)
                if old is not None and not old.done.is_set():
                    old.payload = job.payload
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

        # помечаем ONLINE на job через cmd_id пустой? храним в payload достаточно
        job._require_cmd_id = require_cmd_id  # type: ignore[attr-defined]

        if enqueue:
            self._q.put(job)

        # ждать только ВНЕ coalesce_lock
        if wait_event is not None and wait_job is not None:
            wait_event.wait()
            return wait_job.success
        return True

    def _worker_loop(self):
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
                        job.callback(job.success, "")
                    except Exception:
                        pass

    def _execute_job(self, job: _Job):
        delay = BASE_BACKOFF
        require_cmd_id = getattr(job, "_require_cmd_id", True)
        last_err = ""
        for attempt in range(1, job.retries + 1):
            ok, err = self._attempt(job.payload, job.cmd_id, job.timeout, require_cmd_id)
            if ok:
                job.success = True
                self._last_error = ""
                return
            last_err = err
            self._last_error = err
            with self._io_lock:
                self._disconnect()
            if attempt < job.retries:
                time.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF)
        job.success = False

    def _attempt(self, payload, command_id, timeout, require_cmd_id):
        with self._io_lock:
            try:
                sock = self._connect(timeout)
                if payload.upper() == "ONLINE":
                    wire = b"ONLINE\n"
                else:
                    wire = f"{payload} id={command_id}\n".encode()
                sock.settimeout(timeout)
                sock.sendall(wire)
                reply = self._receive_line(sock)
                if self._is_ack(reply, command_id, require_cmd_id):
                    return True, ""
                return False, f"неожиданный ответ: {reply!r}"
            except Exception as exc:
                self._disconnect()
                return False, str(exc)

    @staticmethod
    def _is_ack(text, command_id, require_cmd_id):
        if not text:
            return False
        upper = text.upper()
        if upper.startswith("OK") or upper.startswith("DUP"):
            if command_id and command_id in text:
                return True
            parts = text.replace("|", " ").split()
            if require_cmd_id and len(parts) >= 2 and parts[1] and parts[1] != command_id:
                return False
            return True
        return True

    def _connect(self, timeout=None):
        if self._socket is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout if timeout is not None else COMMAND_TIMEOUT)
            sock.connect((self.host, self.port))
            for args in (
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            ):
                try:
                    sock.setsockopt(*args)
                except OSError:
                    pass
            self._socket = sock
        return self._socket

    def _receive_line(self, sock):
        while b"\n" not in self._buffer:
            chunk = sock.recv(1024)
            if not chunk:
                raise OSError("сервер закрыл соединение")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line.decode(errors="replace").strip()

    def _disconnect(self):
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        self._buffer = b""

    def close(self):
        self._closed = True
        self._q.put(None)
        with self._io_lock:
            self._disconnect()

    def set_endpoint(self, host, port):
        """Смена хоста/порта с разрывом текущего сокета."""
        with self._io_lock:
            self.host = host
            self.port = port
            self._disconnect()


class MotorController:
    """Стики двигателей → rasb1 или StendRasb2."""

    def __init__(self, link_rasb1, link_rasb2, link_stend, status_callback):
        self.link = link_rasb1
        self.link_rasb2 = link_rasb2
        self.link_stend = link_stend
        self.status_callback = status_callback
        self._condition = threading.Condition()
        self._desired = {"left": 0, "right": 0}
        self._applied = {"left": 0, "right": 0}
        self._dirty = set()
        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _motor_link(self):
        return self.link_stend if use_unified_stend else self.link

    def _aux_link(self):
        return self.link_stend if use_unified_stend else self.link_rasb2

    def set_level(self, side, level):
        level = max(-3, min(3, int(level)))
        with self._condition:
            if self._desired[side] != level:
                self._desired[side] = level
                self._dirty.add(side)
                self._condition.notify()

    def _run(self):
        next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL
        while True:
            with self._condition:
                wait_time = max(0.0, next_keepalive - time.monotonic())
                if not self._dirty and self._running:
                    self._condition.wait(wait_time)
                if not self._running:
                    return
                sides = list(self._dirty)
                self._dirty.clear()

            for side in sides:
                with self._condition:
                    target = self._desired[side]
                    previous = self._applied[side]
                ok = self._apply_level(side, previous, target)
                if ok:
                    with self._condition:
                        self._applied[side] = target
                motor = self._motor_link()
                err = motor._last_error
                detail = (
                    f" [{motor.host}:{motor.port}] {err}"
                    if (not ok and err)
                    else ""
                )
                self.status_callback(
                    f"{'Левый' if side == 'left' else 'Правый'}: "
                    f"{target:+d} — {'OK' if ok else 'НЕТ СВЯЗИ'}{detail}"
                )
                with self._condition:
                    if self._desired[side] != target:
                        self._dirty.add(side)

            if time.monotonic() >= next_keepalive:
                if use_unified_stend:
                    self.link_stend.send(
                        "ONLINE",
                        wait=False,
                        coalesce_key=f"{self.link_stend.name}:keepalive",
                    )
                else:
                    self.link.send(
                        "ONLINE", wait=False, coalesce_key=f"{self.link.name}:keepalive"
                    )
                    self.link_rasb2.send(
                        "ONLINE",
                        wait=False,
                        coalesce_key=f"{self.link_rasb2.name}:keepalive",
                    )
                next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL

    def _apply_level(self, side, previous, target):
        link = self._motor_link()
        gear_command = f"gear_{side}"
        reverse_command = f"reverse_{side}"

        if target == 0:
            return link.send(
                f"{gear_command} 0",
                wait=False,
                coalesce_key=gear_command,
            )

        changed_direction = previous != 0 and (previous > 0) != (target > 0)
        if changed_direction:
            link.send(
                f"{gear_command} 0",
                wait=False,
                coalesce_key=gear_command,
            )

        direction = -1 if target > 0 else 1
        link.send(
            f"{reverse_command} {direction}",
            wait=False,
            coalesce_key=reverse_command,
        )
        return link.send(
            f"{gear_command} {abs(target)}",
            wait=False,
            coalesce_key=gear_command,
        )

    def shutdown(self):
        with self._condition:
            self._running = False
            self._condition.notify()
        self._worker.join(timeout=1.0)
        motor = self._motor_link()
        motor.send("gear_left 0", wait=False, coalesce_key="gear_left")
        motor.send("gear_right 0", wait=False, coalesce_key="gear_right")
        self.link.close()
        self.link_rasb2.close()
        self.link_stend.close()


class AuxController:
    """Логика кнопок E/R/Y/U/J/P/F/I/T как в client.py."""

    def __init__(self, link_rasb1, link_rasb2, link_stend, status_callback, ui_callback):
        self.link1 = link_rasb1
        self.link2 = link_rasb2
        self.link_stend = link_stend
        self.status_callback = status_callback
        self.ui_callback = ui_callback  # key -> color rgba

        self.elevator_level = 2  # стоп
        self.mustache_on = False
        self.pump_on = False
        self.filter_on = False
        self.filter_pulsing = False
        self.filter_interval = FILTER_INTERVAL_SEC
        self.filter_pulse = FILTER_PULSE_SEC
        self._filter_dialog_open = False
        self.lift_last_ok = None
        self.lift_busy = False
        self.use_wan = True  # False=локаль, True=интернет (по умолчанию)
        self._filter_stop = threading.Event()
        self._filter_thread = None

        self._refresh_all_colors()

    def _motor_link(self):
        return self.link_stend if use_unified_stend else self.link1

    def _aux_link(self):
        return self.link_stend if use_unified_stend else self.link2

    def press(self, key: str):
        key = key.lower()
        handlers = {
            "e": self._elevator_forward,
            "r": self._elevator_back,
            "y": self._mustache,
            "u": self._lift_up,
            "j": self._lift_down,
            "p": self._pump,
            "f": self._filter,
            "i": self._toggle_network,
            "t": self._toggle_stend,
        }
        handler = handlers.get(key)
        if handler:
            handler()

    def _set_color(self, key, rgba):
        self.ui_callback(key, rgba)

    def _refresh_all_colors(self):
        gray = (0.35, 0.38, 0.45, 1)
        green = (0.20, 0.75, 0.35, 1)
        red = (0.75, 0.25, 0.25, 1)
        yellow = (0.95, 0.80, 0.15, 1)
        cyan = (0.20, 0.75, 0.85, 1)
        orange = (0.95, 0.55, 0.15, 1)
        lime = (0.55, 0.90, 0.25, 1)
        magenta = (0.85, 0.25, 0.75, 1)

        if self.elevator_level == 1:
            self._set_color("e", green)
            self._set_color("r", gray)
        elif self.elevator_level == 3:
            self._set_color("e", gray)
            self._set_color("r", green)
        else:
            self._set_color("e", gray)
            self._set_color("r", gray)

        self._set_color("y", green if self.mustache_on else red)
        self._set_color("p", green if self.pump_on else red)
        if self.filter_pulsing:
            self._set_color("f", yellow)
        elif self.filter_on:
            self._set_color("f", green)
        else:
            self._set_color("f", red)
        self._set_color("u", green if self.lift_last_ok == "up" else gray)
        self._set_color("j", green if self.lift_last_ok == "down" else gray)
        self._set_color("i", orange if self.use_wan else cyan)
        self._set_color("t", magenta if use_unified_stend else lime)

    def _toggle_stend(self):
        global use_unified_stend
        use_unified_stend = not use_unified_stend
        self._apply_endpoints()
        self._refresh_all_colors()
        stend = "StendRasb2" if use_unified_stend else "2 Pi"
        self.status_callback(f"Стенд: {stend}")
        threading.Thread(target=self._probe_after_switch, daemon=True).start()

    def _apply_endpoints(self):
        if use_unified_stend:
            host = WAN_HOST if self.use_wan else LOCAL_STEND_HOST
            self.link_stend.set_endpoint(host, PORT_STEND)
        else:
            if self.use_wan:
                h1, h2 = WAN_HOST, WAN_HOST
            else:
                h1, h2 = LOCAL_RASB1_HOST, LOCAL_RASB2_HOST
            self.link1.set_endpoint(h1, RASB1_PORT)
            self.link2.set_endpoint(h2, RASB2_PORT)

    def _toggle_network(self):
        self.use_wan = not self.use_wan
        self._apply_endpoints()
        self._refresh_all_colors()
        mode = "ИНТЕРНЕТ" if self.use_wan else "ЛОКАЛЬ"
        if use_unified_stend:
            host = WAN_HOST if self.use_wan else LOCAL_STEND_HOST
            self.status_callback(f"Сеть: {mode} | StendRasb2 {host}:{PORT_STEND}")
        else:
            h1 = WAN_HOST if self.use_wan else LOCAL_RASB1_HOST
            h2 = WAN_HOST if self.use_wan else LOCAL_RASB2_HOST
            self.status_callback(
                f"Сеть: {mode} | {h1}:{RASB1_PORT} / {h2}:{RASB2_PORT}"
            )
        threading.Thread(target=self._probe_after_switch, daemon=True).start()

    def _probe_after_switch(self):
        if use_unified_stend:
            ok = self.link_stend.send(
                "ONLINE", wait=True, coalesce_key="stend:keepalive"
            )
            if ok:
                mode = "ИНТЕРНЕТ" if self.use_wan else "ЛОКАЛЬ"
                self.status_callback(f"StendRasb2 ({mode}): связь OK")
            else:
                self.status_callback(
                    f"StendRasb2 нет связи: {self.link_stend._last_error or '?'}"
                )
            return
        ok1 = self.link1.send("ONLINE", wait=True, coalesce_key="rasb1:keepalive")
        ok2 = self.link2.send("ONLINE", wait=True, coalesce_key="rasb2:keepalive")
        if ok1 and ok2:
            mode = "ИНТЕРНЕТ" if self.use_wan else "ЛОКАЛЬ"
            self.status_callback(f"Сеть {mode}: связь OK")
            return
        parts = []
        if not ok1:
            parts.append(f"rasb1 {self.link1.host}:{self.link1.port}: {self.link1._last_error or '?'}")
        if not ok2:
            parts.append(f"rasb2 {self.link2.host}:{self.link2.port}: {self.link2._last_error or '?'}")
        self.status_callback("Нет связи: " + "; ".join(parts))

    def _elevator_forward(self):
        def worker():
            link = self._aux_link()
            if self.elevator_level == 1:
                self.elevator_level = 2
                ok = link.send("elevator 2", wait=True, coalesce_key="elevator")
                self.status_callback("Элеватор E: стоп" if ok else "Элеватор E: нет связи")
            elif self.elevator_level == 3:
                self.elevator_level = 2
                ok = link.send("elevator 2", wait=True, coalesce_key="elevator")
                self.status_callback("Элеватор: стоп после R" if ok else "Элеватор: нет связи")
            else:
                self.elevator_level = 1
                ok = link.send("elevator 1", wait=True, coalesce_key="elevator")
                self.status_callback("Элеватор E: вперёд" if ok else "Элеватор E: нет связи")
            Clock.schedule_once(lambda *_: self._refresh_all_colors())

        threading.Thread(target=worker, daemon=True).start()

    def _elevator_back(self):
        def worker():
            link = self._aux_link()
            if self.elevator_level == 3:
                self.elevator_level = 0
                ok = link.send("elevator 0", wait=True, coalesce_key="elevator")
                self.status_callback("Элеватор R: стоп" if ok else "Элеватор R: нет связи")
            elif self.elevator_level == 1:
                self.elevator_level = 0
                ok = link.send("elevator 0", wait=True, coalesce_key="elevator")
                self.status_callback("Элеватор: стоп после E" if ok else "Элеватор: нет связи")
            else:
                self.elevator_level = 3
                ok = link.send("elevator 3", wait=True, coalesce_key="elevator")
                self.status_callback("Элеватор R: назад" if ok else "Элеватор R: нет связи")
            Clock.schedule_once(lambda *_: self._refresh_all_colors())

        threading.Thread(target=worker, daemon=True).start()

    def _mustache(self):
        self.mustache_on = not self.mustache_on
        level = 1 if self.mustache_on else 0
        self._refresh_all_colors()

        def worker():
            ok = self._motor_link().send(
                f"mustache {level}", wait=True, coalesce_key="mustache"
            )
            self.status_callback(
                f"Усы Y: {'вкл' if level else 'выкл'}" if ok else "Усы Y: нет связи"
            )

        threading.Thread(target=worker, daemon=True).start()

    def _lift_up(self):
        if self.lift_last_ok == "up":
            self.status_callback("Подъёмники вверх уже были — сначала J")
            return

        self.lift_last_ok = "up"
        self._refresh_all_colors()
        self.status_callback("Подъёмники U: вверх…")

        def worker():
            ok = self._aux_link().send(
                f"lift 1 {LIFT_PULSE_SEC}", wait=True, coalesce_key="lift"
            )
            if ok:
                self.status_callback("Подъёмники U: вверх OK")
            else:
                self.lift_last_ok = None
                self.status_callback("Подъёмники U: нет связи")
                Clock.schedule_once(lambda *_: self._refresh_all_colors())

        threading.Thread(target=worker, daemon=True).start()

    def _lift_down(self):
        if self.lift_last_ok == "down":
            self.status_callback("Подъёмники вниз уже были — сначала U")
            return

        self.lift_last_ok = "down"
        self._refresh_all_colors()
        self.status_callback("Подъёмники J: вниз…")

        def worker():
            ok = self._aux_link().send(
                f"lift -1 {LIFT_PULSE_SEC}", wait=True, coalesce_key="lift"
            )
            if ok:
                self.status_callback("Подъёмники J: вниз OK")
            else:
                self.lift_last_ok = None
                self.status_callback("Подъёмники J: нет связи")
                Clock.schedule_once(lambda *_: self._refresh_all_colors())

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        self.pump_on = not self.pump_on
        level = 1 if self.pump_on else 0
        self._refresh_all_colors()

        def worker():
            ok = self._motor_link().send(
                f"pump {level}", wait=True, coalesce_key="pump"
            )
            self.status_callback(
                f"Помпа P: {'вкл' if level else 'выкл'}" if ok else "Помпа P: нет связи"
            )

        threading.Thread(target=worker, daemon=True).start()

    def _filter(self):
        if self._filter_dialog_open:
            self.status_callback("Диалог фильтра уже открыт")
            return

        if self.filter_on:
            self.filter_on = False
            self.filter_pulsing = False
            self._filter_stop.set()
            self._refresh_all_colors()
            self.status_callback("Фильтр F: выкл")
            return

        # как на Windows: включение + диалог интервала/импульса
        self._show_filter_dialog()

    def _show_filter_dialog(self):
        self._filter_dialog_open = True
        content = BoxLayout(orientation="vertical", spacing=dp(8), padding=dp(12))
        content.add_widget(
            Label(text="Интервал включения (сек)", size_hint_y=None, height=dp(28))
        )
        interval_input = TextInput(
            text=str(self.filter_interval),
            multiline=False,
            input_filter="float",
            size_hint_y=None,
            height=dp(40),
        )
        content.add_widget(interval_input)
        content.add_widget(
            Label(text="Длительность импульса (сек)", size_hint_y=None, height=dp(28))
        )
        pulse_input = TextInput(
            text=str(self.filter_pulse),
            multiline=False,
            input_filter="float",
            size_hint_y=None,
            height=dp(40),
        )
        content.add_widget(pulse_input)

        buttons = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        popup = Popup(
            title="Фильтр тонкой очистки",
            content=content,
            size_hint=(0.55, 0.55),
            auto_dismiss=False,
        )

        def on_ok(*_):
            try:
                interval = float(interval_input.text.replace(",", "."))
                pulse = float(pulse_input.text.replace(",", "."))
                if interval <= 0 or pulse <= 0:
                    raise ValueError("значения должны быть > 0")
            except Exception as exc:
                self.status_callback(f"Фильтр: неверные числа ({exc})")
                return
            self.filter_interval = interval
            self.filter_pulse = pulse
            self.filter_on = True
            self._filter_stop.clear()
            self._refresh_all_colors()
            self.status_callback(
                f"Фильтр F: вкл (интервал {interval}с, импульс {pulse}с)"
            )
            if self._filter_thread is None or not self._filter_thread.is_alive():
                self._filter_thread = threading.Thread(
                    target=self._filter_loop, daemon=True
                )
                self._filter_thread.start()
            self._filter_dialog_open = False
            popup.dismiss()

        def on_cancel(*_):
            self._filter_dialog_open = False
            self.status_callback("Фильтр F: отмена")
            popup.dismiss()

        buttons.add_widget(Button(text="OK", on_press=on_ok))
        buttons.add_widget(Button(text="Отмена", on_press=on_cancel))
        content.add_widget(buttons)
        popup.open()

    def _filter_loop(self):
        while self.filter_on and not self._filter_stop.is_set():
            interval = float(self.filter_interval)
            pulse = float(self.filter_pulse)
            if self._filter_stop.wait(interval):
                break
            if not self.filter_on:
                break
            self.filter_pulsing = True
            Clock.schedule_once(lambda *_: self._refresh_all_colors())
            self._motor_link().send("filter_relay 0", wait=True, coalesce_key="filter_relay")
            time.sleep(pulse)
            self._motor_link().send(
                "filter_relay 1", wait=True, coalesce_key="filter_relay"
            )
            self.filter_pulsing = False
            Clock.schedule_once(lambda *_: self._refresh_all_colors())
            time.sleep(0.2)

    def shutdown(self):
        self.filter_on = False
        self._filter_stop.set()


class RoundKeyButton(Widget):
    """Круглая кнопка с буквой (E, R, Y, …)."""

    key = StringProperty("")
    label = StringProperty("")
    fill_color = ListProperty([0.35, 0.38, 0.45, 1])
    pressed = BooleanProperty(False)

    def __init__(self, key, on_press, **kwargs):
        super().__init__(**kwargs)
        self.key = key
        self.label = key.upper()
        self._on_press = on_press
        with self.canvas:
            self._color = Color(*self.fill_color)
            self._circle = Ellipse()
            Color(0.92, 0.94, 0.98, 1)
        self._text = Label(
            text=self.label,
            font_size="22sp",
            bold=True,
            color=(0.95, 0.97, 1, 1),
            halign="center",
            valign="middle",
        )
        self._text.disabled = True  # не перехватывать касания
        self.add_widget(self._text)
        self.bind(
            pos=self._redraw,
            size=self._redraw,
            fill_color=self._on_color,
            label=self._on_label,
        )

    def _on_color(self, *_):
        self._color.rgba = self.fill_color
        self._redraw()

    def _on_label(self, *_):
        self._text.text = self.label

    def _redraw(self, *_):
        side = min(self.width, self.height) * 0.82
        x = self.center_x - side / 2
        y = self.center_y - side / 2
        self._circle.pos = (x, y)
        self._circle.size = (side, side)
        self._text.size = self.size
        self._text.pos = self.pos
        self._text.text_size = self.size

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self.pressed = True
            # лёгкая вспышка
            self.opacity = 0.7
            return True
        return super().on_touch_down(touch)

    def on_touch_up(self, touch):
        if self.pressed:
            self.pressed = False
            self.opacity = 1.0
            if self.collide_point(*touch.pos):
                self._on_press(self.key)
            return True
        return super().on_touch_up(touch)


class VerticalStick(Widget):
    """Стик с фиксацией скорости: отпустил палец — уровень остаётся."""

    level = NumericProperty(0)

    def __init__(self, on_level, **kwargs):
        super().__init__(**kwargs)
        self._on_level = on_level
        self._active_touch = None
        with self.canvas:
            Color(0.10, 0.12, 0.16, 1)
            self._track = RoundedRectangle(radius=[dp(28)])
            Color(0.35, 0.38, 0.45, 1)
            self._center = Line(width=dp(1.5))
            self._knob_color = Color(0.10, 0.65, 0.95, 1)
            self._knob = Ellipse()
        self._level_label = Label(
            text="0",
            font_size="18sp",
            bold=True,
            color=(0.95, 0.97, 1, 1),
            size_hint=(None, None),
        )
        self.add_widget(self._level_label)
        self.bind(pos=self._redraw, size=self._redraw, level=self._redraw)

    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos) and self._active_touch is None:
            self._active_touch = touch
            touch.grab(self)
            self._set_from_y(touch.y)
            return True
        return super().on_touch_down(touch)

    def on_touch_move(self, touch):
        if touch.grab_current is self:
            self._set_from_y(touch.y)
            return True
        return super().on_touch_move(touch)

    def on_touch_up(self, touch):
        if touch.grab_current is self:
            touch.ungrab(self)
            self._active_touch = None
            # фиксация: НЕ сбрасываем в 0 — скорость остаётся
            # нейтраль: подведи к центру и отпусти
            return True
        return super().on_touch_up(touch)

    def _set_from_y(self, touch_y):
        bottom, top = self._movement_bounds()
        span = max(1.0, top - bottom)
        normalized = (min(max(touch_y, bottom), top) - bottom) / span
        self._set_level(round((normalized * 2.0 - 1.0) * 3))

    def _set_level(self, level):
        level = max(-3, min(3, int(level)))
        if self.level != level:
            self.level = level
            self._on_level(level)

    def _movement_bounds(self):
        knob_radius = min(dp(44), self.width * 0.22)
        margin = dp(16) + knob_radius
        return self.y + margin, self.top - margin

    def _redraw(self, *_):
        track_width = min(dp(130), self.width * 0.7)
        track_x = self.center_x - track_width / 2
        self._track.pos = (track_x, self.y + dp(12))
        self._track.size = (track_width, max(dp(80), self.height - dp(24)))

        bottom, top = self._movement_bounds()
        center_y = (bottom + top) / 2
        self._center.points = [track_x, center_y, track_x + track_width, center_y]

        # активная скорость — зелёный knоb, нейтраль — синий
        if self.level == 0:
            self._knob_color.rgba = (0.10, 0.65, 0.95, 1)
        else:
            self._knob_color.rgba = (0.20, 0.85, 0.40, 1)

        knob_size = min(dp(72), self.width * 0.5)
        knob_y = center_y + (self.level / 3.0) * ((top - bottom) / 2)
        self._knob.pos = (self.center_x - knob_size / 2, knob_y - knob_size / 2)
        self._knob.size = (knob_size, knob_size)

        self._level_label.text = f"{self.level:+d}" if self.level else "0"
        self._level_label.size = (knob_size, knob_size)
        self._level_label.center = (
            self.center_x,
            knob_y,
        )


class AuxPad(GridLayout):
    """Сетка круглых кнопок между стиками."""

    def __init__(self, on_key, **kwargs):
        super().__init__(cols=3, spacing=dp(10), padding=dp(8), **kwargs)
        self.buttons = {}
        for key in ("e", "r", "y", "u", "j", "p", "f", "i", "t"):
            btn = RoundKeyButton(key=key, on_press=on_key, size_hint=(1, 1))
            self.buttons[key] = btn
            self.add_widget(btn)

    def set_color(self, key, rgba):
        btn = self.buttons.get(key.lower())
        if btn is not None:
            btn.fill_color = list(rgba)


class MotorPanel(BoxLayout):
    """Горизонтальный layout: левый стик | кнопки | правый стик."""

    def __init__(self, controller, aux, **kwargs):
        super().__init__(orientation="horizontal", padding=dp(10), spacing=dp(8), **kwargs)
        self.controller = controller
        self.aux = aux

        left_col = BoxLayout(orientation="vertical", size_hint_x=0.28, spacing=dp(4))
        left_col.add_widget(
            Label(text="[b]ЛЕВЫЙ[/b]", markup=True, size_hint_y=0.08, font_size="16sp")
        )
        self.left_stick = VerticalStick(
            on_level=lambda value: controller.set_level("left", value),
            size_hint_y=0.92,
        )
        left_col.add_widget(self.left_stick)

        center = BoxLayout(orientation="vertical", size_hint_x=0.44, spacing=dp(4))
        center.add_widget(
            Label(
                text="[b]E R Y U J P F I T[/b]",
                markup=True,
                size_hint_y=0.08,
                font_size="14sp",
            )
        )
        self.aux_pad = AuxPad(on_key=aux.press, size_hint_y=0.84)
        center.add_widget(self.aux_pad)
        self.status = Label(
            text="Готово",
            size_hint_y=0.08,
            font_size="13sp",
            color=(0.65, 0.75, 0.85, 1),
        )
        center.add_widget(self.status)

        right_col = BoxLayout(orientation="vertical", size_hint_x=0.28, spacing=dp(4))
        right_col.add_widget(
            Label(text="[b]ПРАВЫЙ[/b]", markup=True, size_hint_y=0.08, font_size="16sp")
        )
        self.right_stick = VerticalStick(
            on_level=lambda value: controller.set_level("right", value),
            size_hint_y=0.92,
        )
        right_col.add_widget(self.right_stick)

        self.add_widget(left_col)
        self.add_widget(center)
        self.add_widget(right_col)

        self.levels = Label(text="", size_hint=(None, None), size=(0, 0))
        self.left_stick.bind(level=self._update_levels)
        self.right_stick.bind(level=self._update_levels)

        # привязка цветов кнопок
        aux.ui_callback = self._on_aux_color
        aux._refresh_all_colors()

    def _on_aux_color(self, key, rgba):
        Clock.schedule_once(lambda *_: self.aux_pad.set_color(key, rgba))

    def _update_levels(self, *_):
        pass


class AtlantMotorApp(App):
    def build(self):
        self.title = "Atlant"
        try:
            Window.orientation = "landscape"
        except Exception:
            pass
        try:
            Window.fullscreen = "auto"
        except Exception:
            pass

        self.link1 = MotorLink(WAN_HOST, RASB1_PORT, name="rasb1")
        self.link2 = MotorLink(WAN_HOST, RASB2_PORT, name="rasb2")
        self.link_stend = MotorLink(WAN_HOST, PORT_STEND, name="stend")
        self.aux = AuxController(
            self.link1,
            self.link2,
            self.link_stend,
            status_callback=self._set_status,
            ui_callback=lambda *_: None,
        )
        # зафиксировать интернет-режим на старте
        self.aux.use_wan = True
        self.link1.set_endpoint(WAN_HOST, RASB1_PORT)
        self.link2.set_endpoint(WAN_HOST, RASB2_PORT)
        self.link_stend.set_endpoint(WAN_HOST, PORT_STEND)
        self.controller = MotorController(
            self.link1, self.link2, self.link_stend, self._set_status
        )
        self.panel = MotorPanel(self.controller, self.aux)
        return self.panel

    def _set_status(self, text):
        Clock.schedule_once(lambda _dt: setattr(self.panel.status, "text", text))

    def on_start(self):
        # повторно зафиксировать landscape после старта Activity
        try:
            Window.orientation = "landscape"
        except Exception:
            pass
        threading.Thread(target=self._probe_servers, daemon=True).start()

    def _probe_servers(self):
        """Сразу показать, до кого доходим (и точную ошибку OS)."""
        if use_unified_stend:
            h, p = self.link_stend.host, self.link_stend.port
            self._set_status(f"Проверка StendRasb2… {h}:{p}")
            ok = self.link_stend.send("ONLINE", wait=True, coalesce_key="stend:keepalive")
            if ok:
                self._set_status("Связь OK: StendRasb2")
            else:
                self._set_status(
                    f"StendRasb2 нет связи: {self.link_stend._last_error or '?'}"
                )
            return
        h1, p1 = self.link1.host, self.link1.port
        h2, p2 = self.link2.host, self.link2.port
        self._set_status(f"Проверка… {h1}:{p1} / {h2}:{p2}")
        ok1 = self.link1.send("ONLINE", wait=True, coalesce_key="rasb1:keepalive")
        ok2 = self.link2.send("ONLINE", wait=True, coalesce_key="rasb2:keepalive")
        if ok1 and ok2:
            self._set_status("Связь OK: rasb1 и rasb2")
            return
        parts = []
        if not ok1:
            parts.append(f"rasb1 {h1}:{p1}: {self.link1._last_error or '?'}")
        if not ok2:
            parts.append(f"rasb2 {h2}:{p2}: {self.link2._last_error or '?'}")
        self._set_status("Нет связи: " + "; ".join(parts))

    def on_stop(self):
        self.aux.shutdown()
        self.controller.shutdown()


if __name__ == "__main__":
    AtlantMotorApp().run()
