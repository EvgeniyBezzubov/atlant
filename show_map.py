"""Миникарта: дисковый кэш тайлов, prefetch, резиновое панорамирование,
маркер-оверлей, прогрессивный рендер, кэш кадров PhotoImage.

Дополнительно:
  * follow-режим: карта автоматически центрируется на GPS, пока пользователь
    не начал сдвигать её вручную; после сдвига — «замораживается» на месте,
    пока не будет нажата кнопка «Центрировать на GPS» (подменю ☰);
  * увеличенная карта (360×360);
  * второстепенные кнопки спрятаны в подменю ☰.
"""

from __future__ import annotations

import io
import math
import os
import threading
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageTk

from autopilot import Autopilot, zone_corners

# ---------------------------------------------------------------------------
# Параметры
# ---------------------------------------------------------------------------

MAP_W, MAP_H = 360, 360            # было 220×220
DEFAULT_ZOOM = 15
MIN_ZOOM, MAX_ZOOM = 3, 18
TILE_SIZE = 256
MARGIN = 12
USER_AGENT = "AtlantMiniMap/1.0 (local overlay)"
GAP = 8
TILE_CACHE_MAX = 512
PREFETCH_RING = 1
TILE_WORKERS = 6

# Кнопочная панель (основная строка). Подменю раскрывается кнопкой ☰.
CHROME_H = 92
MENU_W = 160

FRAME_CACHE_MAX = 24
GPS_REDRAW_THRESHOLD_PX = 1.5
PAN_RERENDER_FRACTION = 0.6        # рендер во время drag при уходе > 60 % тайла


# ---------------------------------------------------------------------------
# Дисковый кэш тайлов
# ---------------------------------------------------------------------------

def _cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if not base:
        base = os.path.expanduser("~/.cache")
    p = Path(base) / "AtlantMiniMap" / "tiles"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        p = Path(__file__).with_name("_tile_cache")
        p.mkdir(exist_ok=True)
    return p


_TILE_DIR = _cache_root()


def _tile_path(x: int, y: int, zoom: int) -> Path:
    return _TILE_DIR / str(zoom) / str(x) / f"{y}.png"


def _load_tile_from_disk(x: int, y: int, zoom: int) -> Image.Image | None:
    path = _tile_path(x, y, zoom)
    try:
        if path.is_file():
            with path.open("rb") as fh:
                return Image.open(io.BytesIO(fh.read())).convert("RGB")
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    return None


def _save_tile_to_disk(x: int, y: int, zoom: int, tile: Image.Image) -> None:
    path = _tile_path(x, y, zoom)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".png.tmp")
        tile.save(tmp, format="PNG", optimize=False)
        tmp.replace(path)
    except Exception:
        pass


_tile_cache: "OrderedDict[tuple[int, int, int], Image.Image]" = OrderedDict()
_tile_lock = threading.Lock()
_tile_executor = ThreadPoolExecutor(max_workers=TILE_WORKERS, thread_name_prefix="tile")
_inflight: dict[tuple[int, int, int], threading.Event] = {}
_inflight_lock = threading.Lock()


def _cache_put(key: tuple[int, int, int], tile: Image.Image) -> None:
    with _tile_lock:
        _tile_cache[key] = tile
        _tile_cache.move_to_end(key)
        while len(_tile_cache) > TILE_CACHE_MAX:
            _tile_cache.popitem(last=False)


def _cache_get(key: tuple[int, int, int]) -> Image.Image | None:
    with _tile_lock:
        tile = _tile_cache.get(key)
        if tile is not None:
            _tile_cache.move_to_end(key)
        return tile


def _download_tile(x: int, y: int, zoom: int) -> Image.Image:
    n = 2 ** zoom
    x %= n
    y = max(0, min(n - 1, y))
    url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=8) as resp:
        tile = Image.open(io.BytesIO(resp.read())).convert("RGB")
    _save_tile_to_disk(x, y, zoom, tile)
    return tile


def get_tile_cached(x: int, y: int, zoom: int) -> Image.Image | None:
    n = 2 ** zoom
    x %= n
    y = max(0, min(n - 1, y))
    key = (zoom, x, y)
    tile = _cache_get(key)
    if tile is not None:
        return tile
    tile = _load_tile_from_disk(x, y, zoom)
    if tile is not None:
        _cache_put(key, tile)
    return tile


def fetch_tile(x: int, y: int, zoom: int) -> Image.Image:
    n = 2 ** zoom
    x %= n
    y = max(0, min(n - 1, y))
    key = (zoom, x, y)

    tile = _cache_get(key)
    if tile is not None:
        return tile
    tile = _load_tile_from_disk(x, y, zoom)
    if tile is not None:
        _cache_put(key, tile)
        return tile

    with _inflight_lock:
        ev = _inflight.get(key)
        if ev is None:
            ev = threading.Event()
            _inflight[key] = ev
            owner = True
        else:
            owner = False
    if not owner:
        ev.wait(timeout=12)
        tile = _cache_get(key)
        if tile is not None:
            return tile

    try:
        tile = _download_tile(x, y, zoom)
        _cache_put(key, tile)
        return tile
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        ev.set()


def prefetch_tiles(x0: int, y0: int, x1: int, y1: int, zoom: int) -> None:
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            n = 2 ** zoom
            tx2 = tx % n
            ty2 = max(0, min(n - 1, ty))
            key = (zoom, tx2, ty2)
            if _cache_get(key) is not None:
                continue
            if _tile_path(tx2, ty2, zoom).is_file():
                tile = _load_tile_from_disk(tx2, ty2, zoom)
                if tile is not None:
                    _cache_put(key, tile)
                continue
            _tile_executor.submit(_prefetch_one, tx2, ty2, zoom)


def _prefetch_one(x: int, y: int, zoom: int) -> None:
    key = (zoom, x, y)
    if _cache_get(key) is not None:
        return
    with _inflight_lock:
        if key in _inflight:
            return
        ev = threading.Event()
        _inflight[key] = ev
    try:
        tile = _download_tile(x, y, zoom)
        _cache_put(key, tile)
    except Exception:
        pass
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        ev.set()


# ---------------------------------------------------------------------------
# Геометрия
# ---------------------------------------------------------------------------

