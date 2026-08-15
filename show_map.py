"""Миникарта в правом нижнем углу экрана (как в игре)."""

from __future__ import annotations

import io
import math
import threading
import tkinter as tk
from collections import OrderedDict
from urllib.parse import unquote
from urllib.request import Request, urlopen

from PIL import Image, ImageDraw, ImageTk

from autopilot import Autopilot, zone_corners

MAP_W, MAP_H = 220, 220
DEFAULT_ZOOM = 15
MIN_ZOOM, MAX_ZOOM = 3, 18
TILE_SIZE = 256
MARGIN = 12
USER_AGENT = "AtlantMiniMap/1.0 (local overlay)"
GAP = 8
TILE_CACHE_MAX = 128
CHROME_H = 110  # поле дома + кнопки старта


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_to_latlon(x: float, y: float, zoom: int) -> tuple[float, float]:
    n = 2.0**zoom
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


_tile_cache: OrderedDict[tuple[int, int, int], Image.Image] = OrderedDict()
_tile_lock = threading.Lock()


def fetch_tile(x: int, y: int, zoom: int) -> Image.Image:
    n = 2**zoom
    x %= n
    y = max(0, min(n - 1, y))
    key = (zoom, x, y)
    with _tile_lock:
        if key in _tile_cache:
            _tile_cache.move_to_end(key)
            return _tile_cache[key]

    url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=8) as resp:
        tile = Image.open(io.BytesIO(resp.read())).convert("RGB")

    with _tile_lock:
        _tile_cache[key] = tile
        while len(_tile_cache) > TILE_CACHE_MAX:
            _tile_cache.popitem(last=False)
    return tile


def draw_heading_marker(
    img: Image.Image, mx: float, my: float, heading_deg: float | None
) -> None:
    """Маркер: круг + стрелка компаса (0° = север, по часовой)."""
    draw = ImageDraw.Draw(img)
    r = 7
    draw.ellipse(
        (mx - r, my - r, mx + r, my + r),
        fill=(220, 40, 40),
        outline=(255, 255, 255),
        width=2,
    )
    if heading_deg is None:
        draw.line((mx, my - r - 3, mx, my + r + 3), fill=(255, 255, 255), width=1)
        draw.line((mx - r - 3, my, mx + r + 3, my), fill=(255, 255, 255), width=1)
        return

    # 0° — вверх (север), дальше по часовой
    rad = math.radians(heading_deg % 360.0)
    dx = math.sin(rad)
    dy = -math.cos(rad)
    length = 18
    tip_x = mx + dx * length
    tip_y = my + dy * length
    # основание стрелки (перпендикуляр)
    px, py = -dy, dx
    base = 6
    left = (mx - dx * 4 + px * base, my - dy * 4 + py * base)
    right = (mx - dx * 4 - px * base, my - dy * 4 - py * base)
    draw.polygon(
        [(tip_x, tip_y), left, right],
        fill=(255, 220, 50),
        outline=(255, 255, 255),
    )
    draw.line((mx, my, tip_x, tip_y), fill=(255, 220, 50), width=2)


def render_map(
    center_lat: float,
    center_lon: float,
    zoom: int,
    pin_lat: float | None = None,
    pin_lon: float | None = None,
    heading: float | None = None,
) -> Image.Image:
    left, top = view_origin(center_lat, center_lon, zoom)
    img = Image.new("RGB", (MAP_W, MAP_H), (40, 40, 40))

    x0 = math.floor(left / TILE_SIZE)
    y0 = math.floor(top / TILE_SIZE)
    x1 = math.floor((left + MAP_W) / TILE_SIZE)
    y1 = math.floor((top + MAP_H) / TILE_SIZE)

    for ty in range(y0, y1 + 1):
        for tx in range(x0, x1 + 1):
            try:
                tile = fetch_tile(tx, ty, zoom)
            except Exception:
                tile = Image.new("RGB", (TILE_SIZE, TILE_SIZE), (60, 60, 60))
            img.paste(tile, (int(tx * TILE_SIZE - left), int(ty * TILE_SIZE - top)))

    if pin_lat is not None and pin_lon is not None:
        mx, my = latlon_to_pixel(pin_lat, pin_lon, center_lat, center_lon, zoom)
        if -20 <= mx <= MAP_W + 20 and -20 <= my <= MAP_H + 20:
            draw_heading_marker(img, mx, my, heading)
    return img


