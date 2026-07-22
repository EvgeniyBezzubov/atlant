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

        if coalesce_key:
            with self._coalesce_lock:
                old = self._coalesce.get(coalesce_key)
                if old is not None and not old.done.is_set():
                    old.payload = payload
                    old.cmd_id = job.cmd_id
                    old.timeout = job.timeout
                    old.retries = job.retries
                    old.callback = callback or old.callback
                    if wait:
                        old.done.wait()
                        return old.success
                    return True
                self._coalesce[coalesce_key] = job

        self._q.put(job)

        if wait:
            job.done.wait()
            return job.success
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
hostname = "37.9.243.135"
hostname_local_rasb_1 = "192.168.0.169"
hostname_local_rasb_2 = "192.168.0.168"
port = 12345
port2 = 12346

# rasb1 / rasb2 — постоянное соединение, OK|id / DUP|id
link_rasb1 = ReliableLink(hostname_local_rasb_1, port, name="rasb1", one_shot=False)
link_rasb2 = ReliableLink(hostname_local_rasb_2, port2, name="rasb2", one_shot=False)

filtrochistki_isOn = True
periodvkl = 2400
timeon = 0.5


def lift(arg_lift, time_on):
    """Отправляет команду подъёмникам. True — сервер подтвердил (OK/DUP/любой ACK)."""
    message = "lift " + str(arg_lift)
    return link_rasb2.send(
        message,
        wait=True,
        coalesce_key="lift",
    )


def set_pump(state):
    """Помпа: state 0/1."""
    ok = link_rasb1.send(f"pump {state}", wait=True, coalesce_key="pump")
    print("pump ACK" if ok else "pump FAIL")
    return ok


def set_mustache(state):
    """Усы (оба канала): state 0/1."""
    ok_a = link_rasb1.send(f"mustache_a {state}", wait=True, coalesce_key="mustache_a")
    ok_b = link_rasb1.send(f"mustache_b {state}", wait=True, coalesce_key="mustache_b")
    ok = ok_a and ok_b
    print("mustache ACK" if ok else "mustache FAIL")
    return ok


def run_elevator_new(polozhenie):
    message = "elevator " + str(polozhenie)
    ok = link_rasb2.send(
        message,
        wait=True,
        coalesce_key="elevator",
    )
    print("Server elevator:", "OK" if ok else "FAIL")
    return ok


def Start_filtr_ochistki():
    global filtrochistki_isOn

    while True:
        if filtrochistki_isOn:
            time.sleep(periodvkl)
            link_rasb1.send("filter_relay 0", wait=True, coalesce_key="filter_relay")
            time.sleep(timeon)
            link_rasb1.send("filter_relay 1", wait=True, coalesce_key="filter_relay")
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
    if not link_rasb1.keepalive("ONLINE"):
        print("Расбери 1 офлайн")
    if not link_rasb2.keepalive("ONLINE"):
        print("Расбери 2 офлайн")


def set_reverse_left(direction):
    ok = link_rasb1.send(
        f"reverse_left {direction}",
        wait=True,
        coalesce_key="reverse_left",
    )
    if not ok:
        print("Попытка установки реверса левого двигателя неудачна")


