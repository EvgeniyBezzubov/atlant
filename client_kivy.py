"""Kivy-клиент Android: два вертикальных стика главных двигателей."""

import socket
import threading
import time
import uuid

from kivy.config import Config


Config.set("graphics", "orientation", "landscape")

from kivy.app import App
from kivy.clock import Clock
from kivy.graphics import Color, Ellipse, Line, RoundedRectangle
from kivy.metrics import dp
from kivy.properties import NumericProperty, StringProperty
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.widget import Widget

RASPBERRY_HOST = "192.168.0.169"
RASPBERRY_PORT = 12345
COMMAND_TIMEOUT = 2.0
COMMAND_RETRIES = 3


class MotorLink:
    """Постоянное TCP-соединение с подтверждением команд сервером."""

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self._socket = None
        self._buffer = b""
        self._lock = threading.Lock()

    def send(self, payload):
        command_id = uuid.uuid4().hex[:12]
        wire = f"{payload} id={command_id}\n".encode()

        with self._lock:
            for attempt in range(COMMAND_RETRIES):
                try:
                    sock = self._connect()
                    sock.sendall(wire)
                    reply = self._receive_line(sock)
                    if reply.startswith(("OK", "DUP")) and command_id in reply:
                        return True
                    raise OSError(f"неожиданный ответ: {reply!r}")
                except OSError:
                    self._disconnect()
                    if attempt + 1 < COMMAND_RETRIES:
                        time.sleep(0.25 * (2**attempt))
            return False

    def _connect(self):
        if self._socket is None:
            sock = socket.create_connection(
                (self.host, self.port), timeout=COMMAND_TIMEOUT
            )
            sock.settimeout(COMMAND_TIMEOUT)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
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


class MotorController:
    """Передаёт только последние положения стиков, не накапливая очередь."""

    def __init__(self, link, status_callback):
        self.link = link
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
        next_keepalive = time.monotonic() + 1.0
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
                self.status_callback(
                    f"{'Левый' if side == 'left' else 'Правый'}: "
                    f"{target:+d} — {'OK' if ok else 'НЕТ СВЯЗИ'}"
                )
                with self._condition:
                    if self._desired[side] != target:
                        self._dirty.add(side)

            if time.monotonic() >= next_keepalive:
                if not self.link.send("ONLINE"):
                    self.status_callback("Нет связи с Raspberry Pi")
                next_keepalive = time.monotonic() + 1.0

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


class VerticalStick(Widget):
    level = NumericProperty(0)
    title = StringProperty("")
    up_key = StringProperty("")
    down_key = StringProperty("")

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
        normalized = (min(max(touch_y, bottom), top) - bottom) / (top - bottom)
        self._set_level(round((normalized * 2.0 - 1.0) * 3))

    def _set_level(self, level):
        level = max(-3, min(3, int(level)))
        if self.level != level:
            self.level = level
            self._on_level(level)

    def _movement_bounds(self):
        knob_radius = min(dp(44), self.width * 0.22)
        margin = dp(20) + knob_radius
        return self.y + margin, self.top - margin

    def _redraw(self, *_):
        track_width = min(dp(150), self.width * 0.62)
        track_x = self.center_x - track_width / 2
        self._track.pos = (track_x, self.y + dp(20))
        self._track.size = (track_width, max(dp(80), self.height - dp(40)))

        bottom, top = self._movement_bounds()
        center_y = (bottom + top) / 2
        self._center.points = [
            track_x,
            center_y,
            track_x + track_width,
            center_y,
        ]

        knob_size = min(dp(88), self.width * 0.44)
        knob_y = center_y + (self.level / 3.0) * ((top - bottom) / 2)
        self._knob.pos = (self.center_x - knob_size / 2, knob_y - knob_size / 2)
        self._knob.size = (knob_size, knob_size)


class MotorPanel(BoxLayout):
    def __init__(self, controller, **kwargs):
        super().__init__(orientation="vertical", padding=dp(16), spacing=dp(8), **kwargs)
        self.controller = controller

        headings = BoxLayout(size_hint_y=0.13, spacing=dp(24))
        headings.add_widget(
            Label(text="[b]ЛЕВЫЙ[/b]   7 ↑  /  1 ↓", markup=True, font_size="20sp")
        )
        headings.add_widget(
            Label(text="[b]ПРАВЫЙ[/b]   9 ↑  /  3 ↓", markup=True, font_size="20sp")
        )
        self.add_widget(headings)

        sticks = BoxLayout(spacing=dp(24))
        self.left_stick = VerticalStick(
            title="Левый", up_key="7", down_key="1",
            on_level=lambda value: controller.set_level("left", value),
        )
        self.right_stick = VerticalStick(
            title="Правый", up_key="9", down_key="3",
            on_level=lambda value: controller.set_level("right", value),
        )
        sticks.add_widget(self.left_stick)
        sticks.add_widget(self.right_stick)
        self.add_widget(sticks)

        self.levels = Label(
            text="Левый: 0     Правый: 0",
            size_hint_y=0.08,
            font_size="18sp",
        )
        self.add_widget(self.levels)
        self.status = Label(
            text="Готово",
            size_hint_y=0.07,
            font_size="15sp",
            color=(0.65, 0.75, 0.85, 1),
        )
        self.add_widget(self.status)
        self.left_stick.bind(level=self._update_levels)
        self.right_stick.bind(level=self._update_levels)

    def _update_levels(self, *_):
        self.levels.text = (
            f"Левый: {self.left_stick.level:+d}     "
            f"Правый: {self.right_stick.level:+d}"
        )


class AtlantMotorApp(App):
    def build(self):
        self.title = "Atlant — главные двигатели"
        self.status_text = "Готово"
        self.link = MotorLink(RASPBERRY_HOST, RASPBERRY_PORT)
        self.controller = MotorController(self.link, self._set_status)
        self.panel = MotorPanel(self.controller)
        return self.panel

    def _set_status(self, text):
        self.status_text = text
        Clock.schedule_once(lambda _dt: setattr(self.panel.status, "text", text))

    def on_stop(self):
        self.controller.shutdown()


if __name__ == "__main__":
    AtlantMotorApp().run()