def _split_coord_and_heading(token: str) -> tuple[float, float | None]:
    """
    '43.170572.10' → (43.170572, 10.0)
    '43.170572'    → (43.170572, None)
    """
    token = token.strip()
    # широта/долгота уже с одной точкой; угол — после второй
    parts = token.split(".")
    if len(parts) >= 3:
        # -43.170572.10 → ['-43', '170572', '10'] или ['43', '170572', '10']
        head = ".".join(parts[:-1])
        heading = float(parts[-1])
        return float(head), heading
    return float(token), None


def parse_coords(text: str) -> tuple[float, float, float | None]:
    """
    Форматы:
      lon%2Clat          → lat, lon, None
      lon%2Clat.угол     → lat, lon, угол  (пример: 131.955897%2C43.170572.10)
      lat lon [угол]
    Возвращает (lat, lon, heading|None). Угол компаса: 0°=север, по часовой.
    """
    raw = unquote(text.strip())
    comma_style = "," in raw
    # "lon,lat.10" или "lon, lat, 10"
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
            self.root.attributes("-alpha", 0.92)
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

        self.entry = tk.Entry(
            frame,
            font=("Segoe UI", 10),
            bg="#2d2d2d",
            fg=fg,
            insertbackground=fg,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#555",
            highlightcolor="#888",
        )
        self.entry.pack(fill="x", pady=(0, 2))
        self.entry.insert(0, "131.955897%2C43.170572.10")
        self.entry.bind("<Return>", lambda _e: self.go_to_coords())
        self.entry.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<Control-v>", self._on_paste)
        self.entry.bind("<Control-V>", self._on_paste)
        self.entry.bind("<Shift-Insert>", self._on_paste)
        self.entry.bind("<<Paste>>", self._on_paste)
        self.entry.bind("<Button-3>", self._on_paste)
        self.root.bind("<Control-v>", self._on_paste)
        self.root.bind("<Control-V>", self._on_paste)

        # Точка дома рядом с текущим местоположением
        home_row = tk.Frame(frame, bg=bg)
        home_row.pack(fill="x", pady=(0, 2))
        tk.Label(home_row, text="Дом", font=("Segoe UI", 8), bg=bg, fg="#aaa").pack(side="left")
        self.home_entry = tk.Entry(
            home_row,
            font=("Segoe UI", 10),
            bg="#2d2d2d",
            fg=fg,
            insertbackground=fg,
            relief="flat",
            highlightthickness=1,
            highlightbackground="#555",
            highlightcolor="#888",
        )
        self.home_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
        self.home_entry.insert(0, "131.955897%2C43.170572")
        self.home_entry.bind("<Return>", lambda _e: self._apply_home())
        self.home_entry.bind("<Control-v>", lambda e: self._paste_into(self.home_entry) or "break")
        self.home_entry.bind("<Control-V>", lambda e: self._paste_into(self.home_entry) or "break")
        self.home_entry.bind("<Button-3>", lambda e: self._paste_into(self.home_entry) or "break")

        self.btn_row = tk.Frame(frame, bg=bg)
        self.btn_row.pack(fill="x", pady=(0, 2))
        btn_row = self.btn_row

        self.status = tk.Label(
            btn_row,
            text="колёсико зум · ЛКМ сдвиг",
            font=("Segoe UI", 7),
            bg=bg,
            fg="#aaa",
            anchor="w",
        )
        self.status.pack(side="left", fill="x", expand=True)

        for text, cmd in (("−", self.zoom_out), ("+", self.zoom_in)):
            tk.Button(
                btn_row,
                text=text,
                command=cmd,
                font=("Segoe UI", 9, "bold"),
                bg="#3a3a3a",
                fg=fg,
                relief="flat",
                padx=6,
                cursor="hand2",
            ).pack(side="right", padx=(2, 0))

        tk.Button(
            btn_row,
            text="OK",
            command=self.go_to_coords,
            font=("Segoe UI", 8),
            bg="#3a3a3a",
            fg=fg,
            relief="flat",
            padx=6,
            cursor="hand2",
        ).pack(side="right", padx=(3, 0))

        tk.Button(
            btn_row,
            text="Вставить",
            command=self.paste_clipboard,
            font=("Segoe UI", 8),
            bg="#3a3a3a",
            fg=fg,
            relief="flat",
            padx=6,
            cursor="hand2",
        ).pack(side="right", padx=(3, 0))

        self.zone_btn = tk.Button(
            btn_row,
            text="Зона",
            command=self.toggle_zone_mode,
            font=("Segoe UI", 8),
            bg="#3a3a3a",
            fg=fg,
            relief="flat",
            padx=6,
            cursor="hand2",
        )
        self.zone_btn.pack(side="right", padx=(3, 0))

        tk.Button(
            btn_row,
            text="Сброс",
            command=self.clear_zone,
            font=("Segoe UI", 8),
            bg="#3a3a3a",
            fg=fg,
            relief="flat",
            padx=6,
            cursor="hand2",
        ).pack(side="right")

        nav_row = tk.Frame(frame, bg=bg)
        nav_row.pack(fill="x", pady=(0, 2))
        self.start_btn = tk.Button(
            nav_row,
            text="СТАРТ",
            command=self.start_mission,
            font=("Segoe UI", 9, "bold"),
            bg="#2e7d32",
            fg="white",
            relief="flat",
            padx=10,
            cursor="hand2",
        )
        self.start_btn.pack(side="left", padx=(0, 4))
        self.stop_btn = tk.Button(
            nav_row,
            text="СТОП",
            command=self.stop_mission,
            font=("Segoe UI", 9, "bold"),
            bg="#c62828",
            fg="white",
            relief="flat",
            padx=10,
            cursor="hand2",
        )
        self.stop_btn.pack(side="left")

        self.canvas = tk.Canvas(
            frame, width=MAP_W, height=MAP_H, bg="#111", highlightthickness=0, cursor="fleur"
        )
        self.canvas.pack()

        placeholder = Image.new("RGB", (MAP_W, MAP_H), (30, 30, 30))
        ImageDraw.Draw(placeholder).text((MAP_W // 2 - 40, MAP_H // 2 - 8), "Загрузка…", fill=(180, 180, 180))
        self._photo = ImageTk.PhotoImage(placeholder, master=self.root)
        self._img_id = self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._zone_id: int | None = None
        self._drag_id: int | None = None
        self._wp_ids: list[int] = []
        self._wp_hits: list[tuple[float, float, float, float, int]] = []
        self._home_id: int | None = None

        self.pin_lat = 43.170572
        self.pin_lon = 131.955897
        self.pin_heading: float | None = 10.0
        self.zone: Zone | None = None
        self.center_lat = 43.170572
        self.center_lon = 131.955897
        self.zoom = DEFAULT_ZOOM
        self.home_lat: float | None = None
        self.home_lon: float | None = None

        self._zone_mode = False
        self._drag_kind: str | None = None
        self._drag_start: tuple[int, int] | None = None
        self._pan_origin: tuple[float, float] | None = None
        self._load_gen = 0
        self._reload_after: str | None = None
        self._panning = False

        self.autopilot = Autopilot(
            self._set_motors,
            on_status=lambda s: self.root.after(0, lambda: self.status.config(text=s[:48])),
            on_zone=lambda a, b, c, d: self.root.after(0, lambda: self._on_zone_shrunk(a, b, c, d)),
            on_done=lambda: self.root.after(0, self._on_mission_done),
        )
        self._apply_home(silent=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # ПКМ на карте — выделение зоны (надёжнее, чем Shift)
        self.canvas.bind("<ButtonPress-3>", self._on_zone_press)
        self.canvas.bind("<B3-Motion>", self._on_zone_motion)
        self.canvas.bind("<ButtonRelease-3>", self._on_zone_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self._zoom_by(+1, e.x, e.y))
        self.canvas.bind("<Button-5>", lambda e: self._zoom_by(-1, e.x, e.y))
        if self._owns_mainloop:
            self.root.bind("<Escape>", lambda _e: self.root.destroy())

        self._hook_keyboard_paste()
        print(f"MiniMap (frameless) at ({x}, {y}) size {win_w}x{win_h}", flush=True)
        self.root.after(100, self.reload_view)

    # --- clipboard ---

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

    # --- zone / coords ---

    def _pin_outside_zone(self) -> bool:
        return self.zone is not None and not self.zone.contains(self.pin_lat, self.pin_lon)

    def _set_alert(self, active: bool) -> None:
        """Красная подсветка интерфейса вместо длинного текста-предупреждения."""
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
        self.entry.configure(
            bg=entry_bg,
            highlightbackground=hl,
            highlightcolor="#e53935" if active else "#888",
        )
        self.home_entry.configure(
            bg=entry_bg,
            highlightbackground=hl,
            highlightcolor="#e53935" if active else "#888",
        )
        for child in self.btn_row.winfo_children():
            if child is self.status:
                continue
            if child is self.zone_btn and self._zone_mode and not active:
                child.configure(bg="#1565c0", fg="white")
            elif isinstance(child, tk.Button):
                child.configure(bg=btn_bg, fg=self._fg if not active else "#ffebee")

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

    def _apply_home(self, silent: bool = False) -> None:
        try:
            lat, lon, _ = parse_coords(self.home_entry.get())
        except ValueError as e:
            if not silent:
                self.status.config(text=f"Дом: {e}", fg="#e66")
            return
        self.home_lat, self.home_lon = lat, lon
        self.autopilot.set_home(lat, lon)
        self._redraw_home()
        if not silent:
            self.status.config(text=f"Дом: {lat:.5f}, {lon:.5f}", fg="#8c8")

    def start_mission(self) -> None:
        self._apply_home(silent=True)
        self.go_to_coords()
        if self.zone is not None:
            self.autopilot.set_zone(
                self.zone.min_lat, self.zone.max_lat, self.zone.min_lon, self.zone.max_lon
            )
            self._redraw_waypoints()
        err = self.autopilot.start()
        if err:
            self.status.config(text=err, fg="#e66")
            return
        self.start_btn.config(bg="#1b5e20")
        self.status.config(text="Миссия запущена", fg="#8c8")

    def stop_mission(self) -> None:
        self.autopilot.stop("Стоп")
        self.start_btn.config(bg="#2e7d32")

    def _on_zone_shrunk(self, min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> None:
        self.zone = Zone(min_lat, min_lon, max_lat, max_lon)
        self._redraw_zone()
        self._redraw_waypoints()
        self.reload_view()

    def _on_mission_done(self) -> None:
        self.start_btn.config(bg="#2e7d32")
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
            if not self._alert:
                self.zone_btn.config(bg="#1565c0", fg="white")
            self.canvas.config(cursor="crosshair")
            self.status.config(text="Режим зоны: тяните ЛКМ", fg="#4fc3f7")
        else:
            self._set_alert(self._pin_outside_zone())
            if not self._alert:
                self.zone_btn.config(bg="#3a3a3a", fg=self._fg)
            self.canvas.config(cursor="fleur")
            self.status.config(text="колёсико зум · ЛКМ сдвиг", fg="#aaa")

    def go_to_coords(self) -> None:
        try:
            lat, lon, heading = parse_coords(self.entry.get())
        except ValueError as e:
            self.status.config(text=str(e), fg="#e66")
            return
        self.pin_lat, self.pin_lon = lat, lon
        self.pin_heading = heading
        self.center_lat, self.center_lon = lat, lon
        self.autopilot.update_pose(lat, lon, heading)
        self._set_alert(self.zone is not None and not self.zone.contains(lat, lon))
        self.reload_view()

    # --- pan / zoom / zone input ---

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
        self._panning = False
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
        self._panning = True
        if self._pan_origin is None:
            return
        olat, olon = self._pan_origin
        self.center_lat, self.center_lon = pixel_to_latlon(
            MAP_W / 2 - dx, MAP_H / 2 - dy, olat, olon, self.zoom
        )
        self._schedule_reload(delay_ms=80)

    def _on_release(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        kind = self._drag_kind
        was_panning = self._panning
        self._drag_start = None
        self._pan_origin = None
        self._drag_kind = None

        if kind == "zone":
            self._finish_zone(x0, y0, event.x, event.y)
            return

        if was_panning:
            self._panning = False
            self.reload_view()
            return

        # клик без сдвига — попадание в точку маршрута
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
        # точка под курсором остаётся на месте
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
                px + 8, py - 8, text=str(i + 1), fill="white", font=("Segoe UI", 7, "bold"), anchor="w"
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

    def reload_view(self, warn_outside: bool = False) -> None:
        self._reload_after = None
        self._load_gen += 1
        gen = self._load_gen
        clat, clon, z = self.center_lat, self.center_lon, self.zoom
        plat, plon, phd = self.pin_lat, self.pin_lon, self.pin_heading
        outside = warn_outside or self._pin_outside_zone()
        self._set_alert(outside)
        self.status.config(text=f"Загрузка… z{z}")

        def worker() -> None:
            try:
                img = render_map(clat, clon, z, plat, plon, phd)
                err = None
            except Exception as e:
                img = None
                err = e

            def apply() -> None:
                if gen != self._load_gen:
                    return
                if err is not None or img is None:
                    self.status.config(text=f"Ошибка сети: {err}", fg="#e66")
                    return
                self._photo = ImageTk.PhotoImage(img, master=self.root)
                self.canvas.itemconfig(self._img_id, image=self._photo)
                self._redraw_zone()
                self._set_alert(self._pin_outside_zone())
                htxt = f" · {self.pin_heading:.0f}°" if self.pin_heading is not None else ""
                self.status.config(
                    text=f"{self.pin_lat:.5f}, {self.pin_lon:.5f}{htxt} · z{self.zoom}",
                    fg="#ffcdd2" if self._alert else "#8c8",
                )
                self._redraw_waypoints()
                self._redraw_home()

            self.root.after(0, apply)

        threading.Thread(target=worker, daemon=True).start()

    # совместимость со старым вызовом
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
