import tkinter as tk
from tkinter import simpledialog
import keyboard
import threading
import time
from threading import Thread
import queue
import socket
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# =============================================================================
# Надёжный TCP-канал (вшит из reliable_net)
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
                    # сливаем в уже стоящую в очереди/исполняемую задачу
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

        # ВАЖНО: ждать только ВНЕ coalesce_lock — иначе deadlock с воркером
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
        """Смена хоста/порта с разрывом текущего сокета."""
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


# --- сеть: локальная LAN и внешний IP роутера (проброс портов) ---
WAN_HOST = "37.9.243.135"
# Локальные адреса Pi (как в port forwarding на роутере)
LOCAL_RASB1_HOST = "192.168.0.251"
LOCAL_RASB2_HOST = "192.168.8.20"
LOCAL_STEND_HOST = "192.168.8.20"  # единый StendRasb2 (server3 + serverrasb2)
PORT_RASB1 = 12345
PORT_RASB2 = 12346
PORT_STEND = 12345

# совместимость со старыми именами
hostname = WAN_HOST
hostname_local_rasb_1 = LOCAL_RASB1_HOST
hostname_local_rasb_2 = LOCAL_RASB2_HOST
port = PORT_RASB1
port2 = PORT_RASB2

use_wan = True  # False = локальная сеть, True = интернет через 37.9.243.135
use_unified_stend = False  # False = server3 + serverrasb2, True = StendRasb2

# rasb1 / rasb2 / stend — постоянное соединение, OK|id / DUP|id (старт в режиме интернет)
link_rasb1 = ReliableLink(WAN_HOST, PORT_RASB1, name="rasb1", one_shot=False)
link_rasb2 = ReliableLink(WAN_HOST, PORT_RASB2, name="rasb2", one_shot=False)
link_stend = ReliableLink(WAN_HOST, PORT_STEND, name="stend", one_shot=False)


def link_motor():
    """Канал для передач, реверса, помпы, усов, фильтра."""
    return link_stend if use_unified_stend else link_rasb1


def link_elevator():
    """Канал для элеватора и подъёмников."""
    return link_stend if use_unified_stend else link_rasb2


def current_endpoints():
    """Текущие (host, port) для rasb1/rasb2 или StendRasb2."""
    if use_unified_stend:
        host = WAN_HOST if use_wan else LOCAL_STEND_HOST
        return (host, PORT_STEND), (host, PORT_STEND)
    if use_wan:
        return (WAN_HOST, PORT_RASB1), (WAN_HOST, PORT_RASB2)
    return (LOCAL_RASB1_HOST, PORT_RASB1), (LOCAL_RASB2_HOST, PORT_RASB2)


def apply_network_mode():
    """Применить use_wan и use_unified_stend к каналам."""
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


def toggle_network_mode():
    """Переключить локаль ↔ интернет."""
    global use_wan
    use_wan = not use_wan
    return apply_network_mode()


def toggle_stend_mode():
    """Переключить server3+serverrasb2 ↔ StendRasb2."""
    global use_unified_stend
    use_unified_stend = not use_unified_stend
    return apply_network_mode()

filtrochistki_isOn = True
periodvkl = 1
timeon = 0.5

# UI-колбэки фильтра (регистрирует create_squares)
_filter_ui = {"root": None, "on_pulse": None}


def _notify_filter_pulse(active: bool) -> None:
    """Обновляет цвет кнопки фильтра при импульсе очистки (из фонового потока)."""
    root = _filter_ui.get("root")
    on_pulse = _filter_ui.get("on_pulse")
    if root is not None and on_pulse is not None:
        root.after(0, lambda a=active: on_pulse(a))


def lift(arg_lift, time_on, *, wait=False, callback=None):
    """Отправляет команду подъёмникам: lift <направление> <секунды>.

    По умолчанию wait=False — не блокирует очередь других команд.
    """
    message = f"lift {arg_lift} {time_on}"
    return link_elevator().send(
        message,
        wait=wait,
        coalesce_key="lift",
        callback=callback,
    )


def set_pump(state):
    """Помпа: state 0/1."""
    ok = link_motor().send(f"pump {state}", wait=True, coalesce_key="pump")
    print("pump ACK" if ok else "pump FAIL")
    return ok