def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_to_latlon(x: float, y: float, zoom: int) -> tuple[float, float]:
    n = 2.0 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def view_origin(center_lat: float, center_lon: float, zoom: int) -> tuple[float, float]:
    cx, cy = latlon_to_tile(center_lat, center_lon, zoom)
    return cx * TILE_SIZE - MAP_W / 2, cy * TILE_SIZE - MAP_H / 2


def pixel_to_latlon(
    px: float, py: float, center_lat: float, center_lon: float, zoom: int
) -> tuple[float, float]:
    left, top = view_origin(center_lat, center_lon, zoom)
    return tile_to_latlon((left + px) / TILE_SIZE, (top + py) / TILE_SIZE, zoom)


def latlon_to_pixel(
    lat: float, lon: float, center_lat: float, center_lon: float, zoom: int
) -> tuple[float, float]:
    left, top = view_origin(center_lat, center_lon, zoom)
    tx, ty = latlon_to_tile(lat, lon, zoom)
    return tx * TILE_SIZE - left, ty * TILE_SIZE - top


def _latlon_to_pixel_float(
    lat: float, lon: float, center_lat: float, center_lon: float, zoom: int
) -> tuple[float, float]:
    return latlon_to_pixel(lat, lon, center_lat, center_lon, zoom)


# ---------------------------------------------------------------------------
# Маркер (canvas-item)
# ---------------------------------------------------------------------------

class CanvasMarker:
    def __init__(self, canvas: tk.Canvas) -> None:
        self.canvas = canvas
        self.circle = canvas.create_oval(
            -20, -20, -20, -20, fill="#dc2828", outline="white", width=2
        )
        self.line = canvas.create_line(
            -20, -20, -20, -20, fill="#ffdc32", width=2
        )
        self.arrow = canvas.create_polygon(
            -20, -20, -20, -20, -20, -20, fill="#ffdc32", outline="white"
        )
        self._visible = False
        self.hide()

    def hide(self) -> None:
        if self._visible:
            for item in (self.circle, self.line, self.arrow):
                self.canvas.itemconfig(item, state="hidden")
            self._visible = False

    def show(self) -> None:
        if not self._visible:
            for item in (self.circle, self.line, self.arrow):
                self.canvas.itemconfig(item, state="normal")
            self._visible = True

    def move(self, mx: float, my: float, heading_deg: float | None) -> None:
        r = 7
        self.canvas.coords(self.circle, mx - r, my - r, mx + r, my + r)

        if heading_deg is None:
            self.canvas.coords(self.line, mx, my - r - 3, mx, my + r + 3)
            self.canvas.coords(self.arrow, mx, my, mx, my, mx, my)
            self.canvas.itemconfig(self.arrow, state="hidden")
            self.canvas.itemconfig(self.line, state="normal")
            self.show()
            return

        rad = math.radians(heading_deg % 360.0)
        dx = math.sin(rad)
        dy = -math.cos(rad)
        length = 18
        tip_x = mx + dx * length
        tip_y = my + dy * length
        px, py = -dy, dx
        base = 6
        left = (mx - dx * 4 + px * base, my - dy * 4 + py * base)
        right = (mx - dx * 4 - px * base, my - dy * 4 - py * base)
        self.canvas.coords(self.line, mx, my, tip_x, tip_y)
        self.canvas.coords(self.arrow, tip_x, tip_y, left[0], left[1], right[0], right[1])
        self.canvas.itemconfig(self.arrow, state="normal")
        self.show()


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------

def _visible_tile_range(center_lat: float, center_lon: float, zoom: int):
    left, top = view_origin(center_lat, center_lon, zoom)
    x0 = math.floor(left / TILE_SIZE)
    y0 = math.floor(top / TILE_SIZE)
    x1 = math.floor((left + MAP_W) / TILE_SIZE)
    y1 = math.floor((top + MAP_H) / TILE_SIZE)
    return left, top, x0, y0, x1, y1


def render_map_fast(center_lat: float, center_lon: float, zoom: int) -> tuple[Image.Image, bool]:
    left, top, x0, y0, x1, y1 = _visible_tile_range(center_lat, center_lon, zoom)
    img = Image.new("RGB", (MAP_W, MAP_H), (40, 40, 40))
    complete = True
    missing: list[tuple[int, int]] = []

    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            tile = get_tile_cached(tx, ty, zoom)
            if tile is None:
                complete = False
                missing.append((tx, ty))
                continue
            img.paste(tile, (int(tx * TILE_SIZE - left), int(ty * TILE_SIZE - top)))

    for tx, ty in missing:
        px = int(tx * TILE_SIZE - left)
        py = int(ty * TILE_SIZE - top)
        ImageDraw.Draw(img).rectangle(
            (px, py, px + TILE_SIZE - 1, py + TILE_SIZE - 1), fill=(55, 55, 55)
        )

    _tile_executor.submit(
        prefetch_tiles,
        x0 - PREFETCH_RING, y0 - PREFETCH_RING,
        x1 + PREFETCH_RING, y1 + PREFETCH_RING,
        zoom,
    )
    return img, complete


def render_map_full(center_lat: float, center_lon: float, zoom: int) -> Image.Image:
    left, top, x0, y0, x1, y1 = _visible_tile_range(center_lat, center_lon, zoom)
    img = Image.new("RGB", (MAP_W, MAP_H), (40, 40, 40))

    futures = []
    coords: list[tuple[int, int]] = []
    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            tile = get_tile_cached(tx, ty, zoom)
            if tile is not None:
                img.paste(tile, (int(tx * TILE_SIZE - left), int(ty * TILE_SIZE - top)))
                continue
            futures.append(_tile_executor.submit(fetch_tile, tx, ty, zoom))
            coords.append((tx, ty))

    for (tx, ty), fut in zip(coords, futures):
        try:
            tile = fut.result(timeout=10)
        except Exception:
            tile = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (60, 60, 60))
        img.paste(tile, (int(tx * TILE_SIZE - left), int(ty * TILE_SIZE - top)))

    _tile_executor.submit(
        prefetch_tiles,
        x0 - PREFETCH_RING, y0 - PREFETCH_RING,
        x1 + PREFETCH_RING, y1 + PREFETCH_RING,
        zoom,
    )
    return img


# ---------------------------------------------------------------------------
# Парсинг координат
# ---------------------------------------------------------------------------

