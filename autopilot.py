"""Автопилот: обход 4 углов зоны, сжатие, возврат домой."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

# Управление (уровни UI -3..+3)
TURN_GEAR = 1  # малые обороты — разворот
DRIVE_GEAR = 2  # 2-я скорость — ход
HEADING_OK_DEG = 12  # приемлемое отклонение курса
CORRECT_DEG = 8  # порог докрутки на ходу
WAYPOINT_RADIUS_M = 8  # точка пройдена
SHRINK_M = 10
MIN_SIDE_M = 10
LOOP_DT = 0.35


@dataclass
class LatLon:
    lat: float
    lon: float


def haversine_m(a: LatLon, b: LatLon) -> float:
    r = 6371000.0
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp = math.radians(b.lat - a.lat)
    dl = math.radians(b.lon - a.lon)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(x)))


def bearing_deg(a: LatLon, b: LatLon) -> float:
    """Азимут a→b, 0°=север, по часовой."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlon = math.radians(b.lon - a.lon)
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(target: float, current: float) -> float:
    """Кратчайшая разница target-current в (−180, 180]."""
    d = (target - current + 180.0) % 360.0 - 180.0
    return d if d != -180.0 else 180.0


def meters_to_deg(lat: float, north_m: float, east_m: float) -> tuple[float, float]:
    dlat = north_m / 111320.0
    dlon = east_m / (111320.0 * max(0.2, math.cos(math.radians(lat))))
    return dlat, dlon