def set_mustache(state):
    """Усы: state 0/1 (только GPIO8 на rasb1)."""
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

            # импульс очистки: кнопка меняет цвет на время включения
            _notify_filter_pulse(True)
            link_motor().send("filter_relay 0", wait=True, coalesce_key="filter_relay")
            time.sleep(pulse)
            link_motor().send("filter_relay 1", wait=True, coalesce_key="filter_relay")
            _notify_filter_pulse(False)
            time.sleep(1)
        else:
            time.sleep(0.5)


def call_arduino(text=""):
    # Отправка запроса
    # response = requests.get('http://192.168.0.170'+text)
    # response = requests.get('http://37.9.243.135', timeout=2)
    # # Получение текста ответа
    # Uon1stAkkumIdStart = response.text.find("Voltage")
    # Uon1stAkkumIdEnd = response.text.find("endU1")
    # string_value = response.text[Uon1stAkkumIdStart + 9:Uon1stAkkumIdStart + 14]
    # cleaned_string = string_value.strip()  # удаляем пробелы в начале и конце
    # number = float(cleaned_string) * 1.027
    return 1
    # print("Напряжение питания: " + str(number))


def Wake_On_Lan():
    while True:
        time.sleep(1)
        wake_UP()


def wake_UP():
    """ONLINE — на StendRasb2 одно соединение, иначе на обе Pi параллельно."""
    if use_unified_stend:
        if not link_stend.keepalive("ONLINE"):
            print("StendRasb2 офлайн")
        return

    results = {}

    def ping(name, link):
        results[name] = link.keepalive("ONLINE")

    t1 = Thread(target=ping, args=("rasb1", link_rasb1), daemon=True)
    t2 = Thread(target=ping, args=("rasb2", link_rasb2), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    if not results.get("rasb1"):
        print("Расбери 1 офлайн")
    if not results.get("rasb2"):
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
    """UI level -3..+3 → gear_right 0..3 + reverse_right."""
    global gear
    level = int(arg)
    if level <= 0:
        gear = abs(level)  # 0→0, -1→1, -2→2, -3→3
        reverse_dir = 1
    else:
        gear = level  # 1→1, 2→2, 3→3
        reverse_dir = -1
    print(gear)
    # wait=False + coalesce: быстрые нажатия через WAN не блокируют канал
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
    """UI level -3..+3 → gear_left 0..3 + reverse_left."""
    level = int(arg)
    if level <= 0:
        gear = abs(level)  # 0→0, -1→1, -2→2, -3→3
        reverse_dir = 1
    else:
        gear = level  # 1→1, 2→2, 3→3
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


def create_squares():
    # Создаём окно
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes('-topmost', True)
    root.attributes('-alpha', 0.85)  # Прозрачность 85%

    # Параметры
    square_size = 40
    num_squares = 7  # 7 квадратов для 3 передач вверх и 3 вниз (1 нейтраль + 3 вверх + 3 вниз)
    spacing = 10  # Расстояние между блоками

    # Дополнительные параметры для дополнительных элементов
    circle_size = 25
    circle_spacing = 45  # Расстояние между кружками по вертикали

    # Состояния элементов (элеватор: 0/2=стоп, 1=вперёд, 3=назад)
    elevator_level = 2  # старт в стопе (как arg==2 на сервере)
    mustache_level = 0  # 0 - красный, 1 - зелёный
    lift_level = 0  # 0 - красный, 1 - зелёный
    # Последняя успешно выполненная команда подъёмников: None | "up" | "down"
    # Повтор той же команды блокируется, пока не пройдёт противоположная.
    lift_last_ok = None
    lift_busy = False  # идёт ожидание ответа сервера
    pump_level = 0  # 0 - красный, 1 - зелёный
    filter_level = 0  # 0 - красный, 1 - зелёный

    # Параметры фильтра
    filter_interval = ""  # Интервал включения
    filter_period = ""  # Период включения

    # Параметры напряжения сети
    network_voltage = 26  # Напряжение сети (захардкожено 26)

    # Уровни для левого и правого блока (теперь от -3 до +3)
    left_level = 0  # -3, -2, -1, 0, 1, 2, 3
    right_level = 0  # -3, -2, -1, 0, 1, 2, 3

    # Флаг для предотвращения множественных диалогов
    dialog_active = False

    # Позиция в правом нижнем углу
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()

    # Общая ширина: два блока + промежуток + отступ для текста
    total_width = (square_size * 2) + spacing + 250

    # Высота строго по содержимому (кружки + тумблеры + передачи) — место под миникарту сверху
    # start_y=40, 5 кружков, напряжение, режим сети, режим стенда, блоки 7×40
    content_bottom = (
        40
        + 5 * circle_spacing
        + 10
        + circle_spacing
        + circle_spacing
        + 40
        + num_squares * square_size
        + 20
    )
    total_height = content_bottom

    # Позиционируем окно
    margin_x = 30
    margin_y = 50

    x_pos = screen_width - total_width - margin_x
    y_pos = screen_height - total_height - margin_y

    root.geometry(f"{total_width}x{total_height}+{x_pos}+{y_pos}")

    # Создаём холст с прозрачным фоном
    canvas = tk.Canvas(root, width=total_width, height=total_height,
                       highlightthickness=0, bg='black')
    canvas.pack()

    def create_triangle_up(x, y, size, color, outline):
        """Создаёт треугольник остриём вверх"""
        half_size = size // 2
        points = [
            x + half_size, y,
            x, y + size,
            x + size, y + size
        ]
        return canvas.create_polygon(points, fill=color, outline=outline, width=2)

    def create_triangle_down(x, y, size, color, outline):
        """Создаёт треугольник остриём вниз"""
        half_size = size // 2
        points = [
            x, y,
            x + size, y,
            x + half_size, y + size
        ]
        return canvas.create_polygon(points, fill=color, outline=outline, width=2)

    # Позиции для кружков
    start_y = 40
    circle_x = 50
    text_x = circle_x + circle_size + 10

    # Создаём 5 кружков вертикально слева от блоков
    elements = [
        {"name": "Элеватор", "key": "E", "type": "4state"},  # Изменено на 4state
        {"name": "Усы", "key": "Y", "type": "2state"},
        {"name": "Подъёмники", "key": "U", "type": "2state"},
        {"name": "Помпа", "key": "P", "type": "2state"},
        {"name": "Фильтр тонкой очистки", "key": "F", "type": "2state"}
    ]

    circles = []
    texts = []
    filter_text_id = None

    for i, elem in enumerate(elements):
        circle_y = start_y + i * circle_spacing
        circle = canvas.create_oval(
            circle_x - circle_size // 2, circle_y - circle_size // 2,
            circle_x + circle_size // 2, circle_y + circle_size // 2,
            fill="gray" if elem["type"] == "4state" else "red",
            outline="white", width=2
        )
        circles.append(circle)

        if elem["name"] == "Фильтр тонкой очистки":
            text = canvas.create_text(
                text_x, circle_y,
                text=f"{elem['name']} ({elem['key']})", fill="white",
                font=("Arial", 9, "bold"), anchor="w"
            )
            filter_text_id = text
        else:
            text = canvas.create_text(
                text_x, circle_y,
                text=f"{elem['name']} ({elem['key']})", fill="white",
                font=("Arial", 10, "bold"), anchor="w"
            )
        texts.append(text)

    # Создаём информационную панель напряжения сети
    voltage_y = start_y + 5 * circle_spacing + 10
    network_mode_y = voltage_y + circle_spacing
    stend_mode_y = network_mode_y + circle_spacing

    # Кружок для напряжения сети
    network_voltage_circle = canvas.create_oval(
        circle_x - circle_size // 2, voltage_y - circle_size // 2,
        circle_x + circle_size // 2, voltage_y + circle_size // 2,
        fill="green", outline="white", width=2
    )

    # Текст для напряжения сети
    network_voltage_text = canvas.create_text(
        text_x, voltage_y,
        text=f"Напряжение сети: {network_voltage} В", fill="white",
        font=("Arial", 10, "bold"), anchor="w"
    )

    # Подпись клавиши обновления
    canvas.create_text(
        text_x + 200, voltage_y,
        text="(N - обновить)", fill="white",
        font=("Arial", 8, "italic"), anchor="w"
    )

    # Тумблер локаль ↔ интернет (клавиша I; P занята помпой)
    network_mode_circle = canvas.create_oval(
        circle_x - circle_size // 2, network_mode_y - circle_size // 2,
        circle_x + circle_size // 2, network_mode_y + circle_size // 2,
        fill="cyan", outline="white", width=2
    )
    network_mode_text = canvas.create_text(
        text_x, network_mode_y,
        text="Сеть: ЛОКАЛЬ (I)", fill="white",
        font=("Arial", 10, "bold"), anchor="w"
    )

    # Тумблер server3+serverrasb2 ↔ StendRasb2 (клавиша T)
    stend_mode_circle = canvas.create_oval(
        circle_x - circle_size // 2, stend_mode_y - circle_size // 2,
        circle_x + circle_size // 2, stend_mode_y + circle_size // 2,
        fill="lime", outline="white", width=2
    )
    stend_mode_text = canvas.create_text(
        text_x, stend_mode_y,
        text="Стенд: 2 Pi (T)", fill="white",
        font=("Arial", 10, "bold"), anchor="w"
    )

    # Рассчитываем центр для блоков квадратов
    blocks_width = (square_size * 2) + spacing
    blocks_start_x = (total_width - blocks_width) // 2

    if blocks_start_x < text_x + 20:
        blocks_start_x = text_x + 20

    block_start_y = stend_mode_y + 40

    # Создаём левый блок
    left_block_x = blocks_start_x
    left_shapes = []
    for i in range(num_squares):
        y1 = (num_squares - 1 - i) * square_size + block_start_y

        if i == 0:
            shape = create_triangle_down(left_block_x, y1, square_size, "gray", "white")
        elif i == num_squares - 1:
            shape = create_triangle_up(left_block_x, y1, square_size, "gray", "white")
        else:
            shape = canvas.create_rectangle(left_block_x, y1,
                                            left_block_x + square_size, y1 + square_size,
                                            fill="gray", outline="white", width=2)
        left_shapes.append(shape)

    # Создаём правый блок
    right_block_x = left_block_x + square_size + spacing
    right_shapes = []
    for i in range(num_squares):
        y1 = (num_squares - 1 - i) * square_size + block_start_y

        if i == 0:
            shape = create_triangle_down(right_block_x, y1, square_size, "gray", "white")
        elif i == num_squares - 1:
            shape = create_triangle_up(right_block_x, y1, square_size, "gray", "white")
        else:
            shape = canvas.create_rectangle(right_block_x, y1,
                                            right_block_x + square_size, y1 + square_size,
                                            fill="gray", outline="white", width=2)
        right_shapes.append(shape)

    def update_network_mode_display():
        """Обновляет индикатор режима сети (локаль / интернет)."""
        if use_wan:
            canvas.itemconfig(network_mode_circle, fill="orange")
            canvas.itemconfig(
                network_mode_text,
                text=f"Сеть: ИНТЕРНЕТ (I) {WAN_HOST}",
            )
        else:
            canvas.itemconfig(network_mode_circle, fill="cyan")
            canvas.itemconfig(
                network_mode_text,
                text="Сеть: ЛОКАЛЬ (I) .21 / .20",
            )

    def update_stend_mode_display():
        """Обновляет индикатор режима стенда (2 Pi / StendRasb2)."""
        if use_unified_stend:
            canvas.itemconfig(stend_mode_circle, fill="magenta")
            canvas.itemconfig(
                stend_mode_text,
                text="Стенд: StendRasb2 (T)",
            )
        else:
            canvas.itemconfig(stend_mode_circle, fill="lime")
            canvas.itemconfig(
                stend_mode_text,
                text="Стенд: 2 Pi (T)",
            )

    def update_network_voltage_display():
        """Обновляет отображение напряжения сети и цвет кружка"""
        network_voltage = round(call_arduino(), 3)
        canvas.itemconfig(network_voltage_text, text=f"Напряжение сети: {network_voltage} В")

        if network_voltage <= 21:
            canvas.itemconfig(network_voltage_circle, fill="red")
            print(f"Напряжение сети: {network_voltage} В (КРАСНЫЙ - критическое значение!)")
        else:
            canvas.itemconfig(network_voltage_circle, fill="green")
            print(f"Напряжение сети: {network_voltage} В (норма)")

    def update_filter_display():
        """Обновляет отображение текста фильтра с параметрами"""
        if filter_interval and filter_period and filter_level == 1:
            new_text = f"Фильтр тонкой очистки (F) [{filter_interval} {filter_period}]"
        else:
            new_text = "Фильтр тонкой очистки (F)"

        canvas.itemconfig(filter_text_id, text=new_text)

    def on_filter_pulse(active: bool):
        """Цвет кнопки: жёлтый во время импульса очистки, иначе по filter_level."""
        nonlocal filter_level
        if active:
            canvas.itemconfig(circles[4], fill="yellow")
            print("Фильтр: импульс очистки ВКЛ (жёлтый)")
        else:
            update_circle(4, filter_level, "2state")
            print(f"Фильтр: импульс очистки ВЫКЛ → {['красный', 'зелёный'][filter_level]}")

    _filter_ui["root"] = root
    _filter_ui["on_pulse"] = on_filter_pulse

    def update_circle(index, level, elem_type):
        """Обновляет цвет кружка по индексу"""
        if elem_type == "4state":
            # элеватор: 1/3 включён (вперёд/назад) — зелёный; 0/2 стоп — серый
            if level in (1, 3):
                color = "green"
            else:
                color = "gray"
        else:  # 2state
            if level == 0:
                color = "red"
            else:
                color = "green"
        canvas.itemconfig(circles[index], fill=color)

    def update_all_circles():
        update_circle(0, elevator_level, "4state")
        update_circle(1, mustache_level, "2state")
        update_circle(2, lift_level, "2state")
        update_circle(3, pump_level, "2state")
        update_circle(4, filter_level, "2state")

        elevator_status = {
            0: "стоп (серый)",
            1: "вперёд (зелёный)",
            2: "стоп (серый)",
            3: "назад (зелёный)",
        }[elevator_level]
        mustache_status = ['красный', 'зелёный'][mustache_level]
        lift_status = ['красный', 'зелёный'][lift_level]
        pump_status = ['красный', 'зелёный'][pump_level]
        filter_status = ['красный', 'зелёный'][filter_level]

        print(f"Элеватор: {elevator_status} | Усы: {mustache_status} | "
              f"Подъёмники: {lift_status} | Помпа: {pump_status} | "
              f"Фильтр: {filter_status} [{filter_interval} {filter_period}]")

    def update_squares(send: bool = True):
        # Обновляем левый блок (для 7 квадратов с индексами 0-6, центр на индексе 3)
        for i in range(num_squares):
            color = "gray"

            if left_level == -3:  # 3 передачи назад
                if i == 0 or i == 1 or i == 2:
                    color = "red"
            elif left_level == -2:  # 2 передачи назад
                if i == 1 or i == 2:
                    color = "red"
            elif left_level == -1:  # 1 передача назад
                if i == 2:
                    color = "red"
            elif left_level == 0:  # нейтраль
                if i == 3:
                    color = "yellow"  # Жёлтый цвет для нейтрали
            elif left_level == 1:  # 1 передача вперед
                if i == 4:
                    color = "green"
            elif left_level == 2:  # 2 передачи вперед
                if i == 4 or i == 5:
                    color = "green"
            elif left_level == 3:  # 3 передачи вперед
                if i == 4 or i == 5 or i == 6:
                    color = "green"

            canvas.itemconfig(left_shapes[i], fill=color)

        # Обновляем правый блок
        for i in range(num_squares):
            color = "gray"

            if right_level == -3:  # 3 передачи назад
                if i == 0 or i == 1 or i == 2:
                    color = "red"
            elif right_level == -2:  # 2 передачи назад
                if i == 1 or i == 2:
                    color = "red"
            elif right_level == -1:  # 1 передача назад
                if i == 2:
                    color = "red"
            elif right_level == 0:  # нейтраль
                if i == 3:
                    color = "yellow"  # Жёлтый цвет для нейтрали
            elif right_level == 1:  # 1 передача вперед
                if i == 4:
                    color = "green"
            elif right_level == 2:  # 2 передачи вперед
                if i == 4 or i == 5:
                    color = "green"
            elif right_level == 3:  # 3 передачи вперед
                if i == 4 or i == 5 or i == 6:
                    color = "green"

            canvas.itemconfig(right_shapes[i], fill=color)

        if send:
            Thread(target=set_gear_right, args=(right_level,), daemon=True).start()
            Thread(target=set_gear_left, args=(left_level,), daemon=True).start()

            left_status = {-3: "3 назад", -2: "2 назад", -1: "1 назад", 0: "нейтраль",
                           1: "1 вперед", 2: "2 вперед", 3: "3 вперед"}[left_level]
            right_status = {-3: "3 назад", -2: "2 назад", -1: "1 назад", 0: "нейтраль",
                            1: "1 вперед", 2: "2 вперед", 3: "3 вперед"}[right_level]

            print(f"Левый блок: {left_status} | Правый блок: {right_status}")

    def show_filter_dialog():
        """Показывает диалог для ввода параметров фильтра в отдельном потоке"""
        global dialog_active, periodvkl, timeon

        def ask_parameters():
            nonlocal filter_level, filter_interval, filter_period
            global periodvkl, timeon
            try:
                temp_root = tk.Tk()
                temp_root.withdraw()
                temp_root.attributes('-topmost', True)
                temp_root.lift()

                period_str = simpledialog.askstring(
                    "Интервал включения",
                    "Введите интервал включения (сек):",
                    parent=temp_root
                )

                timeon_str = simpledialog.askstring(
                    "Время включения",
                    "Введите время включения (сек):",
                    parent=temp_root
                )

                temp_root.destroy()

                if period_str and timeon_str:
                    periodvkl = float(period_str)
                    timeon = float(timeon_str)
                    filter_interval = timeon_str
                    filter_period = period_str
                    root.after(0, update_filter_display)
                    root.after(0, lambda: print(
                        f"Фильтр: интервал={periodvkl}с, импульс={timeon}с"
                    ))
                else:
                    global filtrochistki_isOn
                    filtrochistki_isOn = False
                    filter_level = 0
                    root.after(0, lambda: update_circle(4, filter_level, "2state"))
                    root.after(0, lambda: print("Ввод параметров отменён"))
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

    def on_up():
        nonlocal left_level, right_level
        if autopilot_blocks_motors():
            return
        if left_level < 3:
            left_level += 1
        if right_level < 3:
            right_level += 1
        update_squares()

    def on_down():
        nonlocal left_level, right_level
        if autopilot_blocks_motors():
            return
        if left_level > -3:
            left_level -= 1
        if right_level > -3:
            right_level -= 1
        update_squares()

    def on_left():
        nonlocal left_level, right_level
        if autopilot_blocks_motors():
            return
        if right_level < 3:
            right_level += 1
        if left_level > -3:
            left_level -= 1
        update_squares()

    def on_right():
        nonlocal left_level, right_level
        if autopilot_blocks_motors():
            return
        if right_level > -3:
            right_level -= 1
        if left_level < 3:
            left_level += 1
        update_squares()

    def on_space():
        nonlocal left_level, right_level
        if autopilot_blocks_motors():
            return
        left_level = 0
        right_level = 0
        update_squares()

    def on_left_up():
        """7 — повысить скорость только левой части."""
        nonlocal left_level
        if autopilot_blocks_motors():
            return
        if left_level < 3:
            left_level += 1
        update_squares()

    def on_left_down():
        """1 — понизить скорость только левой части."""
        nonlocal left_level
        if autopilot_blocks_motors():
            return
        if left_level > -3:
            left_level -= 1
        update_squares()

    def on_right_up():
        """9 — повысить скорость только правой части."""
        nonlocal right_level
        if autopilot_blocks_motors():
            return
        if right_level < 3:
            right_level += 1
        update_squares()

    def on_right_down():
        """3 — понизить скорость только правой части."""
        nonlocal right_level
        if autopilot_blocks_motors():
            return
        if right_level > -3:
            right_level -= 1
        update_squares()

    def on_elevator_key():
        """E — вперёд (1). Если уже едем или было назад — сначала/только стоп (оба HIGH)."""
        nonlocal elevator_level

        if elevator_level == 1:
            # повтор E: стоп
            elevator_level = 2
            update_circle(0, elevator_level, "4state")
            print("Элеватор: стоп (2) — оба HIGH")
            Thread(target=run_elevator_new, args=(2,), daemon=True).start()
            return

        if elevator_level == 3:
            # было назад (R) → только стоп, оба HIGH (не стартуем вперёд)
            elevator_level = 2
            update_circle(0, elevator_level, "4state")
            print("Элеватор: после R → стоп (2) — оба HIGH")
            Thread(target=run_elevator_new, args=(2,), daemon=True).start()
            return

        # стоп → вперёд (PIN_20 LOW)
        elevator_level = 1
        update_circle(0, elevator_level, "4state")
        print("Элеватор: вперёд (1)")
        Thread(target=run_elevator_new, args=(1,), daemon=True).start()

    def on_elevator_key_down():
        """R — назад (3). Если уже едем или было вперёд — сначала/только стоп (оба HIGH)."""
        nonlocal elevator_level

        if elevator_level == 3:
            # повтор R: стоп
            elevator_level = 0
            update_circle(0, elevator_level, "4state")
            print("Элеватор: стоп (0) — оба HIGH")
            Thread(target=run_elevator_new, args=(0,), daemon=True).start()
            return

        if elevator_level == 1:
            # было вперёд (E) → только стоп, оба HIGH (не стартуем назад)
            elevator_level = 0
            update_circle(0, elevator_level, "4state")
            print("Элеватор: после E → стоп (0) — оба HIGH")
            Thread(target=run_elevator_new, args=(0,), daemon=True).start()
            return

        # стоп → назад (PIN_21 LOW)
        elevator_level = 3
        update_circle(0, elevator_level, "4state")
        print("Элеватор: назад (3)")
        Thread(target=run_elevator_new, args=(3,), daemon=True).start()

    def on_mustache_key():
        nonlocal mustache_level
        mustache_level = (mustache_level + 1) % 2
        level = mustache_level
        update_circle(1, level, "2state")
        print(f"Усы: {['красный', 'зелёный'][level]}")

        def worker():
            set_mustache(level)

        Thread(target=worker, daemon=True).start()

    def on_lift_key_down():
        nonlocal lift_level, lift_last_ok, lift_busy

        if lift_last_ok == "down":
            print("Подъёмники вниз уже выполнены — сначала нажмите U (вверх)")
            return

        print("-1")
        time_on = "4"
        # сразу разрешаем другой ввод; ACK придёт в фоне
        lift_level = 0
        lift_last_ok = "down"
        update_circle(2, 0, "2state")
        print(f"Подъёмники: {['красный', 'зелёный'][lift_level]}")

        def on_done(ok, _resp):
            nonlocal lift_level, lift_last_ok, lift_busy
            lift_busy = False
            if not ok:
                lift_last_ok = None
                print("Подъёмники вниз: сервер не подтвердил команду")

        lift_busy = True
        lift("-1", time_on, wait=False, callback=on_done)

    def on_lift_key():
        nonlocal lift_level, lift_last_ok, lift_busy

        if lift_last_ok == "up":
            print("Подъёмники вверх уже выполнены — сначала нажмите J (вниз)")
            return

        print("1")
        time_on = "4"
        lift_level = 1
        lift_last_ok = "up"
        update_circle(2, 1, "2state")
        print(f"Подъёмники: {['красный', 'зелёный'][lift_level]}")

        def on_done(ok, _resp):
            nonlocal lift_level, lift_last_ok, lift_busy
            lift_busy = False
            if not ok:
                lift_last_ok = None
                print("Подъёмники вверх: сервер не подтвердил команду")

        lift_busy = True
        lift("1", time_on, wait=False, callback=on_done)

    def on_pump_key():
        nonlocal pump_level
        pump_level = (pump_level + 1) % 2
        level = pump_level
        update_circle(3, level, "2state")
        print(f"Помпа: {['красный', 'зелёный'][level]}")
        Thread(target=set_pump, args=(level,), daemon=True).start()

    def on_filter_key():
        nonlocal filter_level, filter_interval, filter_period, dialog_active
        global filtrochistki_isOn

        if dialog_active:
            print("Диалог уже открыт, подождите...")
            return

        old_level = filter_level
        new_level = (filter_level + 1) % 2

        if old_level == 0 and new_level == 1:
            filtrochistki_isOn = True
            dialog_active = True
            filter_level = new_level
            update_circle(4, filter_level, "2state")
            threading.Thread(target=show_filter_dialog, daemon=True).start()
        elif old_level == 1 and new_level == 0:
            filtrochistki_isOn = False
            filter_level = new_level
            filter_interval = ""
            filter_period = ""
            update_circle(4, filter_level, "2state")
            update_filter_display()
            print(f"Фильтр тонкой очистки: КРАСНЫЙ (параметры сброшены)")
        else:
            filter_level = new_level
            update_circle(4, filter_level, "2state")
            print(f"Фильтр тонкой очистки: {['красный', 'зелёный'][filter_level]}")

    def on_network_voltage_key():
        update_network_voltage_display()

    def on_network_mode_key():
        mode, ep1, ep2 = toggle_network_mode()
        update_network_mode_display()
        print(f"Переключено: {mode} | {ep1[0]}:{ep1[1]} / {ep2[0]}:{ep2[1]}")

    def on_stend_mode_key():
        toggle_stend_mode()
        update_stend_mode_display()
        update_network_mode_display()
        stend = "StendRasb2" if use_unified_stend else "server3+serverrasb2"
        print(f"Режим стенда: {stend}")

    def on_esc():
        root.destroy()

    # Регистрируем горячие клавиши
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
    keyboard.add_hotkey("n", on_network_voltage_key)
    keyboard.add_hotkey("i", on_network_mode_key)
    keyboard.add_hotkey("t", on_stend_mode_key)
    keyboard.add_hotkey("esc", on_esc)

    print("Программа запущена!")
    print("=" * 70)
    print("УПРАВЛЕНИЕ ОСНОВНЫМИ БЛОКАМИ:")
    print('⬆️ Стрелка "UP" - увеличить уровень ОБОИХ блоков (+1)')
    print('⬇️ Стрелка "DOWN" - уменьшить уровень ОБОИХ блоков (-1)')
    print('➡️ Стрелка "RIGHT" - правый +1, левый -1')
    print('⬅️ Стрелка "LEFT" - правый -1, левый +1')
    print('␣ ПРОБЕЛ "SPACE" - сброс обоих блоков на уровень 0')
    print('"7" - повысить только ЛЕВЫЙ блок (+1)')
    print('"1" - понизить только ЛЕВЫЙ блок (-1)')
    print('"9" - повысить только ПРАВЫЙ блок (+1)')
    print('"3" - понизить только ПРАВЫЙ блок (-1)')
    print("=" * 70)
    print("УПРАВЛЕНИЕ ДОПОЛНИТЕЛЬНЫМИ ЭЛЕМЕНТАМИ:")
    print('🟡 "E" - вперёд (1); если ехали назад — только стоп (оба HIGH)')
    print('🟡 "R" - назад (3); если ехали вперёд — только стоп (оба HIGH)')
    print('🟡 "Y" - переключение УСОВ (красный↔зелёный)')
    print('🟡 "U" - переключение ПОДЪЁМНИКОВ (красный↔зелёный)')
    print('🟡 "P" - переключение ПОМПЫ (красный↔зелёный)')
    print('🟡 "F" - переключение ФИЛЬТРА:')
    print('    - Красный → Зелёный: запрос параметров')
    print('    - Зелёный → Красный: сброс параметров')
    print('🟡 "N" - обновить отображение НАПРЯЖЕНИЯ СЕТИ')
    print('🌐 "I" - тумблер СЕТИ: локаль ↔ интернет (37.9.243.135)')
    print('🔧 "T" - тумблер СТЕНДА: 2 Pi (server3+serverrasb2) ↔ StendRasb2')
    print('"ESC" - выход')
    print("=" * 70)
    print(f"НАПРЯЖЕНИЕ СЕТИ: {network_voltage} В (критическое: 21 В)")
    print("=" * 70)

    # Начальное обновление
    update_network_mode_display()
    update_stend_mode_display()
    update_network_voltage_display()
    update_all_circles()
    update_squares()

    # Миникарта + автопилот (после update_squares — есть left/right_level)
    try:
        from show_map import MiniMapApp

        def sync_motor_ui(left: int, right: int) -> None:
            nonlocal left_level, right_level
            left_level = max(-3, min(3, int(left)))
            right_level = max(-3, min(3, int(right)))
            root.after(0, lambda: update_squares(send=False))

        root._minimap = MiniMapApp(
            master=root,
            place_above=(x_pos, y_pos, total_width, total_height),
            motor_api={
                "set_left": set_gear_left,
                "set_right": set_gear_right,
                "sync_ui": sync_motor_ui,
            },
        )
    except Exception as e:
        print(f"Миникарта не запущена: {e}")

    root.mainloop()


if __name__ == "__main__":
    print("Надёжный канал: очередь + retry + keepalive + cmd_id")
    mode, ep1, ep2 = apply_network_mode()
    print(f"Старт: {mode} | rasb1 {ep1[0]}:{ep1[1]} | rasb2 {ep2[0]}:{ep2[1]}")

    Thread(target=Wake_On_Lan, daemon=True).start()
    Thread(target=create_squares, daemon=True).start()
    Thread(target=Start_filtr_ochistki, daemon=True).start()

    # держим главный поток живым, пока работает UI-поток
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        link_rasb1.close()
        link_rasb2.close()
        link_stend.close()
        print("Выход")