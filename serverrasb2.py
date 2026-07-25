#!/usr/bin/env python
"""
Сервер Raspberry Pi (rasb2): элеватор / lift / ONLINE.
Постоянное TCP-соединение, cmd_id, ответы OK|id / DUP|id.

Скопируйте этот файл на Pi и запустите:
  python3 rasb2_server.py
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Optional

import RPi.GPIO as GPIO

# --- сеть ---
HOST = "192.168.0.168"
PORT = 12346
IDLE_CONN_TIMEOUT = 90.0  # закрыть клиента, если молчит дольше (ONLINE держит живым)
INACTIVITY_SEC = 5.0  # без команд → пины в HIGH

# --- GPIO ---
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
PIN_5 = 5
PIN_6 = 6
PIN_20 = 20
PIN_21 = 21
GPIO.setup(PIN_5, GPIO.OUT, initial=GPIO.HIGH)
GPIO.setup(PIN_6, GPIO.OUT, initial=GPIO.HIGH)
GPIO.setup(PIN_20, GPIO.OUT, initial=GPIO.HIGH)
GPIO.setup(PIN_21, GPIO.OUT, initial=GPIO.HIGH)

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
    """Все пины в HIGH при отсутствии любых команд (включая ONLINE)."""
    with gpio_lock:
        GPIO.output(PIN_5, GPIO.HIGH)
        GPIO.output(PIN_6, GPIO.HIGH)
        GPIO.output(PIN_20, GPIO.HIGH)
        GPIO.output(PIN_21, GPIO.HIGH)


def elevator(arg: int) -> None:
    with gpio_lock:
        if arg == 0:
            GPIO.output(PIN_20, GPIO.HIGH)
            GPIO.output(PIN_21, GPIO.HIGH)
        elif arg == 1:
            GPIO.output(PIN_20, GPIO.LOW)
            GPIO.output(PIN_21, GPIO.HIGH)
        elif arg == 2:
            GPIO.output(PIN_20, GPIO.HIGH)
            GPIO.output(PIN_21, GPIO.HIGH)
        elif arg == 3:
            GPIO.output(PIN_20, GPIO.HIGH)
            GPIO.output(PIN_21, GPIO.LOW)
        else:
            raise ValueError(f"elevator: неизвестное положение {arg}")
    print(f"elevator -> {arg}")


def lift(pos: int, duration_sec: float) -> None:
    """Импульс: LOW на duration_sec, затем всегда оба пина HIGH."""
    if duration_sec <= 0:
        raise ValueError(f"lift: время должно быть > 0, получено {duration_sec}")

    with gpio_lock:
        try:
            if pos == 1:
                GPIO.output(PIN_5, GPIO.LOW)
                GPIO.output(PIN_6, GPIO.HIGH)
            elif pos == -1:
                GPIO.output(PIN_5, GPIO.HIGH)
                GPIO.output(PIN_6, GPIO.LOW)
            else:
                raise ValueError(f"lift: неизвестное положение {pos}")

            time.sleep(duration_sec)
        finally:
            # даже при ошибке/прерывании пины гасим
            GPIO.output(PIN_5, GPIO.HIGH)
            GPIO.output(PIN_6, GPIO.HIGH)

    print(f"lift -> {pos} for {duration_sec}s (pins HIGH)")


def dispatch(payload: str) -> None:
    parts = payload.split()
    if not parts:
        return
    cmd = parts[0].lower()

    if cmd == "online":
        return

    if cmd == "elevator":
        if len(parts) < 2:
            raise ValueError("elevator: нет аргумента")
        elevator(int(parts[1]))
        return

    if cmd == "lift":
        if len(parts) < 3:
            raise ValueError("lift: нужны направление и время, пример: lift 1 1")
        lift(int(parts[1]), float(parts[2]))
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
            print(f"No commands for {INACTIVITY_SEC:.0f}s — all pins HIGH")


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
                # старый клиент мог не прислать \n — добить хвост буфера
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

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    enable_keepalive(server)
    server.bind((HOST, PORT))
    server.listen(8)
    print(f"server run on {HOST}:{PORT} (persistent)")

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