def set_reverse_right(direction):
    ok = link_rasb1.send(
        f"reverse_right {direction}",
        wait=True,
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
    Thread(target=set_reverse_right, args=(reverse_dir,), daemon=True).start()
    link_rasb1.send(f"gear_right {gear}", wait=True, coalesce_key="gear_right")


def set_gear_left(arg):
    """UI level -3..+3 → gear_left 0..3 + reverse_left."""
    level = int(arg)
    if level <= 0:
        gear = abs(level)  # 0→0, -1→1, -2→2, -3→3
        reverse_dir = 1
    else:
        gear = level  # 1→1, 2→2, 3→3
        reverse_dir = -1
    Thread(target=set_reverse_left, args=(reverse_dir,), daemon=True).start()
    print(gear)
    link_rasb1.send(f"gear_left {gear}", wait=True, coalesce_key="gear_left")


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

    # Состояния элементов (4 состояния для элеватора: 0-серый1, 1-зелёный, 2-серый2, 3-красный)
    elevator_level = 1  # 0 - серый, 1 - серый, 2 - зелёный, 3 - красный
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

    # Высота: квадраты + отступ + кружки с подписями + доп. информация
    top_offset = 350
    total_height = (num_squares * square_size) + top_offset + 50

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

    # Рассчитываем центр для блоков квадратов
    blocks_width = (square_size * 2) + spacing
    blocks_start_x = (total_width - blocks_width) // 2

    if blocks_start_x < text_x + 20:
        blocks_start_x = text_x + 20

    block_start_y = voltage_y + 40

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

    def update_circle(index, level, elem_type):
        """Обновляет цвет кружка по индексу"""
        if elem_type == "4state":
            # 4 состояния: 0-серый, 1-зелёный, 2-серый, 3-красный
            if level == 1 or level == 3:
                color = "gray"
            elif level == 0:
                color = "green"
            elif level == 2:
                color = "red"
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

        elevator_status = ['зелёный', 'серый', 'красный', 'серый'][elevator_level]
        mustache_status = ['красный', 'зелёный'][mustache_level]
        lift_status = ['красный', 'зелёный'][lift_level]
        pump_status = ['красный', 'зелёный'][pump_level]
        filter_status = ['красный', 'зелёный'][filter_level]

        print(f"Элеватор: {elevator_status} | Усы: {mustache_status} | "
              f"Подъёмники: {lift_status} | Помпа: {pump_status} | "
              f"Фильтр: {filter_status} [{filter_interval} {filter_period}]")

    def update_squares():
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

        Thread(target=set_gear_right, args=(right_level,), daemon=True).start()
        Thread(target=set_gear_left, args=(left_level,), daemon=True).start()

        # Вывод статуса передач
        left_status = {-3: "3 назад", -2: "2 назад", -1: "1 назад", 0: "нейтраль",
                       1: "1 вперед", 2: "2 вперед", 3: "3 вперед"}[left_level]
        right_status = {-3: "3 назад", -2: "2 назад", -1: "1 назад", 0: "нейтраль",
                        1: "1 вперед", 2: "2 вперед", 3: "3 вперед"}[right_level]

        print(f"Левый блок: {left_status} | Правый блок: {right_status}")

    def show_filter_dialog():
        """Показывает диалог для ввода параметров фильтра в отдельном потоке"""
        global dialog_active

        def ask_parameters():
            nonlocal filter_level, filter_interval, filter_period
            try:
                temp_root = tk.Tk()
                temp_root.withdraw()
                temp_root.attributes('-topmost', True)
                temp_root.lift()

                periodvkl = simpledialog.askstring(
                    "Интервал включения",
                    "Введите интервал включения:",
                    parent=temp_root
                )

                timeon = simpledialog.askstring(
                    "Время включения",
                    "Введите время включения:",
                    parent=temp_root
                )

                temp_root.destroy()

                if periodvkl and timeon:

                    filter_interval = timeon
                    filter_period = periodvkl
                    root.after(0, update_filter_display)
                    root.after(0, lambda: print(f"Фильтр: интервал={timeon}, период={periodvkl}"))
                else:

                    filter_level = 0
                    root.after(0, lambda: update_circle(4, filter_level, "2state"))
                    root.after(0, lambda: print("Ввод параметров отменён"))
            except Exception as e:
                print(f"Ошибка: {e}")
            finally:
                dialog_active = False

        threading.Thread(target=ask_parameters, daemon=True).start()

    def on_up():
        nonlocal left_level, right_level
        if left_level < 3:
            left_level += 1
        if right_level < 3:
            right_level += 1
        update_squares()

    def on_down():
        nonlocal left_level, right_level
        if left_level > -3:
            left_level -= 1
        if right_level > -3:
            right_level -= 1
        update_squares()

    def on_left():
        nonlocal left_level, right_level
        if right_level < 3:
            right_level += 1
        if left_level > -3:
            left_level -= 1
        update_squares()

    def on_right():
        nonlocal left_level, right_level
        if right_level > -3:
            right_level -= 1
        if left_level < 3:
            left_level += 1
        update_squares()

    def on_space():
        nonlocal left_level, right_level
        left_level = 0
        right_level = 0
        update_squares()

    def on_elevator_key():
        nonlocal elevator_level
        # При первом нажатии: останавливаем элеватор (серый цвет, elevator_level = 1)
        # При повторном нажатии: запускаем в положение 2
        # Следующие нажатия: двигаемся в диапазоне 1-2
        elevator_level = (elevator_level + 1) % 4
        elevator_status_num = [1, 2, 1, 2]

        update_circle(0, elevator_status_num[elevator_level], "4state")
        slow_run_elevator_new = run_elevator_new
        slowfTread_slow_run_elevator_new = Thread(target=slow_run_elevator_new,
                                                  args=(elevator_status_num[elevator_level],))
        slowfTread_slow_run_elevator_new.start()

    # Исправленная функция on_elevator_key_down
    def on_elevator_key_down():
        nonlocal elevator_level
        elevator_level = (elevator_level + 1) % 4
        elevator_status_num = [1, 0, 1, 0]
        # При первом нажатии: переводим в положение elevator_level = 2
        # Следующие нажатия: двигаемся в диапазоне 2-3
        update_circle(0, elevator_status_num[elevator_level], "4state")
        slow_run_elevator_new = run_elevator_new
        slowfTread_slow_run_elevator_new = Thread(target=slow_run_elevator_new,
                                                  args=(elevator_status_num[elevator_level],))
        slowfTread_slow_run_elevator_new.start()

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

        if lift_busy:
            print("Подъёмники: ждём ответ сервера...")
            return
        if lift_last_ok == "down":
            print("Подъёмники вниз уже выполнены — сначала нажмите U (вверх)")
            return

        def worker():
            nonlocal lift_level, lift_last_ok, lift_busy
            lift_busy = True
            try:
                print("-1")
                time_on = "1"
                ok = lift("-1", time_on)
                if ok:
                    lift_level = 0
                    lift_last_ok = "down"
                    root.after(0, lambda: update_circle(2, 0, "2state"))
                    print(f"Подъёмники: {['красный', 'зелёный'][lift_level]}")
                else:
                    print("Подъёмники вниз: сервер не подтвердил команду")
            finally:
                lift_busy = False

        Thread(target=worker, daemon=True).start()

    def on_lift_key():
        nonlocal lift_level, lift_last_ok, lift_busy

        if lift_busy:
            print("Подъёмники: ждём ответ сервера...")
            return
        if lift_last_ok == "up":
            print("Подъёмники вверх уже выполнены — сначала нажмите J (вниз)")
            return

        def worker():
            nonlocal lift_level, lift_last_ok, lift_busy
            lift_busy = True
            try:
                print("1")
                time_on = "1"
                ok = lift("1",time_on)
                if ok:
                    lift_level = 1
                    lift_last_ok = "up"
                    root.after(0, lambda: update_circle(2, 1, "2state"))
                    print(f"Подъёмники: {['красный', 'зелёный'][lift_level]}")
                else:
                    print("Подъёмники вверх: сервер не подтвердил команду")
            finally:
                lift_busy = False

        Thread(target=worker, daemon=True).start()

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

    def on_esc():
        root.destroy()

    def gear1():
        gear = input("Enter your name: ")
        Thread(target=set_gear_left, args=(gear,), daemon=True).start()

    # Регистрируем горячие клавиши
    keyboard.add_hotkey("up", on_up)
    keyboard.add_hotkey("down", on_down)
    keyboard.add_hotkey("right", on_right)
    keyboard.add_hotkey("left", on_left)
    keyboard.add_hotkey("space", on_space)
    keyboard.add_hotkey("e", on_elevator_key)
    keyboard.add_hotkey("r", on_elevator_key_down)
    keyboard.add_hotkey("y", on_mustache_key)
    keyboard.add_hotkey("u", on_lift_key)
    keyboard.add_hotkey("j", on_lift_key_down)
    keyboard.add_hotkey("p", on_pump_key)
    keyboard.add_hotkey("f", on_filter_key)
    keyboard.add_hotkey("n", on_network_voltage_key)
    keyboard.add_hotkey("esc", on_esc)
    keyboard.add_hotkey("1", gear1)

    print("Программа запущена!")
    print("=" * 70)
    print("УПРАВЛЕНИЕ ОСНОВНЫМИ БЛОКАМИ:")
    print('⬆️ Стрелка "UP" - увеличить уровень ОБОИХ блоков (+1)')
    print('⬇️ Стрелка "DOWN" - уменьшить уровень ОБОИХ блоков (-1)')
    print('➡️ Стрелка "RIGHT" - правый +1, левый -1')
    print('⬅️ Стрелка "LEFT" - правый -1, левый +1')
    print('␣ ПРОБЕЛ "SPACE" - сброс обоих блоков на уровень 0')
    print("=" * 70)
    print("УПРАВЛЕНИЕ ДОПОЛНИТЕЛЬНЫМИ ЭЛЕМЕНТАМИ:")
    print('🟡 "E, R" - переключение ЭЛЕВАТОРА (серый→зелёный→серый→красный)')
    print('🟡 "Y" - переключение УСОВ (красный↔зелёный)')
    print('🟡 "U" - переключение ПОДЪЁМНИКОВ (красный↔зелёный)')
    print('🟡 "P" - переключение ПОМПЫ (красный↔зелёный)')
    print('🟡 "F" - переключение ФИЛЬТРА:')
    print('    - Красный → Зелёный: запрос параметров')
    print('    - Зелёный → Красный: сброс параметров')
    print('🟡 "N" - обновить отображение НАПРЯЖЕНИЯ СЕТИ')
    print('"ESC" - выход')
    print("=" * 70)
    print(f"НАПРЯЖЕНИЕ СЕТИ: {network_voltage} В (критическое: 21 В)")
    print("=" * 70)

    # Начальное обновление
    update_network_voltage_display()
    update_all_circles()
    update_squares()
    root.mainloop()


if __name__ == "__main__":
    print("Надёжный канал: очередь + retry + keepalive + cmd_id")
    print(f"  rasb1 {hostname_local_rasb_1}:{port}")
    print(f"  rasb2 {hostname_local_rasb_2}:{port2}")

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
        print("Выход")