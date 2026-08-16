#!/usr/bin/env python
"""
Сервер Raspberry Pi (rasb1 / server3): передачи, реверс, усы, реле.
Постоянное TCP-соединение, cmd_id, ответы OK|id / DUP|id / ERR|id|msg.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import RPi.GPIO as GPIO

# --- сеть ---
PORT = 12345


def get_local_ip() -> str:
    """Текущий IP хоста в локальной сети (без hardcode)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "0.0.0.0"


def resolve_bind_host() -> str:
    ip = get_local_ip()
    if ip and ip != "127.0.0.1":
        return ip
    return "0.0.0.0"
IDLE_CONN_TIMEOUT = 90.0  # закрыть клиента, если молчит дольше (ONLINE держит живым)
INACTIVITY_SEC = 5.0  # без команд → пины в HIGH

# --- GPIO pin numbers (BCM) ---
# 4/5 скорости сняты: не инициализируем GPIO5(2KOM), GPIO27(0.1KOM), GPIO6(2KOM), GPIO12(0.1KOM)
GPIO4 = 4  ###relle 13   4KOM 1st
GPIO17 = 17  ###relle 8 	revers2
GPIO22 = 22  ###relle 3		3.3kom 2nd
GPIO13 = 13  ###relle 11  5KOM  1st
GPIO19 = 19  ###relle 7	revers2
GPIO26 = 26  ###relle 12   4.5 KOM 1st
GPIO18 = 18  ###relle 4    4 kom 2nd
GPIO23 = 23  ###relle 5  4.5 KOM
GPIO24 = 24  ###relle 10 revers 1
GPIO25 = 25  ###relle 14  3.3 KOM  1st
GPIO16 = 16  ###relle 9revers 1
GPIO21 = 21  ###relle 6  5kom 2nd
GPIO3 = 3  ###relle
GPIO10 = 10  ### relle 10 niz
GPIO08 = 8  ###relle 12 niz
GPIO15 = 15  ###relle 9 niz

ALL_GPIO_PINS = [
    GPIO10, GPIO08, GPIO15, GPIO4, GPIO17,
    GPIO22, GPIO13, GPIO19, GPIO26, GPIO18,
    GPIO23, GPIO24, GPIO25, GPIO16, GPIO21,
    GPIO3,
]

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

for _pin in ALL_GPIO_PINS:
    GPIO.setup(_pin, GPIO.OUT, initial=GPIO.HIGH)

# --- состояние ---
last_command_time = time.time()
command_time_lock = threading.Lock()
monitoring_active = True
gpio_lock = threading.Lock()


# ---------- идемпотентность / разбор протокола ----------

class IdempotencyCache:
    def __init__(self, ttl_sec: float = 600.0, max_size: int = 5000):
        self.ttl_sec = ttl_sec
        self.max_size = max_size
        self._lock = threading.Lock()
        self._items: dict[str, float] = {}

    def is_duplicate(self, cmd_id: Optional[str]) -> bool:
        if not cmd_id:
            return False
        now = time.time()
        with self._lock:
            self._purge(now)
            return cmd_id in self._items

    def remember(self, cmd_id: Optional[str]) -> None:
        if not cmd_id:
            return
        now = time.time()
        with self._lock:
            self._purge(now)
            self._items[cmd_id] = now
            overflow = len(self._items) - self.max_size
            if overflow > 0:
                for k, _ in sorted(self._items.items(), key=lambda kv: kv[1])[:overflow]:
                    self._items.pop(k, None)

    def _purge(self, now: float) -> None:
        dead = [k for k, t in self._items.items() if now - t > self.ttl_sec]
        for k in dead:
            del self._items[k]


ID_CACHE = IdempotencyCache()


def parse_client_line(raw: bytes) -> tuple[Optional[str], str]:
    text = raw.decode(errors="replace").strip()
    if not text:
        return None, ""
    if text.startswith("id=") and "|" in text:
        head, payload = text.split("|", 1)
        return head[3:].strip() or None, payload.strip()
    tokens = text.split()
    cmd_id = None
    payload_tokens = []
    for tok in tokens:
        if tok.startswith("id=") and len(tok) > 3:
            cmd_id = tok[3:]
        else:
            payload_tokens.append(tok)
    return cmd_id, " ".join(payload_tokens)


def format_ack(cmd_id: Optional[str], duplicate: bool = False) -> bytes:
    tag = "DUP" if duplicate else "OK"
    if cmd_id:
        return f"{tag}|{cmd_id}\n".encode()
    return f"{tag}\n".encode()


