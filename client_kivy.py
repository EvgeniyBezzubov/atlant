"""Kivy-клиент Android: ландшафт, стики двигателей + круглые кнопки E/R/Y/U/J/P/F."""

import socket
import threading
import time
import uuid

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
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.widget import Widget

# --- сеть: локаль и интернет (проброс портов на роутере) ---
WAN_HOST = "37.9.243.135"
LOCAL_RASB1_HOST = "192.168.8.21"
LOCAL_RASB2_HOST = "192.168.8.20"
RASB1_PORT = 12345
RASB2_PORT = 12346

# стартовые (локальный режим)
RASB1_HOST = LOCAL_RASB1_HOST
RASB2_HOST = LOCAL_RASB2_HOST

COMMAND_TIMEOUT = 5.0
COMMAND_RETRIES = 3
KEEPALIVE_INTERVAL = 1.0
LIFT_PULSE_SEC = "4"
FILTER_INTERVAL_SEC = 1.0
FILTER_PULSE_SEC = 0.5


class MotorLink:
    """Постоянное TCP-соединение с подтверждением (как ReliableLink в client.py)."""

    def __init__(self, host, port, name="link"):
        self.host = host
        self.port = port
        self.name = name
        self._socket = None
        self._buffer = b""
        self._lock = threading.Lock()
        self._last_error = ""

    def send(self, payload, *, require_cmd_id=True):
        body = payload.strip()
        command_id = uuid.uuid4().hex[:12]
        if body.upper() == "ONLINE":
            wire = b"ONLINE\n"
            require_cmd_id = False
        else:
            wire = f"{body} id={command_id}\n".encode()

        with self._lock:
            for attempt in range(COMMAND_RETRIES):
                try:
                    sock = self._connect()
                    sock.sendall(wire)
                    reply = self._receive_line(sock)
                    if self._is_ack(reply, command_id, require_cmd_id):
                        self._last_error = ""
                        return True
                    raise OSError(f"неожиданный ответ: {reply!r}")
                except Exception as exc:
                    self._last_error = str(exc)
                    self._disconnect()
                    if attempt + 1 < COMMAND_RETRIES:
                        time.sleep(0.25 * (2**attempt))
            return False

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

    def _connect(self):
        if self._socket is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(COMMAND_TIMEOUT)
            sock.connect((self.host, self.port))
            # На Android часть setsockopt может давать EPERM — не валим соединение
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
        with self._lock:
            self._disconnect()

    def set_endpoint(self, host, port):
        """Смена хоста/порта с разрывом текущего сокета."""
        with self._lock:
            self.host = host
            self.port = port
            self._disconnect()


