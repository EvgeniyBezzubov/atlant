"""Автопоиск стенда Atlant в локальной сети (порты 12345 / 12346)."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Optional

PORT_MOTOR = 12345  # server3 / StendRasb1
PORT_AUX = 12346  # serverrasb2 (split)
CONNECT_TIMEOUT = 0.25
ONLINE_TIMEOUT = 0.7
SCAN_WORKERS = 80

_RE_WIN_DEFAULT = re.compile(
    r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s*$"
)

_manual_stend_mode = False


@dataclass(frozen=True)
class StendLayout:
    unified: bool
    motor_host: str
    aux_host: Optional[str] = None
    detail: str = ""


def reset_manual_stend_mode() -> None:
    global _manual_stend_mode
    _manual_stend_mode = False


def set_manual_stend_mode() -> None:
    global _manual_stend_mode
    _manual_stend_mode = True


def is_manual_stend_mode() -> bool:
    return _manual_stend_mode


def _ipv4_prefix(ip: str) -> Optional[str]:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    return ".".join(str(addr).split(".")[:3])


def _is_lan_ipv4(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return addr.is_private and not addr.is_loopback and not addr.is_link_local


def _fallback_local_ip() -> Optional[str]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
    except OSError:
        return None
    return ip if _is_lan_ipv4(ip) else None


def _gateways_windows() -> list[tuple[int, str]]:
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "encoding": "oem",
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(["route", "print", "-4"], timeout=5, **kwargs).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[tuple[int, str]] = []
    for line in out.splitlines():
        match = _RE_WIN_DEFAULT.match(line)
        if not match:
            continue
        gateway = match.group(1)
        metric = int(match.group(3))
        if gateway == "0.0.0.0":
            continue
        found.append((metric, gateway))
    return found


def _gateways_proc_net_route() -> list[tuple[int, str]]:
    path = "/proc/net/route"
    if not os.path.exists(path):
        return []
    found: list[tuple[int, str]] = []
    try:
        with open(path, encoding="ascii", errors="ignore") as fh:
            next(fh, None)
            for line in fh:
                parts = line.split()
                if len(parts) < 8:
                    continue
                destination, gateway_hex, flags_hex, metric_s = (
                    parts[1],
                    parts[2],
                    parts[3],
                    parts[6],
                )
                if destination != "00000000":
                    continue
                try:
                    flags = int(flags_hex, 16)
                    metric = int(metric_s)
                    gw_int = struct.unpack("<I", bytes.fromhex(gateway_hex))[0]
                except (ValueError, struct.error):
                    continue
                if not flags & 0x2:
                    continue
                gateway = str(ipaddress.IPv4Address(gw_int))
                if gateway == "0.0.0.0":
                    continue
                found.append((metric, gateway))
    except OSError:
        return []
    return found


def current_gateway() -> Optional[str]:
    """IPv4 default gateway of the active LAN (lowest metric, private first)."""
    routes = _gateways_windows() if sys.platform == "win32" else _gateways_proc_net_route()
    if not routes:
        routes = _gateways_proc_net_route()
    usable = [(metric, gw) for metric, gw in routes if _is_lan_ipv4(gw)]
    if not usable:
        usable = routes
    if not usable:
        return None
    usable.sort(key=lambda item: item[0])
    return usable[0][1]


def scan_subnet_prefixes() -> list[str]:
    """Подсеть текущего шлюза: 192.168.1.1 → 192.168.1.*"""
    gateway = current_gateway()
    prefix = _ipv4_prefix(gateway) if gateway else None
    if prefix:
        return [prefix]
    fallback = _fallback_local_ip()
    prefix = _ipv4_prefix(fallback) if fallback else None
    return [prefix] if prefix else []


def parse_ipv4(text: str) -> Optional[str]:
    """Нормализовать ввод пользователя в IPv4 (допускается суффикс :порт)."""
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.count(":") == 1:
        raw = raw.split(":", 1)[0].strip()
    try:
        return str(ipaddress.IPv4Address(raw))
    except ValueError:
        return None


def probe_online(host: str, port: int, *, timeout: float = ONLINE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"ONLINE\n")
            raw = sock.recv(128)
            if not raw:
                return False
            text = raw.decode(errors="replace").strip().upper()
            return text.startswith("OK") or text.startswith("DUP")
    except OSError:
        return False


def layout_from_manual_host(host: str) -> Optional[StendLayout]:
    """Собрать layout по введённому IP; ONLINE проверяется, но не обязателен."""
    parsed = parse_ipv4(host)
    if not parsed:
        return None
    motor_ok = probe_online(parsed, PORT_MOTOR)
    aux_ok = probe_online(parsed, PORT_AUX)
    if aux_ok:
        return StendLayout(
            unified=False,
            motor_host=parsed,
            aux_host=parsed,
            detail=f"вручную {parsed}:{PORT_MOTOR}/{PORT_AUX}",
        )
    note = "" if motor_ok else " (нет ответа ONLINE)"
    return StendLayout(
        unified=True,
        motor_host=parsed,
        detail=f"вручную {parsed}:{PORT_MOTOR}{note}",
    )


def _pick_host(hosts: set[str]) -> Optional[str]:
    if not hosts:
        return None
    return sorted(hosts, key=lambda ip: tuple(int(p) for p in ip.split(".")))[0]


def _layout_from_sets(motor_hosts: set[str], aux_hosts: set[str]) -> Optional[StendLayout]:
    """Собрать layout только по реальным ответам ONLINE на каждом порту.

    rasb1 = хост, ответивший на 12345; rasb2 = хост, ответивший на 12346.
    Нельзя подставлять IP aux в motor (и наоборот): иначе две Pi с разными
    адресами схлопываются в один IP с разными портами.
    """
    motor_only = motor_hosts - aux_hosts
    aux_only = aux_hosts - motor_hosts
    both = motor_hosts & aux_hosts

    if aux_hosts:
        motor = _pick_host(motor_only) or _pick_host(both)
        aux = _pick_host(aux_only) or _pick_host(both)
        if not motor or not aux:
            return None
        return StendLayout(
            unified=False,
            motor_host=motor,
            aux_host=aux,
            detail=f"2 Pi: rasb1 {motor}:{PORT_MOTOR}, rasb2 {aux}:{PORT_AUX}",
        )

    motor = _pick_host(motor_hosts)
    if not motor:
        return None
    return StendLayout(
        unified=True,
        motor_host=motor,
        detail=f"Единый стенд: {motor}:{PORT_MOTOR}",
    )


def _scan_hosts(
    hosts: list[str],
    *,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[set[str], set[str]]:
    motor_hosts: set[str] = set()
    aux_hosts: set[str] = set()

    def check_host(ip: str) -> tuple[str, bool, bool]:
        return ip, probe_online(ip, PORT_MOTOR), probe_online(ip, PORT_AUX)

    done = 0
    total = len(hosts)
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        futures = {pool.submit(check_host, ip): ip for ip in hosts}
        for fut in as_completed(futures):
            done += 1
            if progress and done % 32 == 0:
                progress(f"Сканирование LAN… {done}/{total}")
            ip, motor_ok, aux_ok = fut.result()
            if motor_ok:
                motor_hosts.add(ip)
            if aux_ok:
                aux_hosts.add(ip)
    return motor_hosts, aux_hosts


def discover_stend(
    *,
    quick_hosts: tuple[str, ...] = (),
    progress: Optional[Callable[[str], None]] = None,
    verbose: bool = True,
) -> Optional[StendLayout]:
    """Найти стенд в LAN и определить режим (единый Pi или 2 Pi)."""
    motor_quick: set[str] = set()
    aux_quick: set[str] = set()
    for host in quick_hosts:
        if not host:
            continue
        if probe_online(host, PORT_MOTOR):
            motor_quick.add(host)
        if probe_online(host, PORT_AUX):
            aux_quick.add(host)

    # Быстрый путь только если нашли ответы на оба порта. Один aux
    # (rasb2) без motor нельзя принимать: раньше motor получал тот же IP.
    if motor_quick and aux_quick:
        quick_layout = _layout_from_sets(motor_quick, aux_quick)
        if quick_layout:
            if verbose:
                print(f"Автопоиск (быстро): {quick_layout.detail}")
            return quick_layout

    prefixes = scan_subnet_prefixes()
    if not prefixes:
        if verbose:
            print("Автопоиск: не удалось определить шлюз LAN")
        return None
    hosts = [f"{prefix}.{n}" for prefix in prefixes for n in range(1, 255)]
    gateway = current_gateway()
    if verbose:
        gw_text = f"шлюз {gateway}, " if gateway else ""
        print(f"Автопоиск: {gw_text}сканируем {prefixes[0]}.* ({len(hosts)} адресов)")
    if progress:
        progress(f"Сканирование {prefixes[0]}.* …")

    motor_hosts, aux_hosts = _scan_hosts(hosts, progress=progress)
    motor_hosts |= motor_quick
    aux_hosts |= aux_quick
    layout = _layout_from_sets(motor_hosts, aux_hosts)
    if layout:
        if verbose:
            print(f"Автопоиск: {layout.detail}")
        return layout

    if verbose:
        print("Автопоиск: стенд в LAN не найден")
    return None


def apply_layout(
    layout: StendLayout,
    targets: dict,
    *,
    respect_manual_mode: bool = True,
) -> None:
    """Обновить host-константы и режим стенда в dict модуля клиента."""
    targets["LOCAL_RASB1_HOST"] = layout.motor_host
    targets["LOCAL_STEND_HOST"] = layout.motor_host
    if layout.aux_host:
        targets["LOCAL_RASB2_HOST"] = layout.aux_host
    if "hostname_local_rasb_1" in targets:
        targets["hostname_local_rasb_1"] = layout.motor_host
    if "hostname_local_rasb_2" in targets and layout.aux_host:
        targets["hostname_local_rasb_2"] = layout.aux_host
    if not respect_manual_mode or not is_manual_stend_mode():
        targets["use_unified_stend"] = layout.unified