def _split_coord_and_heading(token: str) -> tuple[float, float | None]:
    token = token.strip()
    parts = token.split(".")
    if len(parts) >= 3:
        head = ".".join(parts[:-1])
        heading = float(parts[-1])
        return float(head), heading
    return float(token), None


def parse_coords(text: str) -> tuple[float, float, float | None]:
    raw = unquote(text.strip())
    comma_style = "," in raw
    if comma_style:
        chunks = [c.strip() for c in raw.split(",") if c.strip()]
    else:
        chunks = raw.split()

    heading: float | None = None
    if len(chunks) == 3:
        a, b, heading = float(chunks[0]), float(chunks[1]), float(chunks[2])
    elif len(chunks) == 2:
        a = float(chunks[0])
        b, heading = _split_coord_and_heading(chunks[1])
    else:
        raise ValueError("Нужны координаты [и угол]")

    if abs(a) > 90 and abs(b) <= 90:
        lon, lat = a, b
    elif abs(b) > 90 and abs(a) <= 90:
        lat, lon = a, b
    elif comma_style:
        lon, lat = a, b
    else:
        lat, lon = a, b

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("Координаты вне диапазона")
    if heading is not None:
        heading = heading % 360.0
    return lat, lon, heading


class Zone:
    __slots__ = ("min_lat", "max_lat", "min_lon", "max_lon")

    def __init__(self, lat1: float, lon1: float, lat2: float, lon2: float) -> None:
        self.min_lat = min(lat1, lat2)
        self.max_lat = max(lat1, lat2)
        self.min_lon = min(lon1, lon2)
        self.max_lon = max(lon1, lon2)

    def contains(self, lat: float, lon: float) -> bool:
        return self.min_lat <= lat <= self.max_lat and self.min_lon <= lon <= self.max_lon


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MiniMapApp:
    def __init__(
        self,
        master: tk.Misc | None = None,
        *,
        place_above: tuple[int, int, int, int] | None = None,
        motor_api: dict | None = None,
    ) -> None:
        self._owns_mainloop = master is None
        self._motor_api = motor_api or {}
        if master is None:
            self.root = tk.Tk()
        else:
            self.root = tk.Toplevel(master)
        self.root.overrideredirect(True)
        self.root.resizable(False, False)
        self.root.configure(bg="#1e1e1e")

        win_w, win_h = MAP_W + 16, MAP_H + CHROME_H
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        if place_above is not None:
            ax, ay, aw, ah = place_above
            x = ax + max(0, aw - win_w)
            y = ay - win_h - GAP
            if y < GAP:
                x = ax - win_w - GAP
                y = ay + ah - win_h
            y = max(GAP, min(y, sh - win_h - GAP))
            x = max(GAP, min(x, sw - win_w - GAP))
        else:
            x = max(0, sw - win_w - MARGIN)
            y = max(0, sh - win_h - MARGIN - 48)

        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        self.root.lift()
        self.root.after(200, lambda: self.root.attributes("-topmost", True))
        self.root.after(300, self.root.lift)

        bg, fg = "#1e1e1e", "#e8e8e8"
        self._bg, self._fg = bg, fg
        self._alert = False
        self.frame = tk.Frame(self.root, bg=bg, padx=4, pady=4)
        self.frame.pack(fill="both", expand=True)
        frame = self.frame

        # --- строка 1: координаты + компактный статус ---
        top_row = tk.Frame(frame, bg=bg)
        top_row.pack(fill="x", pady=(0, 3))

        self.entry = tk.Entry(
            top_row, font=("Consolas", 10), bg="#2d2d2d", fg=fg, insertbackground=fg,
            relief="flat", highlightthickness=1, highlightbackground="#555", highlightcolor="#888",
        )
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.insert(0, "131.955897%2C43.170572.10")
        self.entry.bind("<Return>", lambda _e: self.go_to_coords())
        self.entry.bind("<Control-v>", self._on_paste)
        self.entry.bind("<Control-V>", self._on_paste)
        self.entry.bind("<Shift-Insert>", self._on_paste)
        self.entry.bind("<<Paste>>", self._on_paste)
        self.entry.bind("<Button-3>", self._on_paste)
        self.root.bind("<Control-v>", self._on_paste)
        self.root.bind("<Control-V>", self._on_paste)

        # --- строка 2: статус + мелкие кнопки ---
        btn_row = tk.Frame(frame, bg=bg)
        btn_row.pack(fill="x", pady=(0, 3))
        self.btn_row = btn_row

        self.status = tk.Label(
            btn_row, text="колёсико зум · ЛКМ сдвиг · ПКМ зона",
            font=("Segoe UI", 7), bg=bg, fg="#aaa", anchor="w",
        )
        self.status.pack(side="left", fill="x", expand=True)

        # Кнопка меню (крайняя правая)
        self.menu_btn = tk.Button(
            btn_row, text="☰", command=self.toggle_menu, font=("Segoe UI", 11, "bold"),
            bg="#3a3a3a", fg=fg, relief="flat", padx=7, cursor="hand2",
        )
        self.menu_btn.pack(side="right")

        tk.Button(
            btn_row, text="−", command=self.zoom_out, font=("Segoe UI", 9, "bold"),
            bg="#3a3a3a", fg=fg, relief="flat", padx=6, cursor="hand2",
        ).pack(side="right", padx=(0, 2))

        tk.Button(
            btn_row, text="+", command=self.zoom_in, font=("Segoe UI", 9, "bold"),
            bg="#3a3a3a", fg=fg, relief="flat", padx=6, cursor="hand2",
        ).pack(side="right", padx=(0, 2))

        tk.Button(
            btn_row, text="OK", command=self.go_to_coords, font=("Segoe UI", 8),
            bg="#3a3a3a", fg=fg, relief="flat", padx=6, cursor="hand2",
        ).pack(side="right", padx=(0, 2))

        # Кнопка центрирования — вне подменю, потому что ей пользуются часто
        self.center_btn = tk.Button(
            btn_row, text="◎", command=self.center_on_gps, font=("Segoe UI", 11, "bold"),
            bg="#3a3a3a", fg=fg, relief="flat", padx=6, cursor="hand2",
        )
        self.center_btn.pack(side="right", padx=(0, 2))

        # --- canvas ---
        self.canvas = tk.Canvas(
            frame, width=MAP_W, height=MAP_H, bg="#111", highlightthickness=0, cursor="fleur"
        )
        self.canvas.pack()

        placeholder = Image.new("RGB", (MAP_W, MAP_H), (30, 30, 30))
        ImageDraw.Draw(placeholder).text(
            (MAP_W // 2 - 40, MAP_H // 2 - 8), "Загрузка…", fill=(180, 180, 180)
        )
        self._photo = ImageTk.PhotoImage(placeholder, master=self.root)
        self._img_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

        self.marker = CanvasMarker(self.canvas)

        self._zone_id: int | None = None
        self._drag_id: int | None = None
        self._wp_ids: list[int] = []
        self._wp_hits: list[tuple[float, float, float, float, int]] = []
        self._home_id: int | None = None

        # --- всплывающее меню ---
        self.menu: tk.Toplevel | None = None
        self.home_entry: tk.Entry | None = None   # создаётся при первом открытии меню

        # --- состояние карты ---
        self.pin_lat = 43.170572
        self.pin_lon = 131.955897
        self.pin_heading: float | None = 10.0
        self.zone: Zone | None = None
        self.center_lat = 43.170572
        self.center_lon = 131.955897
        self.zoom = DEFAULT_ZOOM
        self.home_lat: float | None = None
        self.home_lon: float | None = None

        # follow: пока пользователь не начал двигать карту вручную — true.
        # После панорамирования — false, пока не нажата кнопка «◎».
        self._follow = True

        self._zone_mode = False
        self._drag_kind: str | None = None
        self._drag_start: tuple[int, int] | None = None
        self._pan_origin: tuple[float, float] | None = None
        self._pan_dx = 0
        self._pan_dy = 0
        self._drag_move_last: tuple[int, int] | None = None
        self._load_gen = 0
        self._reload_after: str | None = None
        self._panning = False

        self._frame_cache: "OrderedDict[tuple[float, float, int], ImageTk.PhotoImage]" = OrderedDict()
        self._frame_cache_max = FRAME_CACHE_MAX
        self._last_marker_px: tuple[float, float] | None = None

        self.autopilot = Autopilot(
            self._set_motors,
            on_status=lambda s: self.root.after(0, lambda: self.status.config(text=s[:48])),
            on_zone=lambda a, b, c, d: self.root.after(0, lambda: self._on_zone_shrunk(a, b, c, d)),
            on_done=lambda: self.root.after(0, self._on_mission_done),
        )

        # Дом по умолчанию (тот же, что в старом home_entry)
        try:
            lat0, lon0, _ = parse_coords("131.955897%2C43.170572")
            self.home_lat, self.home_lon = lat0, lon0
            self.autopilot.set_home(lat0, lon0)
        except Exception:
            pass

        # --- привязки ---
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", self._on_zone_press)
        self.canvas.bind("<B3-Motion>", self._on_zone_motion)
        self.canvas.bind("<ButtonRelease-3>", self._on_zone_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._zoom_by(+1, e.x, e.y))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_by(-1, e.x, e.y))
        if self._owns_mainloop:
            self.root.bind("<Escape>", lambda _e: self.root.destroy())

        self._hook_keyboard_paste()
        print(f"MiniMap at ({x}, {y}) size {win_w}x{win_h}", flush=True)
        print(f"Tile cache: {_TILE_DIR}", flush=True)
        self.root.after(100, self.reload_view)

    # ------------------------------------------------------------------ меню

    def toggle_menu(self) -> None:
        if self.menu is not None and self.menu.winfo_exists():
            self.menu.destroy()
            self.menu = None
            return
        self._open_menu()

    def _open_menu(self) -> None:
        m = tk.Toplevel(self.root)
        self.menu = m
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.configure(bg="#2a2a2a")

        # Позиция: слева от кнопки ☰
        self.root.update_idletasks()
        bx = self.menu_btn.winfo_rootx()
        by = self.menu_btn.winfo_rooty() + self.menu_btn.winfo_height() + 2
        w = MENU_W
        h = 320
        sw = m.winfo_screenwidth()
        sh = m.winfo_screenheight()
        x = max(2, min(bx - w + self.menu_btn.winfo_width(), sw - w - 2))
        y = min(by, sh - h - 2)
        m.geometry(f"{w}x{h}+{x}+{y}")

        bg, fg = "#2a2a2a", "#e8e8e8"

        def row(label: str, cmd) -> None:
            b = tk.Button(
                m, text=label, command=lambda: (self._close_menu_and(cmd)),
                font=("Segoe UI", 9), bg="#333", fg=fg, activebackground="#444",
                activeforeground=fg, relief="flat", anchor="w", padx=10, cursor="hand2",
            )
            b.pack(fill="x", padx=6, pady=1)

        def section(label: str) -> None:
            tk.Label(
                m, text=label, font=("Segoe UI", 7, "bold"), bg=bg, fg="#888",
                anchor="w", padx=10,
            ).pack(fill="x", pady=(6, 1))

        # --- Адрес дома ---
        section("ДОМ")
        home_frame = tk.Frame(m, bg=bg)
        home_frame.pack(fill="x", padx=6, pady=(0, 4))
        self.home_entry = tk.Entry(
            home_frame, font=("Consolas", 9), bg="#2d2d2d", fg=fg, insertbackground=fg,
            relief="flat", highlightthickness=1, highlightbackground="#555", highlightcolor="#888",
        )
        self.home_entry.pack(side="left", fill="x", expand=True)
        self.home_entry.insert(0, "131.955897%2C43.170572")
        self.home_entry.bind("<Return>", lambda _e: self._apply_home())
        self.home_entry.bind("<Control-v>", lambda e: self._paste_into(self.home_entry) or "break")
        self.home_entry.bind("<Control-V>", lambda e: self._paste_into(self.home_entry) or "break")
        tk.Button(
            home_frame, text="→", command=self._apply_home, font=("Segoe UI", 9, "bold"),
            bg="#3a3a3a", fg=fg, relief="flat", padx=6, cursor="hand2",
        ).pack(side="right", padx=(3, 0))

        # --- Действия ---
        section("ДЕЙСТВИЯ")
        row("Вставить из буфера", self.paste_clipboard)
        row("Центрировать на GPS", self.center_on_gps)
        row("Зона (ЛКМ) — " + ("вкл" if self._zone_mode else "выкл"), self.toggle_zone_mode)
        row("Сбросить зону", self.clear_zone)

        section("МИССИЯ")
        row("СТАРТ", self.start_mission)
        row("СТОП", self.stop_mission)

        section("ЗАКРЫТЬ")
        row("Закрыть карту", self._close_app)

        # закрытие по клику вне меню
        m.bind("<FocusOut>", lambda _e: self._close_menu_if_unfocused())
        m.focus_set()
        self.root.after(150, m.focus_force)

    def _close_menu_if_unfocused(self) -> None:
        if self.menu is None:
            return
        try:
            focused = self.root.focus_get()
        except Exception:
            focused = None
        if focused is None:
            self._close_menu()

    def _close_menu(self) -> None:
        if self.menu is not None:
            try:
                self.menu.destroy()
            except Exception:
                pass
            self.menu = None
            self.home_entry = None

    def _close_menu_and(self, cmd) -> None:
        self._close_menu()
        try:
            cmd()
        except Exception as e:
            print(f"menu action error: {e}")

    def _close_app(self) -> None:
        self._close_menu()
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------ clipboard

    def _get_clipboard_text(self) -> str:
        try:
            return self.root.clipboard_get()
        except tk.TclError:
            pass
        try:
            import ctypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            if not user32.OpenClipboard(0):
                return ""
            try:
                handle = user32.GetClipboardData(13)
                if not handle:
                    return ""
                ptr = kernel32.GlobalLock(handle)
                text = ctypes.wstring_at(ptr) if ptr else ""
                kernel32.GlobalUnlock(handle)
                return text
            finally:
                user32.CloseClipboard()
        except Exception:
            return ""

    def paste_clipboard(self) -> None:
        text = self._get_clipboard_text().strip().replace("\r", "").replace("\n", "")
        if not text:
            self.status.config(text="Буфер пуст", fg="#e66")
            return
        self.entry.delete(0, tk.END)
        self.entry.insert(0, text)
        self.entry.focus_set()
        self.entry.icursor(tk.END)
        self.status.config(text="Вставлено", fg="#8c8")
        self.go_to_coords()

    def _paste_into(self, widget: tk.Entry) -> None:
        text = self._get_clipboard_text().strip().replace("\r", "").replace("\n", "")
        if not text:
            return
        widget.delete(0, tk.END)
        widget.insert(0, text)

    def _on_paste(self, _event=None):
        self.paste_clipboard()
        return "break"

    def _hook_keyboard_paste(self) -> None:
        try:
            import keyboard as kb
        except Exception:
            return

        def on_ctrl_v() -> None:
            try:
                px, py = self.root.winfo_pointerxy()
                inside = (
                    self.root.winfo_rootx() <= px <= self.root.winfo_rootx() + self.root.winfo_width()
                    and self.root.winfo_rooty() <= py <= self.root.winfo_rooty() + self.root.winfo_height()
                )
                if inside or self.root.focus_get() is self.entry:
                    self.root.after(0, self.paste_clipboard)
            except Exception:
                pass

        try:
            kb.add_hotkey("ctrl+v", on_ctrl_v, suppress=False)
        except Exception:
            pass

    # ------------------------------------------------------------------ alert

    def _pin_outside_zone(self) -> bool:
        return self.zone is not None and not self.zone.contains(self.pin_lat, self.pin_lon)

    def _set_alert(self, active: bool) -> None:
        self._alert = active
        if active:
            root_bg, frame_bg, entry_bg = "#4a1010", "#5c1515", "#6b1a1a"
            hl, btn_bg, status_fg = "#e53935", "#8b2020", "#ffcdd2"
        else:
            root_bg, frame_bg, entry_bg = self._bg, self._bg, "#2d2d2d"
            hl, btn_bg, status_fg = "#555", "#3a3a3a", "#aaa"

        self.root.configure(bg=root_bg)
        self.frame.configure(bg=frame_bg)
        self.btn_row.configure(bg=frame_bg)
        self.status.configure(bg=frame_bg, fg=status_fg)
        self.entry.configure(bg=entry_bg, highlightbackground=hl,
                             highlightcolor="#e53935" if active else "#888")
        if self.home_entry is not None:
            try:
                self.home_entry.configure(bg=entry_bg, highlightbackground=hl,
                                          highlightcolor="#e53935" if active else "#888")
            except Exception:
                pass
        for child in self.btn_row.winfo_children():
            if child is self.status:
                continue
            if isinstance(child, tk.Button):
                child.configure(bg=btn_bg, fg=self._fg if not active else "#ffebee")

    # ------------------------------------------------------------------ motor api

    def _set_motors(self, left: int, right: int) -> None:
        set_l = self._motor_api.get("set_left")
        set_r = self._motor_api.get("set_right")
        if set_l:
            set_l(left)
        if set_r:
            set_r(right)
        sync = self._motor_api.get("sync_ui")
        if sync:
            try:
                sync(left, right)
            except Exception:
                pass

    # ------------------------------------------------------------------ дом

    def _apply_home(self) -> None:
        if self.home_entry is None:
            return
        try:
            lat, lon, _ = parse_coords(self.home_entry.get())
        except ValueError as e:
            self.status.config(text=f"Дом: {e}", fg="#e66")
            return
        self.home_lat, self.home_lon = lat, lon
        self.autopilot.set_home(lat, lon)
        self._redraw_home()
        self.status.config(text=f"Дом: {lat:.5f}, {lon:.5f}", fg="#8c8")

    # ------------------------------------------------------------------ миссия

    def start_mission(self) -> None:
        self.go_to_coords()
        if self.zone is not None:
            self.autopilot.set_zone(
                self.zone.min_lat, self.zone.max_lat, self.zone.min_lon, self.zone.max_lon
            )
            self._redraw_waypoints()
            self._warm_zone_tiles(self.zone, self.zoom)
        err = self.autopilot.start()
        if err:
            self.status.config(text=err, fg="#e66")
            return
        self.status.config(text="Миссия запущена", fg="#8c8")

    def _warm_zone_tiles(self, zone: Zone, zoom: int) -> None:
        try:
            tx0, ty0 = latlon_to_tile(zone.max_lat, zone.min_lon, zoom)
            tx1, ty1 = latlon_to_tile(zone.min_lat, zone.max_lon, zoom)
        except Exception:
            return
        _tile_executor.submit(
            prefetch_tiles,
            int(tx0) - 1, int(ty0) - 1, int(tx1) + 1, int(ty1) + 1, zoom,
        )

    def stop_mission(self) -> None:
        self.autopilot.stop("Стоп")
        self.status.config(text="Стоп", fg="#aaa")

    def _on_zone_shrunk(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> None:
        self.zone = Zone(min_lat, min_lon, max_lat, max_lon)
        self._redraw_zone()
        self._redraw_waypoints()
        self.reload_view()

    def _on_mission_done(self) -> None:
        self.status.config(text="Маршрут завершён", fg="#8c8")

    def clear_zone(self) -> None:
        if self.autopilot.is_running:
            self.stop_mission()
        self.zone = None
        self._redraw_zone()
        self._clear_waypoints()
        self._set_alert(False)
        self.status.config(text="Зона сброшена")

    def toggle_zone_mode(self) -> None:
        self._zone_mode = not self._zone_mode
        if self._zone_mode:
            self.canvas.config(cursor="crosshair")
            self.status.config(text="Режим зоны: тяните ЛКМ", fg="#4fc3f7")
        else:
            self._set_alert(self._pin_outside_zone())
            self.canvas.config(cursor="fleur")
            self.status.config(text="колёсико зум · ЛКМ сдвиг · ПКМ зона", fg="#aaa")

    # ------------------------------------------------------------------ координаты / GPS

    def go_to_coords(self) -> None:
        try:
            lat, lon, heading = parse_coords(self.entry.get())
        except ValueError as e:
            self.status.config(text=str(e), fg="#e66")
            return
        self.pin_lat, self.pin_lon = lat, lon
        self.pin_heading = heading
        self.center_lat, self.center_lon = lat, lon
        self._follow = True                # ручной ввод — снова следуем
        self.autopilot.update_pose(lat, lon, heading)
        self._set_alert(self.zone is not None and not self.zone.contains(lat, lon))
        self.reload_view()

    def center_on_gps(self) -> None:
        """Вернуть карту к текущей позиции и включить follow."""
        self._follow = True
        self.center_lat, self.center_lon = self.pin_lat, self.pin_lon
        self.reload_view()

    def apply_live_gps(
        self, lat: float, lon: float, heading: float | None = None, *, follow: bool = True
    ) -> None:
        """GPS-пакет: маркер обновляется всегда; центр — только в follow-режиме."""
        self.pin_lat, self.pin_lon = lat, lon
        self.pin_heading = heading
        self.autopilot.update_pose(lat, lon, heading)

        # Поле ввода
        focused = self.root.focus_get() if self.root.winfo_exists() else None
        if focused is not self.entry:
            text = (f"{lon:.6f},{lat:.6f},{heading:.0f}" if heading is not None
                    else f"{lon:.6f},{lat:.6f}")
            if self.entry.get() != text:
                self.entry.delete(0, tk.END)
                self.entry.insert(0, text)

        self._set_alert(self.zone is not None and not self.zone.contains(lat, lon))

        # Если пользователь не в follow-режиме — только маркер на текущем кадре.
        if not (follow and self._follow and not self._panning):
            if self._last_marker_px is not None:
                mx, my = _latlon_to_pixel_float(
                    lat, lon, self.center_lat, self.center_lon, self.zoom
                )
                if -30 <= mx <= MAP_W + 30 and -30 <= my <= MAP_H + 30:
                    self.marker.move(mx, my, heading)
                    self._last_marker_px = (mx, my)
                else:
                    self.marker.hide()
            self._update_status()
            return

        # follow: если пиксельное смещение мало — двигаем только маркер.
        if self._last_marker_px is not None:
            mx_old, my_old = _latlon_to_pixel_float(
                lat, lon, self.center_lat, self.center_lon, self.zoom
            )
            dx = MAP_W / 2 - mx_old
            dy = MAP_H / 2 - my_old
            if dx * dx + dy * dy < GPS_REDRAW_THRESHOLD_PX ** 2:
                self.marker.move(mx_old, my_old, heading)
                self._last_marker_px = (mx_old, my_old)
                self._update_status()
                return

        # сдвиг заметный — центрируем и рендерим
        self.center_lat, self.center_lon = lat, lon
        self._schedule_reload(delay_ms=60)

    def _update_status(self) -> None:
        htxt = f" · {self.pin_heading:.0f}°" if self.pin_heading is not None else ""
        follow_txt = "" if self._follow else " · off-center"
        self.status.config(
            text=f"{self.pin_lat:.5f}, {self.pin_lon:.5f}{htxt} · z{self.zoom}{follow_txt}",
            fg="#ffcdd2" if self._alert else "#8c8",
        )

    # ------------------------------------------------------------------ drag / zoom

    def _clear_drag_rect(self) -> None:
        if self._drag_id is not None:
            self.canvas.delete(self._drag_id)
            self._drag_id = None

    def _draw_drag_rect(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self._clear_drag_rect()
        self._drag_id = self.canvas.create_rectangle(
            x0, y0, x1, y1, outline="#4fc3f7", width=2, dash=(4, 2)
        )

    def _finish_zone(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self._clear_drag_rect()
        if abs(x1 - x0) < 8 or abs(y1 - y0) < 8:
            self.status.config(text="Зона слишком маленькая", fg="#e66")
            return
        lat1, lon1 = pixel_to_latlon(x0, y0, self.center_lat, self.center_lon, self.zoom)
        lat2, lon2 = pixel_to_latlon(x1, y1, self.center_lat, self.center_lon, self.zoom)
        self.zone = Zone(lat1, lon1, lat2, lon2)
        self.autopilot.set_zone(
            self.zone.min_lat, self.zone.max_lat, self.zone.min_lon, self.zone.max_lon
        )
        self._redraw_zone()
        self._redraw_waypoints()
        if self._zone_mode:
            self._zone_mode = False
            self.canvas.config(cursor="fleur")
        outside = self._pin_outside_zone()
        self._set_alert(outside)
        self.status.config(text="Зона: 4 точки", fg="#4fc3f7" if not outside else "#ffcdd2")

    def _on_press(self, event: tk.Event) -> None:
        self._drag_start = (event.x, event.y)
        self._drag_move_last = (event.x, event.y)
        self._panning = False
        self._pan_dx = 0
        self._pan_dy = 0
        if self._zone_mode:
            self._drag_kind = "zone"
            self._clear_drag_rect()
        else:
            self._drag_kind = "pan"
            self._pan_origin = (self.center_lat, self.center_lon)

    def _on_motion(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        dx, dy = event.x - x0, event.y - y0

        if self._drag_kind == "zone":
            self._draw_drag_rect(x0, y0, event.x, event.y)
            return

        if abs(dx) < 2 and abs(dy) < 2 and not self._panning:
            return

        # Пользователь начал двигать карту — выключаем follow.
        if not self._panning:
            self._panning = True
            self._follow = False

        mx = event.x - self._drag_move_last[0]
        my = event.y - self._drag_move_last[1]
        self._drag_move_last = (event.x, event.y)
        if mx or my:
            self._shift_canvas_items(mx, my)

        # Если уехали слишком далеко от уже отрисованного кадра — рендерим
        # от нового центра и «сбрасываем» якорь, чтобы дальнейший сдвиг шёл
        # уже от этого кадра.
        if (abs(event.x - x0) > TILE_SIZE * PAN_RERENDER_FRACTION
                or abs(event.y - y0) > TILE_SIZE * PAN_RERENDER_FRACTION):
            if self._pan_origin is not None:
                # центр от ИСХОДНОГО якоря — так не теряем общий сдвиг
                self.center_lat, self.center_lon = pixel_to_latlon(
                    MAP_W / 2 - (event.x - x0),
                    MAP_H / 2 - (event.y - y0),
                    self._pan_origin[0], self._pan_origin[1],
                    self.zoom,
                )
            self._drag_start = (event.x, event.y)
            self._pan_origin = (self.center_lat, self.center_lon)
            self.reload_view()

    def _shift_canvas_items(self, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            return
        self.canvas.move(self._img_id, dx, dy)
        if self._zone_id is not None:
            self.canvas.move(self._zone_id, dx, dy)
        if self._home_id is not None:
            self.canvas.move(self._home_id, dx, dy)
        for item in self._wp_ids:
            self.canvas.move(item, dx, dy)
        for i, (px, py, lat, lon, idx) in enumerate(self._wp_hits):
            self._wp_hits[i] = (px + dx, py + dy, lat, lon, idx)
        self.canvas.move(self.marker.circle, dx, dy)
        self.canvas.move(self.marker.line, dx, dy)
        self.canvas.move(self.marker.arrow, dx, dy)
        if self._last_marker_px is not None:
            self._last_marker_px = (
                self._last_marker_px[0] + dx,
                self._last_marker_px[1] + dy,
            )

    def _on_release(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        kind = self._drag_kind
        was_panning = self._panning
        self._drag_start = None
        self._drag_kind = None

        if kind == "zone":
            self._pan_origin = None
            self._finish_zone(x0, y0, event.x, event.y)
            return

        if was_panning:
            self._panning = False
            # Финальный центр считаем от исходного якоря, а не от промежуточных
            # срабатываний порога — иначе точка «прилипает» к последнему рендеру.
            if self._pan_origin is not None:
                self.center_lat, self.center_lon = pixel_to_latlon(
                    MAP_W / 2 - (event.x - x0),
                    MAP_H / 2 - (event.y - y0),
                    self._pan_origin[0], self._pan_origin[1],
                    self.zoom,
                )
            self._pan_origin = None
            self.reload_view()
            return

        # клик без сдвига
        self._pan_origin = None
        self._try_click_waypoint(event.x, event.y)

    def _on_zone_press(self, event: tk.Event) -> None:
        self._drag_start = (event.x, event.y)
        self._drag_kind = "zone"
        self._clear_drag_rect()
        self.canvas.config(cursor="crosshair")

    def _on_zone_motion(self, event: tk.Event) -> None:
        if self._drag_start is None or self._drag_kind != "zone":
            return
        x0, y0 = self._drag_start
        self._draw_drag_rect(x0, y0, event.x, event.y)

    def _on_zone_release(self, event: tk.Event) -> None:
        if self._drag_start is None or self._drag_kind != "zone":
            return
        x0, y0 = self._drag_start
        self._drag_start = None
        self._drag_kind = None
        self.canvas.config(cursor="crosshair" if self._zone_mode else "fleur")
        self._finish_zone(x0, y0, event.x, event.y)

    def _on_wheel(self, event: tk.Event) -> None:
        delta = 1 if event.delta > 0 else -1
        self._zoom_by(delta, event.x, event.y)

    def zoom_in(self) -> None:
        self._zoom_by(+1, MAP_W // 2, MAP_H // 2)

    def zoom_out(self) -> None:
        self._zoom_by(-1, MAP_W // 2, MAP_H // 2)

    def _zoom_by(self, delta: int, px: int, py: int) -> None:
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom + delta))
        if new_zoom == self.zoom:
            return
        lat, lon = pixel_to_latlon(px, py, self.center_lat, self.center_lon, self.zoom)
        tx, ty = latlon_to_tile(lat, lon, new_zoom)
        cx = tx - (px - MAP_W / 2) / TILE_SIZE
        cy = ty - (py - MAP_H / 2) / TILE_SIZE
        self.center_lat, self.center_lon = tile_to_latlon(cx, cy, new_zoom)
        self.zoom = new_zoom
        self.status.config(text=f"Зум {self.zoom}", fg="#aaa")
        self._schedule_reload(delay_ms=100)

    def _schedule_reload(self, delay_ms: int = 100) -> None:
        if self._reload_after is not None:
            try:
                self.root.after_cancel(self._reload_after)
            except Exception:
                pass
        self._reload_after = self.root.after(delay_ms, self.reload_view)

    # ------------------------------------------------------------------ overlays

    def _redraw_zone(self) -> None:
        if self._zone_id is not None:
            self.canvas.delete(self._zone_id)
            self._zone_id = None
        if self.zone is None:
            self._clear_waypoints()
            return
        x0, y0 = latlon_to_pixel(
            self.zone.max_lat, self.zone.min_lon, self.center_lat, self.center_lon, self.zoom
        )
        x1, y1 = latlon_to_pixel(
            self.zone.min_lat, self.zone.max_lon, self.center_lat, self.center_lon, self.zoom
        )
        self._zone_id = self.canvas.create_rectangle(
            x0, y0, x1, y1, outline="#4fc3f7", width=2, fill="#4fc3f7", stipple="gray50"
        )
        self._redraw_waypoints()
        self._redraw_home()

    def _clear_waypoints(self) -> None:
        for i in self._wp_ids:
            self.canvas.delete(i)
        self._wp_ids.clear()
        self._wp_hits.clear()

    def _try_click_waypoint(self, x: int, y: int) -> None:
        hit_r2 = 12 * 12
        best = None
        best_d2 = hit_r2
        for px, py, lat, lon, idx in getattr(self, "_wp_hits", []):
            d2 = (x - px) ** 2 + (y - py) ** 2
            if d2 <= best_d2:
                best_d2 = d2
                best = (idx, lat, lon)
        if best is None:
            return
        idx, lat, lon = best
        print(f"Точка {idx + 1}: {lon},{lat}  ({lat:.6f} N, {lon:.6f} E)", flush=True)
        self.status.config(text=f"Точка {idx + 1}: {lon:.6f},{lat:.6f}", fg="#8c8")

    def _redraw_waypoints(self) -> None:
        self._clear_waypoints()
        if self.zone is None:
            return
        corners = zone_corners(
            self.zone.min_lat, self.zone.max_lat, self.zone.min_lon, self.zone.max_lon
        )
        cur = self.autopilot.current_target_index if self.autopilot.is_running else -1
        for i, p in enumerate(corners):
            px, py = latlon_to_pixel(p.lat, p.lon, self.center_lat, self.center_lon, self.zoom)
            r = 5
            color = "#ffeb3b" if i == cur else "#00e676"
            if self.autopilot.is_running and i < cur and not self.autopilot.returning_home:
                color = "#9e9e9e"
            oval = self.canvas.create_oval(px - r, py - r, px + r, py + r, fill=color, outline="white")
            label = self.canvas.create_text(
                px + 8, py - 8, text=str(i + 1), fill="white",
                font=("Segoe UI", 7, "bold"), anchor="w",
            )
            self._wp_ids.extend([oval, label])
            self._wp_hits.append((px, py, p.lat, p.lon, i))

    def _redraw_home(self) -> None:
        if self._home_id is not None:
            self.canvas.delete(self._home_id)
            self._home_id = None
        if self.home_lat is None or self.home_lon is None:
            return
        px, py = latlon_to_pixel(
            self.home_lat, self.home_lon, self.center_lat, self.center_lon, self.zoom
        )
        self._home_id = self.canvas.create_polygon(
            px, py - 7, px + 6, py + 5, px - 6, py + 5, fill="#ff9800", outline="white"
        )

    # ------------------------------------------------------------------ кадры

    def _frame_cache_key(self, lat: float, lon: float, z: int):
        return (round(lat, 5), round(lon, 5), z)

    def _frame_cache_get(self, key):
        img = self._frame_cache.get(key)
        if img is not None:
            self._frame_cache.move_to_end(key)
        return img

    def _frame_cache_put(self, key, photo: ImageTk.PhotoImage) -> None:
        self._frame_cache[key] = photo
        self._frame_cache.move_to_end(key)
        while len(self._frame_cache) > self._frame_cache_max:
            self._frame_cache.popitem(last=False)

    # ------------------------------------------------------------------ рендер

    def reload_view(self, warn_outside: bool = False) -> None:
        self._reload_after = None
        self._load_gen += 1
        gen = self._load_gen
        clat, clon, z = self.center_lat, self.center_lon, self.zoom
        outside = warn_outside or self._pin_outside_zone()
        self._set_alert(outside)

        key = self._frame_cache_key(clat, clon, z)
        cached = self._frame_cache_get(key)
        if cached is not None:
            self._photo = cached
            self.canvas.itemconfig(self._img_id, image=self._photo)
            self.canvas.coords(self._img_id, 0, 0)
            self._redraw_zone()
            self._redraw_home()
            self._update_marker_after_render(clat, clon, z)
            self._update_status()
            return

        self.status.config(text=f"Загрузка… z{z}")

        def worker() -> None:
            try:
                fast_img, complete = render_map_fast(clat, clon, z)
                fast_err = None
            except Exception as e:
                fast_img, complete, fast_err = None, False, e

            def apply_fast() -> None:
                if gen != self._load_gen or not self.root.winfo_exists():
                    return
                if fast_err is not None or fast_img is None:
                    self.status.config(text=f"Ошибка: {fast_err}", fg="#e66")
                    return
                photo = ImageTk.PhotoImage(fast_img, master=self.root)
                if complete:
                    self._frame_cache_put(key, photo)
                self._photo = photo
                self.canvas.itemconfig(self._img_id, image=self._photo)
                self.canvas.coords(self._img_id, 0, 0)
                self._redraw_zone()
                self._redraw_home()
                self._update_marker_after_render(clat, clon, z)
                self._update_status()

            self.root.after(0, apply_fast)

            if not complete:
                try:
                    full_img = render_map_full(clat, clon, z)
                except Exception:
                    return

                def apply_full() -> None:
                    if gen != self._load_gen or not self.root.winfo_exists():
                        return
                    photo = ImageTk.PhotoImage(full_img, master=self.root)
                    self._frame_cache_put(key, photo)
                    self._photo = photo
                    self.canvas.itemconfig(self._img_id, image=self._photo)
                    self.canvas.coords(self._img_id, 0, 0)
                    self._redraw_zone()
                    self._redraw_home()
                    self._update_marker_after_render(clat, clon, z)
                    self._update_status()

                self.root.after(0, apply_full)

        threading.Thread(target=worker, daemon=True).start()

    def _update_marker_after_render(self, clat: float, clon: float, z: int) -> None:
        mx, my = _latlon_to_pixel_float(self.pin_lat, self.pin_lon, clat, clon, z)
        if -30 <= mx <= MAP_W + 30 and -30 <= my <= MAP_H + 30:
            self.marker.move(mx, my, self.pin_heading)
        else:
            self.marker.hide()
        self._last_marker_px = (mx, my)

    def update_map(self) -> None:
        self.go_to_coords()

    def run(self) -> None:
        if not self._owns_mainloop:
            return
        self.root.lift()
        self.root.focus_force()
        self.root.mainloop()


if __name__ == "__main__":
    print("Starting minimap overlay...", flush=True)
    try:
        MiniMapApp().run()
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        raise