class MotorController:
    """Стики двигателей → rasb1."""

    def __init__(self, link_rasb1, link_rasb2, status_callback):
        self.link = link_rasb1
        self.link_rasb2 = link_rasb2
        self.status_callback = status_callback
        self._condition = threading.Condition()
        self._desired = {"left": 0, "right": 0}
        self._applied = {"left": 0, "right": 0}
        self._dirty = set()
        self._running = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

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
                err = self.link._last_error
                detail = (
                    f" [{self.link.host}:{self.link.port}] {err}"
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
                ok1 = self.link.send("ONLINE")
                ok2 = self.link_rasb2.send("ONLINE")
                if not ok1 or not ok2:
                    parts = []
                    if not ok1:
                        parts.append(
                            f"rasb1 {self.link.host}:{self.link.port}: "
                            f"{self.link._last_error or '?'}"
                        )
                    if not ok2:
                        parts.append(
                            f"rasb2 {self.link_rasb2.host}:{self.link_rasb2.port}: "
                            f"{self.link_rasb2._last_error or '?'}"
                        )
                    self.status_callback("Нет связи: " + "; ".join(parts))
                next_keepalive = time.monotonic() + KEEPALIVE_INTERVAL

    def _apply_level(self, side, previous, target):
        gear_command = f"gear_{side}"
        reverse_command = f"reverse_{side}"

        if target == 0:
            return self.link.send(f"{gear_command} 0")

        changed_direction = previous != 0 and (previous > 0) != (target > 0)
        if changed_direction and not self.link.send(f"{gear_command} 0"):
            return False

        direction = -1 if target > 0 else 1
        return self.link.send(
            f"{reverse_command} {direction}"
        ) and self.link.send(f"{gear_command} {abs(target)}")

    def shutdown(self):
        with self._condition:
            self._running = False
            self._condition.notify()
        self._worker.join(timeout=1.0)
        self.link.send("gear_left 0")
        self.link.send("gear_right 0")
        self.link.close()
        self.link_rasb2.close()


class AuxController:
    """Логика кнопок E/R/Y/U/J/P/F/I как в client.py."""

    def __init__(self, link_rasb1, link_rasb2, status_callback, ui_callback):
        self.link1 = link_rasb1
        self.link2 = link_rasb2
        self.status_callback = status_callback
        self.ui_callback = ui_callback  # key -> color rgba

        self.elevator_level = 2  # стоп
        self.mustache_on = False
        self.pump_on = False
        self.filter_on = False
        self.filter_pulsing = False
        self.lift_last_ok = None
        self.lift_busy = False
        self.use_wan = False  # False=локаль, True=интернет
        self._filter_stop = threading.Event()
        self._filter_thread = None

        self._refresh_all_colors()

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

    def _toggle_network(self):
        self.use_wan = not self.use_wan
        if self.use_wan:
            h1, h2 = WAN_HOST, WAN_HOST
        else:
            h1, h2 = LOCAL_RASB1_HOST, LOCAL_RASB2_HOST
        self.link1.set_endpoint(h1, RASB1_PORT)
        self.link2.set_endpoint(h2, RASB2_PORT)
        self._refresh_all_colors()
        mode = "ИНТЕРНЕТ" if self.use_wan else "ЛОКАЛЬ"
        self.status_callback(
            f"Сеть: {mode} | {h1}:{RASB1_PORT} / {h2}:{RASB2_PORT}"
        )
        threading.Thread(target=self._probe_after_switch, daemon=True).start()

    def _probe_after_switch(self):
        ok1 = self.link1.send("ONLINE")
        ok2 = self.link2.send("ONLINE")
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
            if self.elevator_level == 1:
                self.elevator_level = 2
                ok = self.link2.send("elevator 2")
                self.status_callback("Элеватор E: стоп" if ok else "Элеватор E: нет связи")
            elif self.elevator_level == 3:
                self.elevator_level = 2
                ok = self.link2.send("elevator 2")
                self.status_callback("Элеватор: стоп после R" if ok else "Элеватор: нет связи")
            else:
                self.elevator_level = 1
                ok = self.link2.send("elevator 1")
                self.status_callback("Элеватор E: вперёд" if ok else "Элеватор E: нет связи")
            Clock.schedule_once(lambda *_: self._refresh_all_colors())

        threading.Thread(target=worker, daemon=True).start()

    def _elevator_back(self):
        def worker():
            if self.elevator_level == 3:
                self.elevator_level = 0
                ok = self.link2.send("elevator 0")
                self.status_callback("Элеватор R: стоп" if ok else "Элеватор R: нет связи")
            elif self.elevator_level == 1:
                self.elevator_level = 0
                ok = self.link2.send("elevator 0")
                self.status_callback("Элеватор: стоп после E" if ok else "Элеватор: нет связи")
            else:
                self.elevator_level = 3
                ok = self.link2.send("elevator 3")
                self.status_callback("Элеватор R: назад" if ok else "Элеватор R: нет связи")
            Clock.schedule_once(lambda *_: self._refresh_all_colors())

        threading.Thread(target=worker, daemon=True).start()

    def _mustache(self):
        self.mustache_on = not self.mustache_on
        level = 1 if self.mustache_on else 0
        self._refresh_all_colors()

        def worker():
            ok = self.link1.send(f"mustache {level}")
            self.status_callback(
                f"Усы Y: {'вкл' if level else 'выкл'}" if ok else "Усы Y: нет связи"
            )

        threading.Thread(target=worker, daemon=True).start()

    def _lift_up(self):
        if self.lift_last_ok == "up":
            self.status_callback("Подъёмники вверх уже были — сначала J")
            return

        # UI сразу; сеть в фоне — не блокируем ввод на время импульса
        self.lift_last_ok = "up"
        self._refresh_all_colors()
        self.status_callback("Подъёмники U: вверх…")

        def worker():
            ok = self.link2.send(f"lift 1 {LIFT_PULSE_SEC}")
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
            ok = self.link2.send(f"lift -1 {LIFT_PULSE_SEC}")
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
            ok = self.link1.send(f"pump {level}")
            self.status_callback(
                f"Помпа P: {'вкл' if level else 'выкл'}" if ok else "Помпа P: нет связи"
            )

        threading.Thread(target=worker, daemon=True).start()

    def _filter(self):
        if self.filter_on:
            self.filter_on = False
            self.filter_pulsing = False
            self._filter_stop.set()
            self._refresh_all_colors()
            self.status_callback("Фильтр F: выкл")
            return

        self.filter_on = True
        self._filter_stop.clear()
        self._refresh_all_colors()
        self.status_callback(
            f"Фильтр F: вкл (интервал {FILTER_INTERVAL_SEC}с, импульс {FILTER_PULSE_SEC}с)"
        )
        if self._filter_thread is None or not self._filter_thread.is_alive():
            self._filter_thread = threading.Thread(target=self._filter_loop, daemon=True)
            self._filter_thread.start()

    def _filter_loop(self):
        while self.filter_on and not self._filter_stop.is_set():
            if self._filter_stop.wait(FILTER_INTERVAL_SEC):
                break
            if not self.filter_on:
                break
            self.filter_pulsing = True
            Clock.schedule_once(lambda *_: self._refresh_all_colors())
            self.link1.send("filter_relay 0")
            time.sleep(FILTER_PULSE_SEC)
            self.link1.send("filter_relay 1")
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
            Color(0.10, 0.65, 0.95, 1)
            self._knob = Ellipse()
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
            self._set_level(0)
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

        knob_size = min(dp(72), self.width * 0.5)
        knob_y = center_y + (self.level / 3.0) * ((top - bottom) / 2)
        self._knob.pos = (self.center_x - knob_size / 2, knob_y - knob_size / 2)
        self._knob.size = (knob_size, knob_size)


class AuxPad(GridLayout):
    """Сетка круглых кнопок между стиками."""

    def __init__(self, on_key, **kwargs):
        super().__init__(cols=2, spacing=dp(10), padding=dp(8), **kwargs)
        self.buttons = {}
        for key in ("e", "r", "y", "u", "j", "p", "f", "i"):
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
                text="[b]E R Y U J P F I[/b]",
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

        self.link1 = MotorLink(RASB1_HOST, RASB1_PORT, name="rasb1")
        self.link2 = MotorLink(RASB2_HOST, RASB2_PORT, name="rasb2")
        self.aux = AuxController(
            self.link1,
            self.link2,
            status_callback=self._set_status,
            ui_callback=lambda *_: None,
        )
        self.controller = MotorController(self.link1, self.link2, self._set_status)
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
        h1, p1 = self.link1.host, self.link1.port
        h2, p2 = self.link2.host, self.link2.port
        self._set_status(f"Проверка… {h1}:{p1} / {h2}:{p2}")
        ok1 = self.link1.send("ONLINE")
        ok2 = self.link2.send("ONLINE")
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