def zone_corners(min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> list[LatLon]:
    """4 точки по краям: СЗ → СВ → ЮВ → ЮЗ."""
    return [
        LatLon(max_lat, min_lon),  # NW
        LatLon(max_lat, max_lon),  # NE
        LatLon(min_lat, max_lon),  # SE
        LatLon(min_lat, min_lon),  # SW
    ]


def zone_side_m(min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> float:
    mid = LatLon((min_lat + max_lat) / 2, (min_lon + max_lon) / 2)
    w = haversine_m(LatLon(mid.lat, min_lon), LatLon(mid.lat, max_lon))
    h = haversine_m(LatLon(min_lat, mid.lon), LatLon(max_lat, mid.lon))
    return min(w, h)


def shrink_zone(
    min_lat: float, max_lat: float, min_lon: float, max_lon: float, meters: float = SHRINK_M
) -> Optional[tuple[float, float, float, float]]:
    mid_lat = (min_lat + max_lat) / 2
    dlat, dlon = meters_to_deg(mid_lat, meters, meters)
    nmin_lat = min_lat + dlat
    nmax_lat = max_lat - dlat
    nmin_lon = min_lon + dlon
    nmax_lon = max_lon - dlon
    if nmin_lat >= nmax_lat or nmin_lon >= nmax_lon:
        return None
    if zone_side_m(nmin_lat, nmax_lat, nmin_lon, nmax_lon) < MIN_SIDE_M - 0.5:
        return None
    return nmin_lat, nmax_lat, nmin_lon, nmax_lon


MotorSet = Callable[[int, int], None]  # left_level, right_level
StatusCb = Callable[[str], None]
ZoneCb = Callable[[float, float, float, float], None]
DoneCb = Callable[[], None]


class Autopilot:
    """
    Цикл: к каждой точке — разворот на 1 передаче, ход на 2-й с докруткой;
    после 4 точек — сжатие зоны на 10 м; при стороне < 10 м — домой и стоп.
    """

    def __init__(
        self,
        set_motors: MotorSet,
        *,
        on_status: Optional[StatusCb] = None,
        on_zone: Optional[ZoneCb] = None,
        on_done: Optional[DoneCb] = None,
    ) -> None:
        self._set_motors = set_motors
        self._on_status = on_status or (lambda _s: None)
        self._on_zone = on_zone
        self._on_done = on_done

        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self.pos = LatLon(0.0, 0.0)
        self.heading = 0.0
        self.home: Optional[LatLon] = None

        self.min_lat = self.max_lat = 0.0
        self.min_lon = self.max_lon = 0.0
        self.waypoints: list[LatLon] = []
        self.wp_index = 0
        self.returning_home = False
        self._phase = "idle"  # turn | drive | idle

    def update_pose(self, lat: float, lon: float, heading: Optional[float]) -> None:
        with self._lock:
            self.pos = LatLon(lat, lon)
            if heading is not None:
                self.heading = heading % 360.0

    def set_home(self, lat: float, lon: float) -> None:
        with self._lock:
            self.home = LatLon(lat, lon)

    def set_zone(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> None:
        with self._lock:
            self.min_lat, self.max_lat = min_lat, max_lat
            self.min_lon, self.max_lon = min_lon, max_lon
            self.waypoints = zone_corners(min_lat, max_lat, min_lon, max_lon)
            self.wp_index = 0
            self.returning_home = False

    @property
    def active_waypoints(self) -> list[LatLon]:
        with self._lock:
            return list(self.waypoints)

    @property
    def current_target_index(self) -> int:
        with self._lock:
            return self.wp_index

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> str:
        with self._lock:
            if self._running:
                return "Уже запущено"
            if self.home is None:
                return "Укажите точку дома"
            if len(self.waypoints) < 4:
                return "Сначала выделите зону"
            if zone_side_m(self.min_lat, self.max_lat, self.min_lon, self.max_lon) < MIN_SIDE_M:
                return "Зона меньше минимума"
            self._running = True
            self.wp_index = 0
            self.returning_home = False
            self._phase = "turn"
        self._thread = threading.Thread(target=self._loop, name="Autopilot", daemon=True)
        self._thread.start()
        self._on_status("Автопилот: старт")
        return ""

    def stop(self, reason: str = "Стоп") -> None:
        self._running = False
        self._set_motors(0, 0)
        self._phase = "idle"
        self._on_status(reason)

    def _target(self) -> Optional[LatLon]:
        with self._lock:
            if self.returning_home:
                return self.home
            if 0 <= self.wp_index < len(self.waypoints):
                return self.waypoints[self.wp_index]
            return None

    def _apply_turn(self, err: float) -> None:
        # err > 0 → нужно вправо (по часовой) → левый вперёд, правый назад
        if err > 0:
            self._set_motors(TURN_GEAR, -TURN_GEAR)
        else:
            self._set_motors(-TURN_GEAR, TURN_GEAR)

    def _apply_drive(self, err: float) -> None:
        if abs(err) <= CORRECT_DEG:
            self._set_motors(DRIVE_GEAR, DRIVE_GEAR)
        elif err > 0:
            # докрутка вправо: чуть больше левый
            self._set_motors(DRIVE_GEAR, max(0, DRIVE_GEAR - 1))
        else:
            self._set_motors(max(0, DRIVE_GEAR - 1), DRIVE_GEAR)

    def _advance_after_waypoint(self) -> None:
        with self._lock:
            if self.returning_home:
                self._running = False
                self._phase = "idle"
                msg = "Дома. Маршрут завершён"
                done = True
            else:
                self.wp_index += 1
                done = False
                msg = ""
                if self.wp_index >= len(self.waypoints):
                    shrunk = shrink_zone(
                        self.min_lat, self.max_lat, self.min_lon, self.max_lon, SHRINK_M
                    )
                    if shrunk is None:
                        self.returning_home = True
                        self._phase = "turn"
                        msg = "Зона минимальна → домой"
                    else:
                        self.min_lat, self.max_lat, self.min_lon, self.max_lon = shrunk
                        self.waypoints = zone_corners(
                            self.min_lat, self.max_lat, self.min_lon, self.max_lon
                        )
                        self.wp_index = 0
                        self._phase = "turn"
                        msg = f"Зона −{SHRINK_M} м, новый круг"
                        if self._on_zone:
                            self._on_zone(
                                self.min_lat, self.max_lat, self.min_lon, self.max_lon
                            )
                else:
                    self._phase = "turn"
                    msg = f"Точка {self.wp_index}/4"

        self._set_motors(0, 0)
        if msg:
            self._on_status(msg)
        if done:
            self._set_motors(0, 0)
            self._on_status(msg)
            if self._on_done:
                self._on_done()

    def _loop(self) -> None:
        while self._running:
            with self._lock:
                pos = self.pos
                heading = self.heading
                phase = self._phase
            target = self._target()
            if target is None:
                self.stop("Нет цели")
                break

            dist = haversine_m(pos, target)
            brg = bearing_deg(pos, target)
            err = angle_diff(brg, heading)

            if dist <= WAYPOINT_RADIUS_M:
                self._on_status(f"Точка достигнута ({dist:.0f} м)")
                self._advance_after_waypoint()
                if not self._running:
                    break
                time.sleep(0.5)
                continue

            if phase == "turn":
                if abs(err) <= HEADING_OK_DEG:
                    with self._lock:
                        self._phase = "drive"
                    self._on_status(f"Курс ок → 2 передача ({brg:.0f}°)")
                    self._apply_drive(err)
                else:
                    self._on_status(f"Разворот {err:+.0f}° → {brg:.0f}°")
                    self._apply_turn(err)
            else:
                # на ходу при большом уходе — снова разворот
                if abs(err) > HEADING_OK_DEG * 2:
                    with self._lock:
                        self._phase = "turn"
                    self._on_status(f"Сход с курса {err:+.0f}°")
                    self._apply_turn(err)
                else:
                    self._on_status(f"Ход 2ск · {dist:.0f} м · err {err:+.0f}°")
                    self._apply_drive(err)

            time.sleep(LOOP_DT)

        self._set_motors(0, 0)