def enable_keepalive(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    if hasattr(socket, "TCP_KEEPIDLE"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 3)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except OSError:
            pass


# ---------- логика устройства ----------

def update_last_command_time() -> None:
    global last_command_time
    with command_time_lock:
        last_command_time = time.time()


def set_pins_safe() -> None:
    with gpio_lock:
        for pin in ALL_GPIO_PINS:
            GPIO.output(pin, GPIO.HIGH)


def set_mustache(state: int) -> None:
    """Усы (GPIO8). state: 1=вкл, 0=выкл."""
    if state == 1:
        GPIO.output(GPIO08, GPIO.LOW)
    elif state == 0:
        GPIO.output(GPIO08, GPIO.HIGH)


def set_spare_out(state: int) -> None:
    """Запасной выход (GPIO15). state: 1=вкл, 0=выкл."""
    if state == 1:
        GPIO.output(GPIO15, GPIO.LOW)
    elif state == 0:
        GPIO.output(GPIO15, GPIO.HIGH)


def set_pump(state: int) -> None:
    """Помпа (GPIO10). state: 1=вкл, 0=выкл."""
    if state == 1:
        GPIO.output(GPIO10, GPIO.LOW)
    elif state == 0:
        GPIO.output(GPIO10, GPIO.HIGH)


def set_reverse_right(direction: int) -> None:
    """Реверс правого двигателя. 1=назад, -1=вперёд."""
    if direction == 1:
        GPIO.output(GPIO24, GPIO.HIGH)
        GPIO.output(GPIO16, GPIO.HIGH)
    elif direction == -1:
        GPIO.output(GPIO24, GPIO.LOW)
        GPIO.output(GPIO16, GPIO.LOW)


def set_reverse_left(direction: int) -> None:
    """Реверс левого двигателя. 1=назад, -1=вперёд."""
    if direction == 1:
        GPIO.output(GPIO17, GPIO.HIGH)
        GPIO.output(GPIO19, GPIO.HIGH)
    elif direction == -1:
        GPIO.output(GPIO17, GPIO.LOW)
        GPIO.output(GPIO19, GPIO.LOW)


def set_gear_right(level: int) -> None:
    """Передача правого двигателя: 0=нейтраль, 1..3=скорость."""
    if level == 0:
        GPIO.output(GPIO13, GPIO.HIGH)  # 5KOM
        GPIO.output(GPIO26, GPIO.HIGH)  # 4.5KOM
        GPIO.output(GPIO4, GPIO.HIGH)   # 4KOM
        GPIO.output(GPIO25, GPIO.HIGH)  # 3.3KOM
    elif level == 1:
        GPIO.output(GPIO13, GPIO.LOW)
        GPIO.output(GPIO26, GPIO.LOW)
        GPIO.output(GPIO4, GPIO.HIGH)
        GPIO.output(GPIO25, GPIO.HIGH)
    elif level == 2:
        GPIO.output(GPIO13, GPIO.LOW)
        GPIO.output(GPIO26, GPIO.HIGH)
        GPIO.output(GPIO4, GPIO.LOW)
        GPIO.output(GPIO25, GPIO.HIGH)
    elif level == 3:
        GPIO.output(GPIO13, GPIO.LOW)
        GPIO.output(GPIO26, GPIO.HIGH)
        GPIO.output(GPIO4, GPIO.HIGH)
        GPIO.output(GPIO25, GPIO.LOW)
    else:
        raise ValueError(f"gear_right: уровень {level} вне диапазона 0..3")


def set_gear_left(level: int) -> None:
    """Передача левого двигателя: 0=нейтраль, 1..3=скорость."""
    if level == 0:
        GPIO.output(GPIO21, GPIO.HIGH)  # 5KOM
        GPIO.output(GPIO23, GPIO.HIGH)  # 4.5KOM
        GPIO.output(GPIO18, GPIO.HIGH)  # 4KOM
        GPIO.output(GPIO22, GPIO.HIGH)  # 3.3KOM
    elif level == 1:
        GPIO.output(GPIO21, GPIO.LOW)
        GPIO.output(GPIO23, GPIO.LOW)
        GPIO.output(GPIO18, GPIO.HIGH)
        GPIO.output(GPIO22, GPIO.HIGH)
    elif level == 2:
        GPIO.output(GPIO21, GPIO.LOW)
        GPIO.output(GPIO23, GPIO.HIGH)
        GPIO.output(GPIO18, GPIO.LOW)
        GPIO.output(GPIO22, GPIO.HIGH)
    elif level == 3:
        GPIO.output(GPIO21, GPIO.LOW)
        GPIO.output(GPIO23, GPIO.HIGH)
        GPIO.output(GPIO18, GPIO.HIGH)
        GPIO.output(GPIO22, GPIO.LOW)
    else:
        raise ValueError(f"gear_left: уровень {level} вне диапазона 0..3")


def set_filter_relay(state: int) -> None:
    """Реле фильтра тонкой очистки. state: 1/0."""
    if state == 1:
        GPIO.output(GPIO3, GPIO.HIGH)
    elif state == 0:
        GPIO.output(GPIO3, GPIO.LOW)


def _require_arg(parts: list[str], cmd: str) -> int:
    if len(parts) < 2:
        raise ValueError(f"{cmd}: нет аргумента")
    return int(parts[1])


def dispatch(payload: str) -> None:
    parts = payload.split()
    if not parts:
        return
    cmd = parts[0].lower()

    if cmd == "online":
        return

    with gpio_lock:
        if cmd == "gear_right":
            set_gear_right(_require_arg(parts, cmd))
            return
        if cmd == "gear_left":
            set_gear_left(_require_arg(parts, cmd))
            return
        if cmd == "reverse_right":
            set_reverse_right(_require_arg(parts, cmd))
            return
        if cmd == "reverse_left":
            set_reverse_left(_require_arg(parts, cmd))
            return
        if cmd == "filter_relay":
            set_filter_relay(_require_arg(parts, cmd))
            return
        if cmd == "pump":
            set_pump(_require_arg(parts, cmd))
            return
        if cmd == "spare_out":
            set_spare_out(_require_arg(parts, cmd))
            return
        if cmd == "mustache":
            set_mustache(_require_arg(parts, cmd))
            return
        if cmd == "mustache_a":
            # совместимость со старым клиентом
            set_mustache(_require_arg(parts, cmd))
            return

    raise ValueError(f"неизвестная команда: {payload!r}")


def monitor_inactivity() -> None:
    global monitoring_active, last_command_time
    while monitoring_active:
        time.sleep(1)
        with command_time_lock:
            idle = time.time() - last_command_time
        if idle >= INACTIVITY_SEC:
            set_pins_safe()
            with command_time_lock:
                last_command_time = time.time()
            print(f"No commands for {INACTIVITY_SEC:.0f}s — pins HIGH")


# ---------- TCP: постоянное соединение ----------

def process_line(conn: socket.socket, line: bytes) -> None:
    if not line.strip():
        return

    cmd_id, payload = parse_client_line(line)
    update_last_command_time()
    print(f"<= {payload!r} id={cmd_id}")

    if cmd_id and ID_CACHE.is_duplicate(cmd_id):
        conn.sendall(format_ack(cmd_id, duplicate=True))
        print(f"=> DUP|{cmd_id}")
        return

    try:
        dispatch(payload)
        ID_CACHE.remember(cmd_id)
        conn.sendall(format_ack(cmd_id, duplicate=False))
        print(f"=> OK|{cmd_id or ''}")
    except Exception as e:
        err = f"ERR|{cmd_id or ''}|{e}\n"
        conn.sendall(err.encode())
        print(f"=> {err.strip()}")


def handle_client(conn: socket.socket, addr) -> None:
    print(f"client connected: {addr}")
    enable_keepalive(conn)
    conn.settimeout(IDLE_CONN_TIMEOUT)
    buf = b""
    try:
        while True:
            try:
                chunk = conn.recv(1024)
            except socket.timeout:
                print(f"client idle timeout: {addr}")
                break

            if not chunk:
                if buf.strip():
                    process_line(conn, buf)
                    buf = b""
                break

            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                process_line(conn, line)

    except ConnectionResetError:
        print(f"client reset: {addr}")
    finally:
        try:
            conn.close()
        except Exception:
            pass
        print(f"client closed: {addr}")


def main() -> None:
    monitor = threading.Thread(target=monitor_inactivity, daemon=True)
    monitor.start()

    host = resolve_bind_host()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_keepalive(server)
    server.bind((host, PORT))
    server.listen(8)
    print(f"server run on {host}:{PORT} (persistent)")

    try:
        while True:
            client, addr = server.accept()
            threading.Thread(
                target=handle_client, args=(client, addr), daemon=True
            ).start()
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        global monitoring_active
        monitoring_active = False
        try:
            server.close()
        except Exception:
            pass
        set_pins_safe()
        GPIO.cleanup()


if __name__ == "__main__":
    main()
